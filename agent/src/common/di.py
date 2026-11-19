from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import httpx
from fastapi import Depends, Request

from common.backend_client import BackendClient
from common.config import Settings
from common.decisions import DecisionsClient
from common.event_log import EventBus, LoggingSubAgent
from common.tools import ExternalToolsClient


@dataclass
class AppContainer:
    """FastAPI lifespan에서 생명주기를 관리하는 공통 의존성 컨테이너."""

    settings: Settings
    http_client: httpx.AsyncClient
    redis_client: Any = None
    backend_client: BackendClient | None = None
    external_tools: ExternalToolsClient | None = None
    decisions_client: DecisionsClient | None = None
    event_bus: EventBus | None = None
    logging_sub_agent: LoggingSubAgent | None = None
    supervisor_store: Any = None
    a2a_caller: Any = None

    async def aclose(self) -> None:
        """컨테이너 종료 시 HTTP·Redis 커넥션을 정리한다."""
        await self.http_client.aclose()
        if self.redis_client is not None:
            close = getattr(self.redis_client, "aclose", None) or getattr(self.redis_client, "close", None)
            if close is not None:
                res = close()
                if hasattr(res, "__await__"):
                    await res


def build_container(settings: Settings | None = None, *, enable_redis: bool = False) -> AppContainer:
    """설정을 바탕으로 공통 DI 컨테이너를 생성한다."""
    cfg = settings or Settings()
    http_client = httpx.AsyncClient(timeout=cfg.http_timeout_seconds)
    redis_client = None
    if enable_redis:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(cfg.redis_url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=1.5)
    return AppContainer(
        settings=cfg,
        http_client=http_client,
        redis_client=redis_client,
        backend_client=BackendClient(cfg, http_client),
        external_tools=ExternalToolsClient(cfg, http_client),
        decisions_client=DecisionsClient(cfg),
    )


def get_container(request: Request) -> AppContainer:
    """앱 상태에 바인딩된 DI 컨테이너를 반환한다."""
    return request.app.state.container


def get_settings(request: Request) -> Settings:
    """공통 설정 의존성을 반환한다."""
    return get_container(request).settings


def get_http_client(request: Request) -> httpx.AsyncClient:
    """공용 비동기 HTTP 클라이언트를 반환한다."""
    return get_container(request).http_client


def get_redis_client(request: Request) -> Any:
    """비동기 Redis 클라이언트를 반환한다."""
    return get_container(request).redis_client


def get_backend_client(container: AppContainer = Depends(get_container)) -> BackendClient:
    """Spring MCP/REST 클라이언트를 반환한다."""
    return container.backend_client or BackendClient(container.settings, container.http_client)


def get_external_tools(container: AppContainer = Depends(get_container)) -> ExternalToolsClient:
    """외부 API Tool 3종 클라이언트를 반환한다."""
    return container.external_tools or ExternalToolsClient(container.settings, container.http_client)


def get_decisions_client(container: AppContainer = Depends(get_container)) -> DecisionsClient:
    """OpenAI Decisions API 클라이언트를 반환한다."""
    return container.decisions_client or DecisionsClient(container.settings)
