"""SSE 연결이 끊겨도 실행 락·원장 쓰기를 정해진 순서로 정리하는 보조 도구."""

from __future__ import annotations

import asyncio
from typing import Any

import anyio
from fastapi.responses import StreamingResponse
from starlette.types import Send


async def settle(task: asyncio.Future[Any]) -> Any:
    """호출자가 취소돼도 task가 끝날 때까지 기다린 뒤 취소를 전파한다.

    Starlette의 단절 처리(anyio cancel scope)는 await마다 취소를 다시 전달하므로 대기 구간을 보호한다.
    """
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await asyncio.wait([task])
        if not task.cancelled():
            task.exception()  # 취소가 우선이며 결과는 원장 조회로 확인한다
        raise


class ClosingStreamingResponse(StreamingResponse):
    """전송 중 단절로 yield에 멈춘 제너레이터를 즉시 닫아 GC에 맡기지 않고 락을 해제한다."""

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                with anyio.CancelScope(shield=True):
                    await close()
