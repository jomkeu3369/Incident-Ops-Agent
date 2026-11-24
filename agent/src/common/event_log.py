from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


def now_iso_utc() -> str:
    """현재 UTC 시각을 RFC 3339 문자열로 반환한다."""
    return datetime.now(timezone.utc).isoformat()


class EventBus:
    """메인 에이전트가 구독자 구현을 알지 못한 채 실행 이벤트를 발행하는 버스."""

    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> None:
        """이벤트 구독 핸들러를 등록한다."""
        if handler not in self._subscribers:
            self._subscribers.append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        """등록된 이벤트 구독 핸들러를 해제한다."""
        if handler in self._subscribers:
            self._subscribers.remove(handler)

    async def publish(self, session_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """세션 식별자와 시각을 붙여 이벤트를 발행하며, 구독자 실패가 메인을 중단시키지 않게 격리한다."""
        event = {"sessionId": session_id, "eventType": event_type, "timestamp": now_iso_utc(), "payload": payload or {}}
        for handler in list(self._subscribers):
            try:
                await handler(event)
            except Exception:
                # 로깅 에이전트나 저장소 장애가 발생해도 메인 에이전트는 정상 동작해야 한다
                logger.warning("실행 이벤트 구독 처리 실패(격리됨): %s", event_type)
        return event


class LoggingSubAgent:
    """발행된 실행 이벤트를 LLM 호출 없이 구독해 세션 단위로 원본 적재하는 서브 에이전트."""

    def __init__(self, log_writer: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self._write_log = log_writer
        self.llm_calls = 0  # 로그 수집·저장 단계 LLM 호출 0회 보장 검증용 카운터

    async def handle_event(self, event: dict[str, Any]) -> None:
        """수신한 원본 이벤트를 가공 없이 세션 실행 로그 저장소에 추가한다."""
        session_id = str(event.get("sessionId") or "").strip()
        if not session_id:
            return
        await self._write_log(session_id, event)


def attach_logging_sub_agent(bus: EventBus, sub_agent: LoggingSubAgent) -> None:
    """EventBus에 로깅 서브 에이전트 구독자를 연결한다."""
    bus.subscribe(sub_agent.handle_event)


async def publish_safe(bus: EventBus | None, session_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
    """버스가 주입된 경우에만 비동기로 이벤트를 발행한다."""
    if bus is None:
        return
    await asyncio.shield(bus.publish(session_id, event_type, payload))
