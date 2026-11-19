from __future__ import annotations

import json
from fastapi import APIRouter, Depends, FastAPI
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils.errors import UnsupportedOperationError
from pydantic import ValidationError

from common.a2a_utils import make_agent_json_message
from common.decisions import DecisionsClient
from common.di import get_decisions_client, get_external_tools
from common.security import verify_internal_token
from common.tools import ExternalToolsClient
from remediation_agent.schemas import (
    RemediationExecuteRequest,
    RemediationExecuteResponse,
    RemediationPlanRequest,
    RemediationPlanResponse,
)
from remediation_agent.services import build_remediation_plan, execute_approved_remediation

router = APIRouter(prefix="/internal", tags=["a2a-internal"], dependencies=[Depends(verify_internal_token)])


@router.get("/ping")
async def internal_ping() -> dict[str, str]:
    """내부 토큰 인증 확인용 엔드포인트."""
    return {"service": "remediation-agent", "protocol": "a2a", "auth": "verified"}


@router.post("/plan", response_model=RemediationPlanResponse)
async def create_plan_draft(body: RemediationPlanRequest, tools: ExternalToolsClient = Depends(get_external_tools), decisions: DecisionsClient = Depends(get_decisions_client)) -> RemediationPlanResponse:
    """복구 계획 수립을 직접 호출하는 내부 DI 엔드포인트."""
    return await build_remediation_plan(tools, decisions, body)


@router.post("/execute", response_model=RemediationExecuteResponse)
async def run_approved_action(body: RemediationExecuteRequest) -> RemediationExecuteResponse:
    """승인된 복구 조치를 실행하는 내부 DI 엔드포인트."""
    return await execute_approved_remediation(body)


class RemediationAgentExecutor(AgentExecutor):
    """A2A JSON-RPC 요청을 받아 복구 계획 수립 또는 승인 조치 실행을 수행하는 실행기."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """action 값(plan|execute)에 따라 계획 수립 또는 실행 결과를 A2A 메시지로 반환한다."""
        raw = context.get_user_input()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            err = {"ok": False, "status": "BLOCKED", "message": "복구 에이전트 요청 JSON 형식이 올바르지 않습니다."}
            await event_queue.enqueue_event(make_agent_json_message(err, context_id=context.context_id))
            return

        action = data.get("action", "plan") if isinstance(data, dict) else "plan"
        container = self._app.state.container
        tools = container.external_tools or ExternalToolsClient(container.settings, container.http_client)
        decisions = container.decisions_client or DecisionsClient(container.settings)

        try:
            if action == "execute":
                exec_req = RemediationExecuteRequest.model_validate(data)
                result = await execute_approved_remediation(exec_req)
            else:
                plan_req = RemediationPlanRequest.model_validate(data)
                result = await build_remediation_plan(tools, decisions, plan_req)
        except ValidationError:
            err = {"ok": False, "status": "BLOCKED", "message": "복구 에이전트 요청 필드가 누락되었거나 올바르지 않습니다."}
            await event_queue.enqueue_event(make_agent_json_message(err, context_id=context.context_id))
            return

        await event_queue.enqueue_event(make_agent_json_message(result.model_dump(), context_id=context.context_id))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """취소 요청은 지원하지 않는다."""
        raise UnsupportedOperationError()
