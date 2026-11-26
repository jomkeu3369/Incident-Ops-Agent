from __future__ import annotations

from fastapi import FastAPI
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from common.a2a_utils import mount_a2a_agent
from common.app_factory import create_app
from common.config import Settings
from log_agent.routers import LogAgentExecutor, router


def _build_agent_card(cfg: Settings) -> AgentCard:
    """로그 분석 A2A 에이전트 카드를 생성한다."""
    return AgentCard(
        name="log-analyzer-agent",
        description="Spring MCP 코퍼스에서 DR-DCI 방식으로 장애 로그와 런북 근거를 인출하는 에이전트",
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[AgentSkill(id="dr_dci_investigate", name="DR-DCI Log Investigation", description="장애 코드 기준으로 로그·런북 메타데이터를 검색하고 필요한 청크만 선별 인출한다.", tags=["mcp", "dr-dci", "logs"])],
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0", url=cfg.log_agent_a2a_url)],
    )


def create_log_agent_app(settings: Settings | None = None) -> FastAPI:
    """로그 분석 에이전트 FastAPI 앱과 A2A 라우트를 조립한다."""
    cfg = settings or Settings()
    app = create_app("log-analyzer-agent", settings=cfg, enable_redis=False)
    app.include_router(router)
    mount_a2a_agent(app, agent_card=_build_agent_card(cfg), executor=LogAgentExecutor(app), expected_token=lambda s: s.log_agent_token)
    return app


app = create_log_agent_app()
