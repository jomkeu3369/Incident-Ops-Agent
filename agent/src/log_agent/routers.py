from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import time
import uuid
from fastapi import APIRouter, Depends, FastAPI
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Artifact, Part, Task, TaskArtifactUpdateEvent, TaskState, TaskStatus, TaskStatusUpdateEvent
from pydantic import ValidationError

from common.a2a_utils import make_agent_json_message
from common.backend_client import BackendClient
from common.config import Settings
from common.di import get_backend_client, get_settings
from common.security import verify_internal_token
from log_agent.schemas import LogInvestigationRequest, LogInvestigationResponse
from log_agent.services import run_dr_dci_investigation

router = APIRouter(prefix="/internal", tags=["a2a-internal"], dependencies=[Depends(verify_internal_token)])
_CACHE_TTL = 600
_CACHE_SIZE = 512
_RequestKey = tuple[str, str, str]


def _request_key(req: LogInvestigationRequest) -> _RequestKey | None:
    idem_key = (req.idempotencyKey or "").strip()
    if not idem_key:
        return None
    fields = req.model_dump(exclude={"incidentCode", "idempotencyKey", "traceId"})
    fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return idem_key, req.incidentCode, fingerprint


def _prune_cache(cache: OrderedDict) -> None:
    now = time.monotonic()
    while cache and (len(cache) > _CACHE_SIZE or next(iter(cache.values()))[0] <= now):
        cache.popitem(last=False)


def _remember(cache: OrderedDict, key, value) -> None:
    cache[key] = (time.monotonic() + _CACHE_TTL, value)
    cache.move_to_end(key)
    _prune_cache(cache)


@dataclass
class _Investigation:
    task: asyncio.Task[LogInvestigationResponse]
    pulling: asyncio.Event
    waiters: set[str] = field(default_factory=set)


def _investigator_backend(app: FastAPI) -> BackendClient:
    """조사 워커(log_agent)는 항상 읽기 전용 investigator 역할의 BackendClient만 사용한다."""
    container = app.state.container
    if container.backend_client is not None:
        if getattr(container.backend_client, "agent_role", None) == "investigator":
            return container.backend_client
        # 테스트에서 주입된 mcp_caller를 유지하면서 investigator 읽기 전용 경계를 적용한다
        return BackendClient(
            container.settings,
            container.http_client,
            mcp_caller=getattr(container.backend_client, "_mcp_caller", None),
            mcp_resource_reader=getattr(container.backend_client, "_mcp_resource_reader", None),
            agent_role="investigator",
        )
    return BackendClient(container.settings, container.http_client, agent_role="investigator")


@router.get("/ping")
async def internal_ping() -> dict[str, str]:
    """내부 토큰 인증 확인용 엔드포인트."""
    return {"service": "log-analyzer-agent", "protocol": "a2a", "auth": "verified"}


@router.post("/investigate", response_model=LogInvestigationResponse)
async def investigate_logs(body: LogInvestigationRequest, backend: BackendClient = Depends(get_backend_client), cfg: Settings = Depends(get_settings)) -> LogInvestigationResponse:
    """DR-DCI 로그 조사를 직접 실행하는 내부 DI 엔드포인트."""
    ro_backend = (
        backend
        if getattr(backend, "agent_role", None) == "investigator"
        else BackendClient(
            cfg,
            getattr(backend, "_http", None),
            mcp_caller=getattr(backend, "_mcp_caller", None),
            mcp_resource_reader=getattr(backend, "_mcp_resource_reader", None),
            agent_role="investigator",
        )
    )
    return await run_dr_dci_investigation(ro_backend, cfg, body)


