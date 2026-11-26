"""명시적 취소가 스레드 락을 기다리기 전에 진행 중인 실행에 신호를 전달하기 위한 프로세스 내 레지스트리.

Supervisor를 여러 프로세스로 늘리면 공유 신호 채널이 필요하다.
"""

from __future__ import annotations

import asyncio
from typing import Any


class ActiveRun:
    def __init__(self, incident_code: str) -> None:
        self.incident_code = incident_code
        self.cancel_requested = False
        self._task: asyncio.Task[Any] | None = None

    def attach(self, task: asyncio.Task[Any]) -> None:
        """중단해도 안전한 작업만 연결한다. 원장 쓰기 작업은 연결하지 않는다."""
        self._task = task
        if self.cancel_requested:
            task.cancel()

    def detach(self) -> None:
        self._task = None

    def request_cancel(self) -> None:
        if self.cancel_requested:
            return
        self.cancel_requested = True
        if self._task is not None and not self._task.done():
            self._task.cancel()


def was_cancelled_by_request(run: ActiveRun) -> bool:
    """자식 작업의 CancelledError가 명시적 취소 때문인지(연결 단절이 아닌지) 판별한다."""
    current = asyncio.current_task()
    return run.cancel_requested and (current is None or current.cancelling() == 0)
