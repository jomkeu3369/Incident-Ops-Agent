from __future__ import annotations

import json
from fastapi import APIRouter, Depends, FastAPI
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils.errors import UnsupportedOperationError
from pydantic import ValidationError

from common.a2a_utils import make_agent_json_message
from common.backend_client import BackendClient
from common.config import Settings
from common.di import get_backend_client, get_settings
from common.security import verify_internal_token
from log_agent.schemas import LogInvestigationRequest, LogInvestigationResponse
from log_agent.services import run_dr_dci_investigation

router = APIRouter(prefix="/internal", tags=["a2a-internal"], dependencies=[Depends(verify_internal_token)])


@router.get("/ping")
async def internal_ping() -> dict[str, str]:
    """내부 토큰 인증 확인용 엔드포인트."""
    return {"service": "log-analyzer-agent", "protocol": "a2a", "auth": "verified"}


@router.post("/investigate", response_model=LogInvestigationResponse)
async def investigate_logs(body: LogInvestigationRequest, backend: BackendClient = Depends(get_backend_client), cfg: Settings = Depends(get_settings)) -> LogInvestigationResponse:
    """DR-DCI 로그 조사를 직접 실행하는 내부 DI 엔드포인트."""
    return await run_dr_dci_investigation(backend, cfg, body)


class LogAgentExecutor(AgentExecutor):
    """A2A JSON-RPC 요청을 받아 DR-DCI 로그 조사를 수행하는 실행기."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """사용자 메시지 JSON을 파싱해 DR-DCI 결과를 A2A 응답 메시지로 반환한다."""
        raw = context.get_user_input()
        try:
            req = LogInvestigationRequest.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError):
            err = LogInvestigationResponse(ok=False, incidentCode="", error={"code": "INVALID_INPUT", "message": "로그 조사 요청 JSON 형식이 올바르지 않습니다."})
            await event_queue.enqueue_event(make_agent_json_message(err.model_dump(), context_id=context.context_id))
            return

        container = self._app.state.container
        backend = container.backend_client or BackendClient(container.settings, container.http_client)
        result = await run_dr_dci_investigation(backend, container.settings, req)
        await event_queue.enqueue_event(make_agent_json_message(result.model_dump(), context_id=context.context_id))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """취소 요청은 지원하지 않는다."""
        raise UnsupportedOperationError()