class LogAgentExecutor(AgentExecutor):
    """A2A JSON-RPC(동기 SendMessage · 스트리밍 SendStreamingMessage · 취소 CancelTask) 로그 조사 실행기."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._idempotency_cache: OrderedDict[_RequestKey, tuple[float, LogInvestigationResponse]] = OrderedDict()
        self._inflight_by_key: dict[_RequestKey, _Investigation] = {}
        self._task_waiters: dict[str, asyncio.Task[LogInvestigationResponse]] = {}
        self._canceled_task_ids: OrderedDict[str, tuple[float, None]] = OrderedDict()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """사용자 메시지 JSON을 파싱해 표준 A2A 진행 상태·산출물 이벤트를 발행한다."""
        raw = context.get_user_input()
        task_id = str(getattr(context, "task_id", None) or uuid.uuid4())
        ctx_id = str(getattr(context, "context_id", None) or "")

        try:
            req = LogInvestigationRequest.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError):
            err = LogInvestigationResponse(ok=False, incidentCode="", error={"code": "INVALID_INPUT", "message": "로그 조사 요청 JSON 형식이 올바르지 않습니다."})
            await event_queue.enqueue_event(make_agent_json_message(err.model_dump(), context_id=ctx_id))
            return

        # A2A v1.0 규격: TaskStatusUpdateEvent/TaskArtifactUpdateEvent 발행 전 초기 Task 객체를 먼저 등록한다
        if getattr(context, "current_task", None) is None:
            await event_queue.enqueue_event(
                Task(
                    id=task_id,
                    context_id=ctx_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                )
            )

        async def progress(stage: str) -> None:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=ctx_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_WORKING,
                        message=make_agent_json_message(
                            {"stage": stage, "incidentCode": req.incidentCode, "taskId": task_id},
                            context_id=ctx_id,
                        ),
                    ),
                )
            )
        key = _request_key(req)
        _prune_cache(self._idempotency_cache)
        _prune_cache(self._canceled_task_ids)
        cached = self._idempotency_cache.get(key) if key is not None else None
        if cached is not None:
            await progress("deduplicated_hit")
            result = cached[1]
        else:
            await progress("searching_index")
            if task_id in self._canceled_task_ids:
                return
            work = self._inflight_by_key.get(key) if key is not None else None
            if work is None:
                pulling = asyncio.Event()

                async def on_pull_start() -> None:
                    pulling.set()

                cfg = self._app.state.container.settings
                task = asyncio.create_task(
                    run_dr_dci_investigation(_investigator_backend(self._app), cfg, req, on_pull_start=on_pull_start)
                )
                work = _Investigation(task, pulling)
                if key is not None:
                    self._inflight_by_key[key] = work
            work.waiters.add(task_id)
            shared = work

            async def wait_for_result() -> LogInvestigationResponse:
                pull_signal = asyncio.create_task(shared.pulling.wait())
                try:
                    await asyncio.wait((shared.task, pull_signal), return_when=asyncio.FIRST_COMPLETED)
                    if shared.pulling.is_set():
                        await progress("pulling_chunks")
                    return await asyncio.shield(shared.task)
                finally:
                    pull_signal.cancel()

            waiter = asyncio.create_task(wait_for_result())
            self._task_waiters[task_id] = waiter
            try:
                result = await waiter
                if (
                    key is not None and task_id not in self._canceled_task_ids
                    and result.ok and result.error is None and result.incidentCode == req.incidentCode
                    and result.stopReason not in {"TIMEOUT_BUDGET_REACHED", "GUARD_REJECTED_INVALID_CHUNK", "BACKEND_ERROR", "STOP_INSUFFICIENT"}
                    and all(tool.get("ok") is not False and not tool.get("error") for tool in result.toolTrace)
                ):
                    _remember(self._idempotency_cache, key, result)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if task_id in self._canceled_task_ids or (current is not None and current.cancelling()):
                    raise
                # 요청하지 않은 취소가 새면 작업이 WORKING에 남으므로 실패로 기록되게 한다
                raise RuntimeError("조사 작업이 예기치 않게 중단됐습니다.") from None
            finally:
                self._task_waiters.pop(task_id, None)
                work.waiters.discard(task_id)
                if not work.waiters:
                    if key is not None:
                        self._inflight_by_key.pop(key, None)
                    if not work.task.done():
                        work.task.cancel()
                    await asyncio.gather(work.task, return_exceptions=True)

        if task_id in self._canceled_task_ids:
            return

        payload = result.model_dump()

        # 4) 최종 산출물(Artifact) 발행 후 완료 상태 전이
        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=task_id,
                context_id=ctx_id,
                artifact=Artifact(
                    artifact_id=f"art-{task_id}",
                    name="dr_dci_investigation",
                    parts=[Part(text=json.dumps(payload, ensure_ascii=False))],
                ),
                last_chunk=True,
            )
        )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=ctx_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_COMPLETED if result.ok else TaskState.TASK_STATE_FAILED,
                    message=make_agent_json_message(
                        {
                            "stage": "completed" if result.ok else "failed",
                            "incidentCode": req.incidentCode,
                            "taskId": task_id,
                            "pulledCount": result.pulledCount,
                            "deduplicated": cached is not None,
                        },
                        context_id=ctx_id,
                    ),
                ),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """진행 중인 A2A 조사 요청만 격리 취소하며, 공유 작업은 마지막 대기자가 취소될 때만 중단한다."""
        task_id = str(getattr(context, "task_id", None) or "")
        ctx_id = str(getattr(context, "context_id", None) or "")
        if task_id:
            _remember(self._canceled_task_ids, task_id, None)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id or str(uuid.uuid4()),
                context_id=ctx_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_CANCELED,
                    message=make_agent_json_message({"stage": "canceled", "taskId": task_id}, context_id=ctx_id),
                ),
            )
        )
        waiter = self._task_waiters.get(task_id)
        if waiter is not None and not waiter.done():
            waiter.cancel()
