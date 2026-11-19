from __future__ import annotations

from fastapi import FastAPI
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from common.a2a_utils import mount_a2a_agent
from common.app_factory import create_app
from common.config import Settings
from remediation_agent.routers import RemediationAgentExecutor, router


def _build_agent_card(cfg: Settings) -> AgentCard:
    """복구 계획 A2A 에이전트 카드를 생성한다."""
    return AgentCard(
        name="remediation-agent",
        description="외부 API Tool 3종(GitHub·Grafana·Calendar)과 Decisions API로 복구 계획을 수립·재계획하고 승인된 조치를 실행하는 에이전트",
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(id="plan_remediation", name="Plan Remediation", description="롤백 후보·지표·변경 금지 시간을 조회해 근거 판정을 통과한 복구 계획을 작성한다.", tags=["a2a", "planner", "decisions"]),
            AgentSkill(id="execute_remediation", name="Execute Approved Remediation", description="운영자 승인을 받은 복구 조치를 실행하고 검증·포스트모템 초안을 반환한다.", tags=["a2a", "execution"]),
        ],
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0", url=cfg.remediation_agent_a2a_url)],
    )


def create_remediation_agent_app(settings: Settings | None = None) -> FastAPI:
    """복구 계획 에이전트 FastAPI 앱과 A2A 라우트를 조립한다."""
    cfg = settings or Settings()
    app = create_app("remediation-agent", settings=cfg, enable_redis=False)
    app.include_router(router)
    mount_a2a_agent(app, agent_card=_build_agent_card(cfg), executor=RemediationAgentExecutor(app))
    return app


app = create_remediation_agent_app()
