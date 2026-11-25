from __future__ import annotations

import asyncio
import json
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
        self._idempotency_cache: dict[str, dict] = {}
        self._inflight_by_key: dict[str, asyncio.Task[LogInvestigationResponse]] = {}
        self._inflight_waiters: dict[str, set[str]] = {}
        self._task_waiters: dict[str, asyncio.Task[LogInvestigationResponse]] = {}
        self._task_to_idem_key: dict[str, str] = {}
        self._canceled_task_ids: set[str] = set()

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

        # 1) 멱등 중복 요청 확인 (동일 idempotencyKey 재전송 시 워커 재실행 0건)
        idem_key = (req.idempotencyKey or "").strip()
        if idem_key and idem_key in self._idempotency_cache:
            cached_payload = dict(self._idempotency_cache[idem_key])
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=ctx_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_WORKING,
                        message=make_agent_json_message(
                            {"stage": "deduplicated_hit", "incidentCode": req.incidentCode, "taskId": task_id, "idempotencyKey": idem_key},
                            context_id=ctx_id,
                        ),
                    ),
                )
            )
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=task_id,
                    context_id=ctx_id,
                    artifact=Artifact(
                        artifact_id=f"art-{task_id}",
                        name="dr_dci_investigation",
                        parts=[Part(text=json.dumps(cached_payload, ensure_ascii=False))],
                    ),
                    last_chunk=True,
                )
            )
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=ctx_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_COMPLETED,
                        message=make_agent_json_message(
                            {"stage": "completed", "incidentCode": req.incidentCode, "taskId": task_id, "deduplicated": True},
                            context_id=ctx_id,
                        ),
                    ),
                )
            )
            return

        # 2) 진행 상태 1: 색인 조회 시작
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=ctx_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=make_agent_json_message(
                        {"stage": "searching_index", "incidentCode": req.incidentCode, "taskId": task_id, "traceId": req.traceId},
                        context_id=ctx_id,
                    ),
                ),
            )
        )

        # 3) 진행 상태 2: 청크 선별 인출
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=ctx_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=make_agent_json_message(
                        {"stage": "pulling_chunks", "incidentCode": req.incidentCode, "taskId": task_id, "traceId": req.traceId},
                        context_id=ctx_id,
                    ),
                ),
            )
        )

        container = self._app.state.container
        backend = _investigator_backend(self._app)
        # 동일 idempotencyKey의 동시 진행 중(in-flight) 요청은 작업을 공유하되, 취소는 요청별 waiter로 격리한다
        if idem_key and idem_key in self._inflight_by_key:
            work_task = self._inflight_by_key[idem_key]
        else:
            work_task = asyncio.create_task(run_dr_dci_investigation(backend, container.settings, req))
            if idem_key:
                self._inflight_by_key[idem_key] = work_task

        if idem_key:
            self._inflight_waiters.setdefault(idem_key, set()).add(task_id)
            self._task_to_idem_key[task_id] = idem_key

        async def _await_shared() -> LogInvestigationResponse:
            return await asyncio.shield(work_task) if idem_key else await work_task

        waiter_task: asyncio.Task[LogInvestigationResponse] = asyncio.create_task(_await_shared())
        self._task_waiters[task_id] = waiter_task
        try:
            result = await waiter_task
        except asyncio.CancelledError:
            self._canceled_task_ids.add(task_id)
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=ctx_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_CANCELED,
                        message=make_agent_json_message({"stage": "canceled", "taskId": task_id}, context_id=ctx_id),
                    ),
                )
            )
            return
        finally:
            self._task_waiters.pop(task_id, None)
            self._task_to_idem_key.pop(task_id, None)
            if idem_key:
                waiters = self._inflight_waiters.get(idem_key)
                if waiters is not None:
                    waiters.discard(task_id)
                    if not waiters:
                        self._inflight_waiters.pop(idem_key, None)
                        if self._inflight_by_key.get(idem_key) is work_task:
                            self._inflight_by_key.pop(idem_key, None)
                            if not work_task.done() and task_id in self._canceled_task_ids:
                                work_task.cancel()
            elif not work_task.done() and task_id in self._canceled_task_ids:
                work_task.cancel()

        if task_id in self._canceled_task_ids:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=ctx_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_CANCELED,
                        message=make_agent_json_message({"stage": "canceled", "taskId": task_id}, context_id=ctx_id),
                    ),
                )
            )
            return

        payload = result.model_dump()
        if idem_key and result.ok:
            self._idempotency_cache[idem_key] = payload

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
            self._canceled_task_ids.add(task_id)
            waiter = self._task_waiters.get(task_id)
            if waiter is not None and not waiter.done():
                waiter.cancel()
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
