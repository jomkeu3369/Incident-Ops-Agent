from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
import time
import uuid
from typing import Any, AsyncIterator
import anyio
import httpx
from fastapi import HTTPException, status
from pydantic import ValidationError
from ag_ui.core import (
    CustomEvent,
    MessagesSnapshotEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder

from common.a2a_utils import A2ATaskNotActiveError, cancel_a2a_task, send_a2a_json
from common.backend_client import BackendApiError, BackendClient
from common.config import Settings
from common.event_log import EventBus, publish_safe
from common.observability import end_span, langsmith_tracing_scope, traced_span
from supervisor.active_runs import ActiveRun, was_cancelled_by_request
from supervisor.cruds import SupervisorRedisStore
from supervisor.graph import A2ACaller, build_supervisor_graph
from supervisor.ledger import (
    OPEN_APPROVAL_STATUSES,
    approval_status,
    approval_view,
    find_approval,
    find_execution,
    find_open_approval,
    find_plan,
    is_resolved,
    latest_approval,
    reconcile_snapshot,
)
from supervisor.schemas import ForwardedPropsInput
from supervisor.streaming import settle


def _extract_last_user_text(run_input: RunAgentInput) -> str:
    """RunAgentInput.messages에서 마지막 사용자 메시지 텍스트를 추출한다."""
    for msg in reversed(run_input.messages or []):
        if getattr(msg, "role", None) == "user":
            content = getattr(msg, "content", "")
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return ""


def _load_fresh_tool_cache(memory: dict[str, Any], ttl_seconds: float, now: float) -> tuple[dict[str, Any], dict[str, float]]:
    """TTL이 유효한 스레드 Tool 캐시 항목과 항목별 적재 시각을 반환하고, 만료된 항목은 제거한다."""
    if ttl_seconds <= 0:
        return {}, {}
    raw_cache = memory.get("cachedToolResults")
    if not isinstance(raw_cache, dict) or not raw_cache:
        return {}, {}
    raw_timestamps = memory.get("cachedToolResultsTimestamps")
    timestamps = raw_timestamps if isinstance(raw_timestamps, dict) else {}
    legacy_cached_at = memory.get("cachedToolResultsAt")
    fresh_cache: dict[str, Any] = {}
    fresh_timestamps: dict[str, float] = {}
    for key, val in raw_cache.items():
        ts = timestamps.get(key, legacy_cached_at)
        if isinstance(ts, (int, float)) and (now - float(ts)) <= ttl_seconds:
            fresh_cache[key] = val
            fresh_timestamps[key] = float(ts)
    return fresh_cache, fresh_timestamps


def _isolate_incident_memory(memory: dict[str, Any], thread_id: str, incident_code: str) -> dict[str, Any]:
    """스레드에 묶인 장애 코드가 다르면 이전 장애의 근거·스냅샷·Tool 캐시 상속을 차단한다."""
    bound_incident = memory.get("incidentCode") or (memory.get("snapshot") or {}).get("incidentCode")
    if bound_incident and bound_incident != incident_code:
        return {
            "threadId": thread_id,
            "incidentCode": incident_code,
            "messages": [],
            "constraints": {},
            "snapshot": None,
            "replanHistory": [],
            "pulledChunkIds": [],
            "cachedToolResults": {},
            "cachedToolResultsTimestamps": {},
        }
    return memory


class SupervisorService:
    """AG-UI 스트리밍·HITL 승인·LangGraph 5단계 오케스트레이션을 총괄하는 서비스."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient, backend: BackendClient, store: SupervisorRedisStore, event_bus: EventBus | None = None, a2a_caller: A2ACaller | None = None, langsmith_client: Any | None = None, checkpointer: Any | None = None) -> None:
        self._settings = settings
        self._http = http_client
        self._backend = backend
        self._store = store
        self._bus = event_bus
        self._a2a_caller = a2a_caller
        self._langsmith_client = langsmith_client
        self._checkpointer = checkpointer
        self._decision_epoch: int | None = None

    def parse_forwarded_props(self, run_input: RunAgentInput) -> ForwardedPropsInput:
        """스트림을 열기 전에 forwardedProps 계약을 검증한다(위반 시 HTTP 400)."""
        if not isinstance(run_input.forwarded_props, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="forwardedProps.incidentCode는 필수입니다.")
        try:
            return ForwardedPropsInput.model_validate(run_input.forwarded_props)
        except ValidationError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="forwardedProps 형식이 올바르지 않습니다.")

    async def prepare_approval_decision(self, props: ForwardedPropsInput, operator_id: str, thread_id: str | None = None) -> dict[str, Any] | None:
        """승인·반려 요청이면 스트림 오픈 전에 Spring 원장에 결정을 반영하며, 스레드에 반영되지 못한 기존 결정은 이어서 처리한다."""
        if props.approvalDecision is None:
            return None
        # 승인 저장 중에 들어온 취소를 놓치지 않도록 저장 전 값을 기억한다
        self._decision_epoch = None if thread_id is None else self._store.cancel_epoch(thread_id, props.incidentCode)
        try:
            return await self._backend.decide_approval(
                props.incidentCode,
                props.approvalDecision.approvalId,
                approved_by=operator_id,
                decision=props.approvalDecision.decision,
            )
        except BackendApiError as exc:
            # 같은 결정이 이미 원장에 있으면(응답 유실·실행 중단) 스트림에서 원장 상태를 이어 처리한다.
            if exc.status_code == 409:
                recorded = await self._unfinished_decision(props, thread_id)
                if recorded is not None:
                    return recorded
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    async def _unfinished_decision(self, props: ForwardedPropsInput, thread_id: str | None) -> dict[str, Any] | None:
        """원장에 같은 결정이 있고 이어서 처리할 일이 남았을 때만 승인 행을 반환한다."""
        approval_id = props.approvalDecision.approvalId  # type: ignore[union-attr]
        detail = await self._ledger_detail(props.incidentCode)
        recorded = find_approval(detail, approval_id)
        if recorded is None or recorded.get("decision") != props.approvalDecision.decision:  # type: ignore[union-attr]
            return None
        if recorded.get("decision") == "APPROVED" and find_execution(detail, approval_id) is None:
            # 실행 기록이 없는 승인은 화면 상태와 무관하게 이어서 실행할 수 있다
            return None if is_resolved(detail) else recorded
        # 종결된 결정의 재전송: 더 새로운 승인을 덮지 않고, 아직 전달하지 못한 결과만 복구한다
        if find_open_approval(detail) is not None:
            return None
        if thread_id is not None:
            memory = _isolate_incident_memory(await self._store.get_thread_memory(thread_id), thread_id, props.incidentCode)
            snapshot = memory.get("snapshot") or {}
            known_id = (snapshot.get("approval") or {}).get("approvalId")
            if known_id and known_id != approval_id:
                return None
            if known_id and snapshot.get("workflowStatus") in ("RESOLVED", "EXECUTION_FAILED", "REJECTED"):
                return None
        return recorded

    async def _ledger_detail(self, incident_code: str) -> dict[str, Any] | None:
        """원장을 조회한다. 확인할 수 없으면 None(상태를 추정하지 않는다)."""
        try:
            detail = await self._backend.get_incident_detail(incident_code)
        except Exception:
            return None
        return detail if isinstance(detail, dict) else None

    async def _load_plan_from_backend(self, incident_code: str, plan_id: str | None) -> dict[str, Any] | None:
        """승인 대상 planId에 해당하는 계획 정보를 백엔드 상세 응답에서 조회한다."""
        return find_plan(await self._ledger_detail(incident_code), plan_id)

    async def _commit_snapshot(self, thread_id: str, memory: dict[str, Any], props: ForwardedPropsInput, snapshot: dict[str, Any], last_step: str) -> dict[str, Any]:
        """모든 상태 전이에 같은 규칙을 적용한다: 순번을 올리고 저장한 뒤에만 전송한다."""
        sequence = max(int(memory.get("lastEventSeq") or 0), props.lastEventSeq or 0) + 1
        committed = {**snapshot, "eventSeq": sequence}
        memory.update(incidentCode=props.incidentCode, snapshot=committed, lastEventSeq=sequence, lastCompletedStep=last_step)
        await self._store.save_thread_memory(thread_id, memory)
        return committed

    async def stream_ag_ui(self, run_input: RunAgentInput, props: ForwardedPropsInput, operator_id: str, decided_approval: dict[str, Any] | None, accept_header: str | None) -> AsyncIterator[str]:
        """AG-UI 표준 SSE 이벤트 스트림을 생성한다."""
        encoder = EventEncoder(accept=accept_header)
        tid, rid = run_input.thread_id, run_input.run_id

        try:
            yield encoder.encode(RunStartedEvent(thread_id=tid, run_id=rid))

            # 1) 새로고침 복원 흐름 (restore=True, 워커 호출·원장 쓰기 없음)
            if props.restore:
                async with aclosing(self._handle_restore_stream(encoder, tid, rid, props)) as stream:
                    async for chunk in stream:
                        yield chunk
                return

            # 2) 명시적 취소 흐름 (단순 연결 단절과 분리: 실행 중인 작업에 먼저 신호를 보낸다)
            if props.explicitCancel:
                async with aclosing(self._handle_cancel_stream(encoder, tid, rid, props, operator_id)) as stream:
                    async for chunk in stream:
                        yield chunk
                return

            # 락 대기 중인 실행도 취소할 수 있도록 락보다 먼저 등록한다
            with self._store.track_run(tid, ActiveRun(props.incidentCode)) as run:
                if props.approvalDecision is not None and self._decision_epoch not in (None, self._store.cancel_epoch(tid, props.incidentCode)):
                    run.request_cancel()
                async with self._store.thread_lock(tid):
                    if run.cancel_requested:
                        # 시작 전에 취소됨: 그 사이 커밋된 결정이 있으면 화면 상태만 원장에 맞춘다
                        memory = _isolate_incident_memory(await self._store.get_thread_memory(tid), tid, props.incidentCode)
                        stored = memory.get("snapshot")
                        if stored:
                            current = reconcile_snapshot(stored, await self._ledger_detail(props.incidentCode))
                            if current is not stored:
                                committed = await self._commit_snapshot(tid, memory, props, current, "ledger_reconciled")
                                yield encoder.encode(StateSnapshotEvent(snapshot=committed))
                        yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))
                        return
                    memory = _isolate_incident_memory(await self._store.get_thread_memory(tid), tid, props.incidentCode)

                    # 3) 운영자 승인/반려 처리 흐름
                    if props.approvalDecision is not None and decided_approval is not None:
                        stream = self._handle_approval_stream(encoder, tid, rid, props, operator_id, decided_approval, memory, run)
                    # 4) 일반 계획 요청 / 체크포인트 재개 흐름
                    else:
                        stream = self._handle_planning_stream(encoder, run_input, props, operator_id, memory, run)
                    # 단절 시 하위 스트림의 정리(태스크 취소·원장 쓰기 대기)가 끝난 뒤 락을 놓는다
                    async with aclosing(stream):
                        async for chunk in stream:
                            yield chunk
        except Exception:
            yield encoder.encode(RunErrorEvent(message="에이전트 실행 중 오류가 발생했습니다.", code="AGENT_EXECUTION_ERROR"))

    async def _handle_restore_stream(self, encoder: EventEncoder, tid: str, rid: str, props: ForwardedPropsInput) -> AsyncIterator[str]:
        """저장 스냅샷을 원장과 대조해 복원한다. 원장이 앞서 있으면 그 상태를 저장하고 보낸다."""
        memory = _isolate_incident_memory(await self._store.get_thread_memory(tid), tid, props.incidentCode)
        snapshot = memory.get("snapshot") or _idle_snapshot(props.incidentCode, memory.get("replanHistory"))
        # 진행 중인 실행이 있거나 원장이 앞서면, 실행의 저장과 직렬화한 뒤 다시 읽는다
        if self._store.active_runs(tid) or reconcile_snapshot(snapshot, await self._ledger_detail(props.incidentCode)) is not snapshot:
            async with self._store.thread_lock(tid):
                memory = _isolate_incident_memory(await self._store.get_thread_memory(tid), tid, props.incidentCode)
                latest = memory.get("snapshot") or _idle_snapshot(props.incidentCode, memory.get("replanHistory"))
                current = reconcile_snapshot(latest, await self._ledger_detail(props.incidentCode))
                snapshot = latest if current is latest else await self._commit_snapshot(tid, memory, props, current, "ledger_reconciled")
        yield encoder.encode(StateSnapshotEvent(snapshot=snapshot))
        yield encoder.encode(MessagesSnapshotEvent(messages=memory.get("messages") or []))
        yield encoder.encode(_checkpoint_sync(props.incidentCode, memory, snapshot))
        yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))

    async def _handle_cancel_stream(self, encoder: EventEncoder, tid: str, rid: str, props: ForwardedPropsInput, operator_id: str) -> AsyncIterator[str]:
        """진행 중인 작업을 중단한 뒤 원장을 확인해 상태를 확정한다. 커밋된 업무 상태는 덮어쓰지 않는다."""
        runs = self._store.active_runs(tid)
        scope_error = RunErrorEvent(message="이 스레드의 작업은 다른 장애에 속해 있어 취소하지 않았습니다.", code="CANCEL_SCOPE_MISMATCH")
        if any(run.incident_code != props.incidentCode for run in runs):
            yield encoder.encode(scope_error)
            return
        self._store.bump_cancel_epoch(tid, props.incidentCode)
        for run in runs:
            run.request_cancel()

        async with self._store.thread_lock(tid):
            raw = await self._store.get_thread_memory(tid)
            bound = raw.get("incidentCode") or (raw.get("snapshot") or {}).get("incidentCode")
            if bound and bound != props.incidentCode and not runs:
                yield encoder.encode(scope_error)
                return
            memory = _isolate_incident_memory(raw, tid, props.incidentCode)
            # 명시적 취소만 원격 작업까지 중단한다(단순 연결 단절은 재개를 위해 남겨 둔다).
            # 취소 요청마저 끊겨도 빠지지 않도록 보호 구간에서 보낸다
            with anyio.CancelScope(shield=True):
                unconfirmed = await self._cancel_remote_tasks(tid, props.incidentCode)
            draft_plan = await self._checkpoint_plan(tid)
            if self._checkpointer is not None and hasattr(self._checkpointer, "adelete_thread"):
                await self._checkpointer.adelete_thread(tid)
            detail = await self._ledger_detail(props.incidentCode)
            if detail is None:
                yield encoder.encode(RunErrorEvent(message="작업 중단은 요청했지만 승인 원장을 확인할 수 없습니다. 상태를 다시 조회해 주세요.", code="LEDGER_UNAVAILABLE"))
                return

            snapshot, text = _snapshot_after_cancel(props.incidentCode, memory.get("snapshot"), detail, draft_plan)
            if unconfirmed:
                text += " 원격 조사 중단은 확인하지 못했습니다. 다시 취소하면 재시도합니다."
            msg_id = f"msg-a-{uuid.uuid4().hex[:6]}"
            memory.setdefault("messages", []).append({"id": msg_id, "role": "assistant", "content": text})
            snapshot = await self._commit_snapshot(tid, memory, props, snapshot, "cancelled")
            await publish_safe(self._bus, tid, "run_cancelled", {"incidentCode": props.incidentCode, "operatorId": operator_id, "workflowStatus": snapshot["workflowStatus"]})
            yield encoder.encode(StateSnapshotEvent(snapshot=snapshot))
            for chunk in _text_message(encoder, msg_id, text):
                yield chunk
            yield encoder.encode(_checkpoint_sync(props.incidentCode, memory, snapshot))
            yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))

    async def _cancel_remote_tasks(self, thread_id: str, incident_code: str) -> int:
        """이 대화에 남은 원격 A2A 작업에 CancelTask를 보내고, 중단을 확인하지 못한 작업 수를 반환한다."""
        remote_tasks = self._store.remote_tasks(thread_id, incident_code)
        unconfirmed = 0
        for task_id, (base_url, _) in list(remote_tasks.items()):
            try:
                await cancel_a2a_task(self._http, base_url=base_url, internal_token=self._settings.internal_service_token.get_secret_value(), task_id=task_id)
                confirmed = True
                remote_tasks.pop(task_id, None)
            except A2ATaskNotActiveError:
                confirmed = False
                remote_tasks.pop(task_id, None)
            except Exception:
                # 전달되지 못한 취소는 다음 취소 요청에서 다시 시도할 수 있게 남긴다
                confirmed = False
                unconfirmed += 1
            await publish_safe(self._bus, thread_id, "remote_task_cancel", {"taskId": task_id, "confirmed": confirmed})
        return unconfirmed

    async def _checkpoint_plan(self, thread_id: str) -> dict[str, Any] | None:
        """체크포인트에 보존된 계획 초안을 읽는다(취소 후에도 승인 대상 계획을 보여 주기 위함)."""
        if self._checkpointer is None or not hasattr(self._checkpointer, "aget_tuple"):
            return None
        try:
            saved = await self._checkpointer.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
        except Exception:
            return None
        values = (saved.checkpoint.get("channel_values") or {}) if saved is not None else {}
        draft = (values.get("remediation_result") or {}).get("planDraft")
        return {k: v for k, v in draft.items() if k != "approvalStepNo"} if isinstance(draft, dict) else None

    async def _handle_planning_stream(self, encoder: EventEncoder, run_input: RunAgentInput, props: ForwardedPropsInput, operator_id: str, memory: dict[str, Any], run: ActiveRun) -> AsyncIterator[str]:
        """LangGraph를 실시간 스트리밍으로 실행하거나 저장된 체크포인트를 이어서 실행한다."""
        tid, rid = run_input.thread_id, run_input.run_id
        user_text = _extract_last_user_text(run_input)
        history_msgs = list(memory.get("messages") or [])
        if user_text and (not props.resume or _last_user_content(history_msgs) != user_text):
            history_msgs.append({"id": f"msg-u-{uuid.uuid4().hex[:6]}", "role": "user", "content": user_text})

        loaded_cache, loaded_timestamps = _load_fresh_tool_cache(
            memory, self._settings.tool_cache_ttl_seconds, time.time()
        )
        trace_id = str(memory.get("traceId") or f"tr-{rid}")
        # 재개 요청은 원래 실행의 ID를 이어받아 계획·승인 식별자가 유지된다
        logical_run_id = (props.resumeRunId if props.resume else None) or rid
        init_state = {
            "session_id": tid,
            "run_id": logical_run_id,
            "trace_id": trace_id,
            "a2a_task_ids": list(memory.get("a2aTaskIds") or []),
            "incident_code": props.incidentCode,
            "service_name": self._settings.default_service_name,
            "user_message": user_text,
            "incident_started_at": props.incidentStartedAt,
            "constraints": dict(memory.get("constraints") or {}),
            "evidence": list(memory.get("evidence") or []),
            "pulled_chunk_ids": list(memory.get("pulledChunkIds") or []),
            "remaining_chunk_ids": list(memory.get("remainingChunkIds") or []),
            "runbook_summary": str(memory.get("runbookSummary") or ""),
            "recommended_command": memory.get("recommendedCommand"),
            "cached_tool_results": loaded_cache,
            "cached_tool_results_timestamps": loaded_timestamps,
            "replan_count": 0,
            "should_replan": False,
            "replan_history": list(memory.get("replanHistory") or []),
            "snapshot": memory.get("snapshot"),
            "ui_events": [],
        }
        ui_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        ledger_writes: list[asyncio.Future[Any]] = []
        live_graph = build_supervisor_graph(
            self._settings,
            self._http,
            self._backend,
            event_bus=self._bus,
            a2a_caller=self._a2a_caller,
            on_ui_event=ui_queue.put,
            checkpointer=self._checkpointer,
            ledger_writes=ledger_writes,
            remote_tasks=self._store.remote_tasks(tid, props.incidentCode),
        )
        graph_config = {"configurable": {"thread_id": tid}} if self._checkpointer is not None else None

        async def _run_graph() -> dict[str, Any]:
            with langsmith_tracing_scope(
                self._settings,
                client=self._langsmith_client,
                tags=["ag-ui", "supervisor", "resume" if props.resume else "run"],
                metadata={
                    "incident_code": props.incidentCode,
                    "thread_id": tid,
                    "run_id": rid,
                    "trace_id": trace_id,
                    "operator_id": operator_id,
                    "resume": props.resume,
                },
            ):
                async with traced_span(
                    "supervisor.ag_ui_run",
                    run_type="chain",
                    inputs={"incidentCode": props.incidentCode, "userMessage": user_text, "resume": props.resume},
                    tags=["ag-ui", "supervisor"],
                ) as span:
                    graph_input: dict[str, Any] | None = init_state
                    replay: dict[str, Any] | None = None
                    if props.resume and graph_config is not None:
                        saved_state = await live_graph.aget_state(graph_config)
                        saved_vals = dict(getattr(saved_state, "values", None) or {})
                        same_incident = saved_vals.get("incident_code") == props.incidentCode
                        # 서버에 닿지 못한 새 요청을 이전 실행의 결과로 답하지 않는다
                        identified = props.resumeRunId is not None and saved_vals.get("run_id") == props.resumeRunId
                        same_request = (props.resumeRunId is None or identified) and (
                            not user_text or saved_vals.get("user_message") == user_text
                        )
                        if getattr(saved_state, "next", None):
                            if same_incident and same_request:
                                graph_input = None
                            elif not same_incident and hasattr(self._checkpointer, "adelete_thread"):
                                # 다른 장애의 체크포인트가 섞이지 않도록 격리 삭제하고 새 장애 입력으로 시작한다
                                await self._checkpointer.adelete_thread(tid)
                        elif same_incident and same_request and identified and saved_vals.get("last_completed_step") == "respond":
                            # 끝난 실행의 재전송은 어떤 실행인지 명시한 요청에만 허용한다
                            # 이미 끝난 실행은 다시 돌리지 않고 저장된 결과를 재전송한다
                            replay = {**saved_vals, "_replayed": True}
                    if replay is not None:
                        result = replay
                    elif graph_config is not None:
                        result = await live_graph.ainvoke(graph_input, config=graph_config)
                    else:
                        result = await live_graph.ainvoke(init_state)
                    end_span(
                        span,
                        outputs={
                            "intent": result.get("intent"),
                            "workflowStatus": result.get("workflow_status"),
                            "replanCount": result.get("replan_count"),
                            "pulledChunkIds": result.get("pulled_chunk_ids"),
                            "resumed": graph_input is None or replay is not None,
                        },
                    )
                    return result

        graph_task = asyncio.create_task(_run_graph())
        # 시작 전에 취소되어도 대기 루프가 끝나도록 완료 콜백으로 종료 신호를 넣는다
        graph_task.add_done_callback(lambda _: ui_queue.put_nowait(None))
        run.attach(graph_task)
        final_state: dict[str, Any] | None = None
        try:
            while (ev := await ui_queue.get()) is not None:
                for encoded in _encode_ui_event(encoder, ev):
                    yield encoded
            try:
                final_state = await graph_task
            except asyncio.CancelledError:
                if not was_cancelled_by_request(run):
                    raise
        finally:
            run.detach()
            # 단절 시 취소가 await마다 다시 전달되므로 정리 구간을 보호한다
            with anyio.CancelScope(shield=True):
                # SSE 클라이언트가 중간에 연결을 끊으면 백그라운드 그래프 태스크를 즉시 정리한다
                if not graph_task.done():
                    # 이미 취소 중이면 다시 취소하지 않는다: 원격 작업 ID를 확보하는 대기가 중단된다
                    if not graph_task.cancelling():
                        graph_task.cancel()
                    await asyncio.wait([graph_task])
                    if not graph_task.cancelled():
                        graph_task.exception()
                # 시작한 원장 쓰기는 결과가 확정된 뒤에 스레드 락을 놓는다
                if ledger_writes:
                    await asyncio.wait(ledger_writes)

        if final_state is None:
            # 취소된 실행의 최종 상태는 취소 요청 스트림이 원장 기준으로 확정한다
            yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))
            return

        replayed = bool(final_state.get("_replayed"))
        if replayed and memory.get("snapshot") and memory.get("lastGraphRunId") == final_state.get("run_id"):
            # 저장까지 끝난 실행: 이후 승인 처리가 반영된 저장본을 원장과 대조해 다시 보낸다
            stored = memory["snapshot"]
            current = reconcile_snapshot(stored, await self._ledger_detail(props.incidentCode))
            snapshot = stored if current is stored else await self._commit_snapshot(tid, memory, props, current, "ledger_reconciled")
            yield encoder.encode(StateSnapshotEvent(snapshot=snapshot))
            yield encoder.encode(MessagesSnapshotEvent(messages=memory.get("messages") or []))
            yield encoder.encode(_checkpoint_sync(props.incidentCode, memory, snapshot, traceId=trace_id))
            yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))
            return

        snapshot = final_state.get("snapshot") or _idle_snapshot(props.incidentCode)
        if replayed:
            snapshot = reconcile_snapshot(snapshot, await self._ledger_detail(props.incidentCode))
        reply_text = str(final_state.get("reply_text") or "")
        msg_id = f"msg-a-{uuid.uuid4().hex[:6]}"
        history_msgs.append({"id": msg_id, "role": "assistant", "content": reply_text})
        new_cache = final_state.get("cached_tool_results") or {}
        state_timestamps = final_state.get("cached_tool_results_timestamps") or {}
        now_ts = time.time()
        next_timestamps = {
            key: (
                float(state_timestamps[key])
                if key in state_timestamps
                else (
                    loaded_timestamps[key]
                    if key in loaded_cache and loaded_cache[key] == val and key in loaded_timestamps
                    else now_ts
                )
            )
            for key, val in new_cache.items()
        }
        new_memory = {
            "threadId": tid,
            "traceId": trace_id,
            "a2aTaskIds": final_state.get("a2a_task_ids") or [],
            "lastEventSeq": memory.get("lastEventSeq"),
            "lastGraphRunId": final_state.get("run_id"),
            "messages": history_msgs[-20:],
            "constraints": final_state.get("constraints") or {},
            "evidence": final_state.get("evidence") or [],
            "pulledChunkIds": final_state.get("pulled_chunk_ids") or [],
            "remainingChunkIds": final_state.get("remaining_chunk_ids") or [],
            "runbookSummary": final_state.get("runbook_summary") or "",
            "recommendedCommand": final_state.get("recommended_command"),
            "cachedToolResults": new_cache,
            "cachedToolResultsTimestamps": next_timestamps,
            "replanHistory": final_state.get("replan_history") or [],
        }
        last_step = str(final_state.get("last_completed_step") or "respond")
        # 전송 중 단절에도 결과가 남도록 저장한 뒤에 보낸다
        snapshot = await self._commit_snapshot(tid, new_memory, props, snapshot, last_step)
        yield encoder.encode(StateSnapshotEvent(snapshot=snapshot))
        for chunk in _text_message(encoder, msg_id, reply_text):
            yield chunk
        yield encoder.encode(_checkpoint_sync(props.incidentCode, new_memory, snapshot, traceId=trace_id))
        yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))

    async def _handle_approval_stream(self, encoder: EventEncoder, tid: str, rid: str, props: ForwardedPropsInput, operator_id: str, decided: dict[str, Any], memory: dict[str, Any], run: ActiveRun) -> AsyncIterator[str]:
        """승인/반려 결정 이후 A2A 실행 및 Spring MCP 완료·포스트모템 반영 스트림을 처리한다."""
        decision = props.approvalDecision.decision  # type: ignore[union-attr]
        approval_id = props.approvalDecision.approvalId  # type: ignore[union-attr]
        prev_snap = dict(memory.get("snapshot") or {"incidentCode": props.incidentCode, "plan": None, "approval": None, "replanHistory": []})
        snap_plan = prev_snap.get("plan")
        target_plan_id = decided.get("planId") or (snap_plan or {}).get("planId")
        # 승인 대상 planId와 스냅샷 계획이 다르면 백엔드에서 해당 계획을 조회해 다른 계획의 근거가 섞이지 않게 막는다
        plan = snap_plan if isinstance(snap_plan, dict) and snap_plan.get("planId") == target_plan_id else await self._load_plan_from_backend(props.incidentCode, target_plan_id)
        prev_snap = {**prev_snap, "plan": plan}
        approval_obj = {"approvalId": approval_id, "planId": target_plan_id, "stepNo": decided.get("stepNo") or (prev_snap.get("approval") or {}).get("stepNo") or 2, "decision": decision}

        await publish_safe(self._bus, tid, "approval_decided", {"incidentCode": props.incidentCode, "approvalId": approval_id, "decision": decision, "approvedBy": operator_id})

        async def _finish(workflow_status: str, text: str, last_step: str, *, step_name: str | None = None) -> AsyncIterator[str]:
            msg_id = f"msg-a-{uuid.uuid4().hex[:6]}"
            memory.setdefault("messages", []).append({"id": msg_id, "role": "assistant", "content": text})
            snap = await self._commit_snapshot(tid, memory, props, {**prev_snap, "workflowStatus": workflow_status, "approval": approval_obj}, last_step)
            if step_name:
                yield encoder.encode(StepFinishedEvent(step_name=step_name))
            yield encoder.encode(StateSnapshotEvent(snapshot=snap))
            for chunk in _text_message(encoder, msg_id, text):
                yield chunk
            yield encoder.encode(_checkpoint_sync(props.incidentCode, memory, snap))
            yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))

        if decision == "REJECTED":
            text = f"승인 요청({approval_id})이 반려되었습니다. 조치를 실행하지 않았으며 필요 시 조건을 바꿔 계획을 다시 수립할 수 있습니다."
            async for chunk in _finish("REJECTED", text, "rejected"):
                yield chunk
            return

        # 락을 잡은 뒤 원장을 다시 본다: 실행 결과가 이미 있으면 워커 호출과 쓰기를 반복하지 않는다
        detail = await self._ledger_detail(props.incidentCode)
        if find_execution(detail, approval_id) is not None:
            recorded = "RESOLVED" if is_resolved(detail) else str(approval_status(detail, approval_id))
            text = f"승인({approval_id})의 실행 결과가 이미 원장에 기록되어 있어 다시 실행하지 않았습니다. 장애 상태: {recorded}"
            async for chunk in _finish(recorded, text, "ledger_reconciled"):
                yield chunk
            return

        # APPROVED: 실행 대기 상태를 먼저 저장해 워커 장애 시에도 재개 가능한 스냅샷을 유지한다
        waiting_snap = await self._commit_snapshot(tid, memory, props, {**prev_snap, "workflowStatus": "AWAITING_EXECUTION_RESULT", "approval": approval_obj}, "approved")
        yield encoder.encode(StateSnapshotEvent(snapshot=waiting_snap))
        yield encoder.encode(StepStartedEvent(step_name="execute_approved"))

        action_cmd = str(decided.get("actionCommand") or _find_plan_command(plan, approval_obj["stepNo"]) or f"kubectl rollout undo deployment/{self._settings.default_service_name}")
        exec_payload = {"action": "execute", "incidentCode": props.incidentCode, "approvalId": approval_id, "planId": str(approval_obj["planId"] or "PLAN-UNKNOWN"), "actionCommand": action_cmd, "verificationCriteria": str((plan or {}).get("verificationCriteria") or ""), "evidence": list((plan or {}).get("evidence") or [])}

        async def _call_executor() -> dict[str, Any]:
            if self._a2a_caller is not None:
                return await self._a2a_caller(self._settings.remediation_agent_a2a_url, exec_payload, tid)
            return await send_a2a_json(self._http, base_url=self._settings.remediation_agent_a2a_url, internal_token=self._settings.internal_service_token.get_secret_value(), payload=exec_payload, context_id=tid)

        async def _execute_and_complete() -> tuple[str, str | None, str]:
            with langsmith_tracing_scope(
                self._settings,
                client=self._langsmith_client,
                tags=["ag-ui", "supervisor", "approval-execution"],
                metadata={
                    "incident_code": props.incidentCode,
                    "thread_id": tid,
                    "run_id": rid,
                    "approval_id": approval_id,
                    "operator_id": operator_id,
                },
            ):
                async with traced_span(
                    "supervisor.execute_approved",
                    run_type="chain",
                    inputs={"incidentCode": props.incidentCode, "approvalId": approval_id, "planId": exec_payload["planId"]},
                    tags=["ag-ui", "approval-execution"],
                ) as exec_span:
                    # 워커 호출까지만 취소할 수 있다. 원장 완료 쓰기를 시작하면 끝까지 확인한다.
                    worker = asyncio.create_task(_call_executor())
                    run.attach(worker)
                    try:
                        exec_res = await worker
                    except asyncio.CancelledError:
                        if not was_cancelled_by_request(run):
                            raise
                        end_span(exec_span, error="execution_cancelled")
                        return ("CANCELLED", None, "")
                    except Exception:
                        end_span(exec_span, error="execution_interrupted")
                        return ("INTERRUPTED", None, "")
                    finally:
                        run.detach()
                    if run.cancel_requested:
                        end_span(exec_span, error="execution_cancelled")
                        return ("CANCELLED", None, "")

                    completion = asyncio.ensure_future(asyncio.wait_for(
                        self._backend.complete_incident_with_postmortem(
                            approval_id=approval_id,
                            incident_code=props.incidentCode,
                            action_command=action_cmd,
                            executed_by="remediation-agent",
                            execution_status=exec_res.get("executionStatus", "SUCCESS"),
                            execution_output=exec_res.get("executionOutput"),
                            verification_summary=exec_res.get("verificationSummary"),
                            root_cause=exec_res.get("rootCause"),
                            timeline_markdown=exec_res.get("timelineMarkdown"),
                            prevention_items=exec_res.get("preventionItems"),
                        ),
                        timeout=self._settings.http_timeout_seconds * 2,
                    ))
                    try:
                        # 단절돼도 시작한 완료 쓰기가 끝난 뒤에 락을 놓는다
                        await settle(completion)
                        status_out = "RESOLVED" if exec_res.get("executionStatus", "SUCCESS") == "SUCCESS" else "EXECUTION_FAILED"
                        text_out = f"승인({approval_id})된 조치(`{action_cmd}`)를 실행하고 포스트모템을 반영했습니다. 장애 상태: {status_out}"
                        end_span(exec_span, outputs={"finalStatus": status_out, "executionStatus": exec_res.get("executionStatus")})
                        return ("DONE", status_out, text_out)
                    except Exception as exc:
                        # 응답을 받지 못했어도 커밋됐을 수 있으므로 원장으로 확정한다
                        after = await self._ledger_detail(props.incidentCode)
                        if find_execution(after, approval_id) is not None:
                            status_out = "RESOLVED" if is_resolved(after) else str(approval_status(after, approval_id))
                            end_span(exec_span, outputs={"finalStatus": status_out, "recoveredFromLedger": True})
                            return ("DONE", status_out, f"완료 응답은 받지 못했지만 원장에서 승인({approval_id})의 실행 결과를 확인했습니다. 장애 상태: {status_out}")
                        # 원장에 실행 기록이 없으면 승인은 여전히 실행 대기다(거절 사유만 전달한다)
                        rejection = exc.message if isinstance(exc, BackendApiError) and exc.status_code < 500 else ""
                        end_span(exec_span, error=rejection or "completion_unconfirmed")
                        return ("INTERRUPTED", None, rejection)

        outcome, maybe_status, reply_text = await _execute_and_complete()
        if outcome == "CANCELLED":
            yield encoder.encode(StepFinishedEvent(step_name="execute_approved"))
            yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))
            return
        if outcome == "INTERRUPTED":
            await publish_safe(self._bus, tid, "execution_interrupted", {"incidentCode": props.incidentCode, "approvalId": approval_id})
            reason = f" 반영이 거절됐습니다: {reply_text}" if reply_text else ""
            retry_text = f"승인({approval_id})은 기록되었으나 실행 결과를 확정하지 못했습니다.{reason} 같은 승인 건으로 다시 요청하면 원장을 확인한 뒤 이어서 처리합니다."
            async for chunk in _finish("AWAITING_EXECUTION_RESULT", retry_text, "execution_interrupted", step_name="execute_approved"):
                yield chunk
            return

        final_status = maybe_status or "EXECUTION_FAILED"
        await publish_safe(self._bus, tid, "execution_completed", {"incidentCode": props.incidentCode, "approvalId": approval_id, "finalStatus": final_status})
        async for chunk in _finish(final_status, reply_text, "executed", step_name="execute_approved"):
            yield chunk


def _idle_snapshot(incident_code: str, replan_history: Any = None) -> dict[str, Any]:
    return {"incidentCode": incident_code, "workflowStatus": "IDLE", "plan": None, "approval": None, "replanHistory": list(replan_history or [])}


def _last_user_content(messages: list[dict[str, Any]]) -> str | None:
    return next((str(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), None)


def _text_message(encoder: EventEncoder, message_id: str, text: str) -> list[str]:
    return [
        encoder.encode(TextMessageStartEvent(message_id=message_id, role="assistant")),
        encoder.encode(TextMessageContentEvent(message_id=message_id, delta=text)),
        encoder.encode(TextMessageEndEvent(message_id=message_id)),
    ]


def _checkpoint_sync(incident_code: str, memory: dict[str, Any], snapshot: dict[str, Any], **extra: Any) -> CustomEvent:
    return CustomEvent(
        name="checkpoint_sync",
        value={
            "incidentCode": incident_code,
            "eventSeq": int(snapshot.get("eventSeq") or memory.get("lastEventSeq") or 0),
            "lastCompletedStep": memory.get("lastCompletedStep"),
            "workflowStatus": snapshot.get("workflowStatus"),
            **extra,
        },
    )


def _snapshot_after_cancel(incident_code: str, previous: dict[str, Any] | None, detail: dict[str, Any], draft_plan: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
    """취소 이후의 화면 상태를 원장에 남은 사실로 결정한다."""
    prev = dict(previous or _idle_snapshot(incident_code))
    if is_resolved(detail):
        return {**prev, "workflowStatus": "RESOLVED"}, "이미 해결된 장애라 취소할 작업이 없습니다."

    approval = find_open_approval(detail)
    if approval is None:
        failed = _failed_approval(detail, prev)
        if failed is not None:
            plan_id = failed.get("planId")
            plan = next((p for p in (prev.get("plan"), draft_plan, find_plan(detail, plan_id)) if isinstance(p, dict) and p.get("planId") == plan_id), None)
            return (
                {**prev, "workflowStatus": "EXECUTION_FAILED", "plan": plan, "approval": approval_view(failed)},
                f"취소 전에 승인({failed.get('approvalId')})의 실행이 실패로 원장에 기록됐습니다.",
            )
        return (
            {**prev, "workflowStatus": "CANCELLED", "approval": None},
            "실행 중인 작업을 취소했습니다. 원장에 남은 승인 요청은 없습니다.",
        )

    approval_id = str(approval.get("approvalId"))
    plan_id = approval.get("planId")
    candidates = (prev.get("plan"), draft_plan, find_plan(detail, plan_id))
    plan = next((p for p in candidates if isinstance(p, dict) and p.get("planId") == plan_id), None)
    workflow_status = str(approval_status(detail, approval_id))
    if workflow_status not in OPEN_APPROVAL_STATUSES:
        workflow_status = "CANCELLED"
    text = (
        f"실행 중인 작업은 중단했습니다. 다만 승인 요청({approval_id})은 이미 원장에 등록되어 있습니다. 계획을 폐기하려면 반려해 주세요."
        if workflow_status == "AWAITING_APPROVAL"
        else f"실행 중인 작업은 중단했습니다. 승인({approval_id})은 기록되어 있고 실행 결과는 없습니다. 같은 승인으로 다시 실행할 수 있습니다."
    )
    return {**prev, "workflowStatus": workflow_status, "plan": plan, "approval": approval_view(approval)}, text


def _failed_approval(detail: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any] | None:
    """화면이 보던 승인, 없으면 원장의 최신 승인이 실패한 실행으로 끝났는지 확인한다."""
    shown_id = (previous.get("approval") or {}).get("approvalId")
    if shown_id:
        candidate = find_approval(detail, shown_id)
    else:
        candidate = latest_approval(detail)
    if candidate is None or approval_status(detail, str(candidate.get("approvalId"))) != "EXECUTION_FAILED":
        return None
    return candidate


def _find_plan_command(plan: dict[str, Any] | None, step_no: int) -> str | None:
    """스냅샷 계획에서 승인 단계 번호에 해당하는 actionCommand를 찾는다."""
    if not isinstance(plan, dict):
        return None
    for step in plan.get("steps") or []:
        if step.get("stepNo") == step_no and step.get("actionCommand"):
            return str(step["actionCommand"])
    return None


def _encode_ui_event(encoder: EventEncoder, ev: dict[str, Any]) -> list[str]:
    """LangGraph 내부 이벤트 레코드를 AG-UI 표준 SSE 프레임 목록으로 변환한다."""
    kind = ev.get("kind")
    if kind == "step_start":
        return [encoder.encode(StepStartedEvent(step_name=str(ev["step"])))]
    if kind == "step_finish":
        return [encoder.encode(StepFinishedEvent(step_name=str(ev["step"])))]
    if kind == "a2a_progress":
        return [encoder.encode(CustomEvent(name="a2a_progress", value=ev.get("value")))]
    if kind == "replan":
        return [encoder.encode(CustomEvent(name="replan", value=ev.get("value")))]
    if kind == "tool_call":
        tc_id = str(ev["id"])
        return [
            encoder.encode(ToolCallStartEvent(tool_call_id=tc_id, tool_call_name=str(ev["name"]))),
            encoder.encode(ToolCallArgsEvent(tool_call_id=tc_id, delta=json.dumps(ev.get("args", {}), ensure_ascii=False))),
            encoder.encode(ToolCallEndEvent(tool_call_id=tc_id)),
            encoder.encode(ToolCallResultEvent(message_id=f"msg-tc-{tc_id}", tool_call_id=tc_id, content=json.dumps(ev.get("result", {}), ensure_ascii=False))),
        ]
    return []
