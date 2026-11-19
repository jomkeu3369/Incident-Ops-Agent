from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI

from common.app_factory import create_app
from common.config import Settings
from common.event_log import EventBus, LoggingSubAgent, attach_logging_sub_agent
from supervisor.cruds import SupervisorRedisStore
from supervisor.routers import router


def create_supervisor_app(settings: Settings | None = None, *, attach_logger: bool = True) -> FastAPI:
    """총괄 에이전트 FastAPI 앱을 생성하고 EventBus·로깅 서브 에이전트·AG-UI 라우터를 조립한다."""
    app = create_app("supervisor-agent", settings=settings, enable_redis=True)
    base_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def supervisor_lifespan(target: FastAPI):
        async with base_lifespan(target):
            container = target.state.container
            store = SupervisorRedisStore(container.redis_client)
            bus = EventBus()
            sub_agent = LoggingSubAgent(store.append_session_log)
            if attach_logger:
                attach_logging_sub_agent(bus, sub_agent)
            container.supervisor_store = store
            container.event_bus = bus
            container.logging_sub_agent = sub_agent
            yield

    app.router.lifespan_context = supervisor_lifespan
    app.include_router(router)
    return app


app = create_supervisor_app()
