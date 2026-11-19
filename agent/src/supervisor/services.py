from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncIterator
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

from common.a2a_utils import send_a2a_json
from common.backend_client import BackendApiError, BackendClient
from common.config import Settings
from common.event_log import EventBus, publish_safe
from supervisor.cruds import SupervisorRedisStore
from supervisor.graph import A2ACaller, build_supervisor_graph
from supervisor.schemas import ForwardedPropsInput


def _extract_last_user_text(run_input: RunAgentInput) -> str:
    """RunAgentInput.messages에서 마지막 사용자 메시지 텍스트를 추출한다."""
    for msg in reversed(run_input.messages or []):
        if getattr(msg, "role", None) == "user":
            content = getattr(msg, "content", "")
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return ""


class SupervisorService:
    """AG-UI 스트리밍·HITL 승인·LangGraph 5단계 오케스트레이션을 총괄하는 서비스."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient, backend: BackendClient, store: SupervisorRedisStore, event_bus: EventBus | None = None, a2a_caller: A2ACaller | None = None) -> None:
        self._settings = settings
        self._http = http_client
        self._backend = backend
        self._store = store
        self._bus = event_bus
        self._a2a_caller = a2a_caller

    def parse_forwarded_props(self, run_input: RunAgentInput) -> ForwardedPropsInput:
        """스트림을 열기 전에 forwardedProps 계약을 검증한다(위반 시 HTTP 400)."""
        if not isinstance(run_input.forwarded_props, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="forwardedProps.incidentCode는 필수입니다.")
        try:
            return ForwardedPropsInput.model_validate(run_input.forwarded_props)
        except ValidationError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="forwardedProps 형식이 올바르지 않습니다.")

    async def prepare_approval_decision(self, props: ForwardedPropsInput, operator_id: str) -> dict[str, Any] | None:
        """승인·반려 요청이면 스트림 오픈 전에 Spring 원장에 결정을 반영하며, 중단된 미완료 APPROVED 건은 재개를 허용한다."""
        if props.approvalDecision is None:
            return None
        try:
            return await self._backend.decide_approval(
                props.incidentCode,
                props.approvalDecision.approvalId,
                approved_by=operator_id,
                decision=props.approvalDecision.decision,
            )
        except BackendApiError as exc:
            # 이미 APPROVED 상태지만 워커 장애 등으로 아직 실행·완료가 기록되지 않은 건만 재개를 허용한다
            if exc.status_code == 409 and props.approvalDecision.decision == "APPROVED":
                resumable = await self._find_resumable_approval(props.incidentCode, props.approvalDecision.approvalId)
                if resumable is not None:
                    return resumable
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    async def _find_resumable_approval(self, incident_code: str, approval_id: str) -> dict[str, Any] | None:
        """백엔드에서 APPROVED 기록은 있으나 실행 결과가 아직 없는 미완료 승인 건을 조회한다."""
        try:
            detail = await self._backend.get_incident_detail(incident_code)
        except BackendApiError:
            return None
        incident = detail.get("incident") if isinstance(detail, dict) else None
        if not isinstance(incident, dict) or incident.get("status") == "RESOLVED":
            return None
        approvals = detail.get("approvals") if isinstance(detail, dict) else None
        matched = next((a for a in (approvals or []) if isinstance(a, dict) and a.get("approvalId") == approval_id and a.get("decision") == "APPROVED"), None)
        if matched is None:
            return None
        executions = detail.get("executions") if isinstance(detail, dict) else None
        if any(isinstance(e, dict) and e.get("approvalId") == approval_id for e in (executions or [])):
            return None
        return matched

    async def stream_ag_ui(self, run_input: RunAgentInput, props: ForwardedPropsInput, operator_id: str, decided_approval: dict[str, Any] | None, accept_header: str | None) -> AsyncIterator[str]:
        """AG-UI 표준 SSE 이벤트 스트림을 생성한다."""
        encoder = EventEncoder(accept=accept_header)
        tid, rid = run_input.thread_id, run_input.run_id
        memory = await self._store.get_thread_memory(tid)

        # 스레드에 저장된 장애 코드가 현재 요청과 다르면 이전 장애의 근거·스냅샷 상속을 차단한다
        bound_incident = memory.get("incidentCode") or (memory.get("snapshot") or {}).get("incidentCode")
        if bound_incident and bound_incident != props.incidentCode:
            memory = {"threadId": tid, "incidentCode": props.incidentCode, "messages": [], "constraints": {}, "snapshot": None, "replanHistory": [], "pulledChunkIds": [], "cachedToolResults": {}}

        try:
            yield encoder.encode(RunStartedEvent(thread_id=tid, run_id=rid))

            # 1) 새로고침 복원 흐름 (restore=True)
            if props.restore:
                snap = memory.get("snapshot") or {"incidentCode": props.incidentCode, "workflowStatus": "IDLE", "plan": None, "approval": None, "replanHistory": memory.get("replanHistory") or []}
                yield encoder.encode(StateSnapshotEvent(snapshot=snap))
                yield encoder.encode(MessagesSnapshotEvent(messages=memory.get("messages") or []))
                yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))
                return

            # 2) 운영자 승인/반려 처리 흐름
            if props.approvalDecision is not None and decided_approval is not None:
                async for chunk in self._handle_approval_stream(encoder, tid, rid, props, operator_id, decided_approval, memory):
                    yield chunk
                return

            # 3) 일반 계획 요청 / 대화 흐름 (LangGraph 5단계 실시간 스트리밍 실행)
            user_text = _extract_last_user_text(run_input)
            history_msgs = list(memory.get("messages") or [])
            if user_text:
                history_msgs.append({"id": f"msg-u-{uuid.uuid4().hex[:6]}", "role": "user", "content": user_text})

            init_state = {
                "session_id": tid,
                "run_id": rid,
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
                "cached_tool_results": dict(memory.get("cachedToolResults") or {}),
                "replan_count": 0,
                "should_replan": False,
                "replan_history": list(memory.get("replanHistory") or []),
                "snapshot": memory.get("snapshot"),
                "ui_events": [],
            }
            ui_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
            live_graph = build_supervisor_graph(self._settings, self._http, self._backend, event_bus=self._bus, a2a_caller=self._a2a_caller, on_ui_event=ui_queue.put)

            async def _run_graph() -> dict[str, Any]:
                try:
                    return await live_graph.ainvoke(init_state)
                finally:
                    await ui_queue.put(None)

            graph_task = asyncio.create_task(_run_graph())
            while (ev := await ui_queue.get()) is not None:
                for encoded in _encode_ui_event(encoder, ev):
                    yield encoded
            final_state = await graph_task

            snapshot = final_state.get("snapshot") or {"incidentCode": props.incidentCode, "workflowStatus": "IDLE", "plan": None, "approval": None, "replanHistory": []}
            yield encoder.encode(StateSnapshotEvent(snapshot=snapshot))

            reply_text = str(final_state.get("reply_text") or "")
            msg_id = f"msg-a-{uuid.uuid4().hex[:6]}"
            yield encoder.encode(TextMessageStartEvent(message_id=msg_id, role="assistant"))
            yield encoder.encode(TextMessageContentEvent(message_id=msg_id, delta=reply_text))
            yield encoder.encode(TextMessageEndEvent(message_id=msg_id))

            history_msgs.append({"id": msg_id, "role": "assistant", "content": reply_text})
            await self._store.save_thread_memory(
                tid,
                {
                    "threadId": tid,
                    "incidentCode": props.incidentCode,
                    "messages": history_msgs[-20:],
                    "constraints": final_state.get("constraints") or {},
                    "evidence": final_state.get("evidence") or [],
                    "pulledChunkIds": final_state.get("pulled_chunk_ids") or [],
                    "remainingChunkIds": final_state.get("remaining_chunk_ids") or [],
                    "runbookSummary": final_state.get("runbook_summary") or "",
                    "recommendedCommand": final_state.get("recommended_command"),
                    "cachedToolResults": final_state.get("cached_tool_results") or {},
                    "snapshot": snapshot,
                    "replanHistory": final_state.get("replan_history") or [],
                },
            )
            yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))
        except Exception:
            yield encoder.encode(RunErrorEvent(message="에이전트 실행 중 오류가 발생했습니다.", code="AGENT_EXECUTION_ERROR"))

    async def _handle_approval_stream(self, encoder: EventEncoder, tid: str, rid: str, props: ForwardedPropsInput, operator_id: str, decided: dict[str, Any], memory: dict[str, Any]) -> AsyncIterator[str]:
        """승인/반려 결정 이후 A2A 실행 및 Spring MCP 완료·포스트모템 반영 스트림을 처리한다."""
        decision = props.approvalDecision.decision  # type: ignore[union-attr]
        approval_id = props.approvalDecision.approvalId  # type: ignore[union-attr]
        prev_snap = dict(memory.get("snapshot") or {"incidentCode": props.incidentCode, "plan": None, "approval": None, "replanHistory": []})
        plan = prev_snap.get("plan")
        approval_obj = {"approvalId": approval_id, "planId": decided.get("planId") or (plan or {}).get("planId"), "stepNo": decided.get("stepNo") or (prev_snap.get("approval") or {}).get("stepNo") or 2, "decision": decision}

        await publish_safe(self._bus, tid, "approval_decided", {"incidentCode": props.incidentCode, "approvalId": approval_id, "decision": decision, "approvedBy": operator_id})

        if decision == "REJECTED":
            snap = {**prev_snap, "workflowStatus": "REJECTED", "approval": approval_obj}
            yield encoder.encode(StateSnapshotEvent(snapshot=snap))
            msg_id = f"msg-a-{uuid.uuid4().hex[:6]}"
            text = f"승인 요청({approval_id})이 반려되었습니다. 조치를 실행하지 않았으며 필요 시 조건을 바꿔 계획을 다시 수립할 수 있습니다."
            yield encoder.encode(TextMessageStartEvent(message_id=msg_id, role="assistant"))
            yield encoder.encode(TextMessageContentEvent(message_id=msg_id, delta=text))
            yield encoder.encode(TextMessageEndEvent(message_id=msg_id))
            memory["incidentCode"] = props.incidentCode
            memory["snapshot"] = snap
            memory.setdefault("messages", []).append({"id": msg_id, "role": "assistant", "content": text})
            await self._store.save_thread_memory(tid, memory)
            yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))
            return

        # APPROVED: 실행 대기 상태를 먼저 저장해 워커 장애 시에도 재개 가능한 스냅샷을 유지한다
        waiting_snap = {**prev_snap, "workflowStatus": "AWAITING_EXECUTION_RESULT", "approval": approval_obj}
        memory["incidentCode"] = props.incidentCode
        memory["snapshot"] = waiting_snap
        await self._store.save_thread_memory(tid, memory)
        yield encoder.encode(StateSnapshotEvent(snapshot=waiting_snap))
        yield encoder.encode(StepStartedEvent(step_name="execute_approved"))

        action_cmd = str(decided.get("actionCommand") or _find_plan_command(plan, approval_obj["stepNo"]) or f"kubectl rollout undo deployment/{self._settings.default_service_name}")
        exec_payload = {"action": "execute", "incidentCode": props.incidentCode, "approvalId": approval_id, "planId": str(approval_obj["planId"] or "PLAN-UNKNOWN"), "actionCommand": action_cmd, "verificationCriteria": str((plan or {}).get("verificationCriteria") or ""), "evidence": list((plan or {}).get("evidence") or [])}

        try:
            if self._a2a_caller is not None:
                exec_res = await self._a2a_caller(self._settings.remediation_agent_a2a_url, exec_payload, tid)
            else:
                exec_res = await send_a2a_json(self._http, base_url=self._settings.remediation_agent_a2a_url, internal_token=self._settings.internal_service_token.get_secret_value(), payload=exec_payload, context_id=tid)
        except Exception:
            yield encoder.encode(StepFinishedEvent(step_name="execute_approved"))
            await publish_safe(self._bus, tid, "execution_interrupted", {"incidentCode": props.incidentCode, "approvalId": approval_id})
            msg_id = f"msg-a-{uuid.uuid4().hex[:6]}"
            retry_text = f"승인({approval_id})은 기록되었으나 실행 워커 호출이 일시 중단되었습니다. 같은 승인 건으로 다시 요청하면 실행을 이어서 완료할 수 있습니다."
            yield encoder.encode(TextMessageStartEvent(message_id=msg_id, role="assistant"))
            yield encoder.encode(TextMessageContentEvent(message_id=msg_id, delta=retry_text))
            yield encoder.encode(TextMessageEndEvent(message_id=msg_id))
            memory.setdefault("messages", []).append({"id": msg_id, "role": "assistant", "content": retry_text})
            await self._store.save_thread_memory(tid, memory)
            yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))
            return

        try:
            await self._backend.complete_incident_with_postmortem(
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
            )
            final_status = "RESOLVED" if exec_res.get("executionStatus") == "SUCCESS" else "EXECUTION_FAILED"
            reply_text = f"승인({approval_id})된 조치(`{action_cmd}`)를 실행하고 포스트모템을 반영했습니다. 장애 상태: {final_status}"
        except BackendApiError as exc:
            final_status = "EXECUTION_FAILED"
            reply_text = f"실행 결과 반영 중 백엔드 오류가 발생했습니다: {exc.message}"

        yield encoder.encode(StepFinishedEvent(step_name="execute_approved"))
        await publish_safe(self._bus, tid, "execution_completed", {"incidentCode": props.incidentCode, "approvalId": approval_id, "finalStatus": final_status})
        final_snap = {**prev_snap, "workflowStatus": final_status, "approval": approval_obj}
        yield encoder.encode(StateSnapshotEvent(snapshot=final_snap))

        msg_id = f"msg-a-{uuid.uuid4().hex[:6]}"
        yield encoder.encode(TextMessageStartEvent(message_id=msg_id, role="assistant"))
        yield encoder.encode(TextMessageContentEvent(message_id=msg_id, delta=reply_text))
        yield encoder.encode(TextMessageEndEvent(message_id=msg_id))
        memory["incidentCode"] = props.incidentCode
        memory["snapshot"] = final_snap
        memory.setdefault("messages", []).append({"id": msg_id, "role": "assistant", "content": reply_text})
        await self._store.save_thread_memory(tid, memory)
        yield encoder.encode(RunFinishedEvent(thread_id=tid, run_id=rid))


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
