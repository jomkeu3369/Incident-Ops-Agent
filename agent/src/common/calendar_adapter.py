from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
import httpx

from common.config import Settings
from common.planner_math import parse_rfc3339


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


@runtime_checkable
class FreezeCalendarAdapter(Protocol):
    """변경 금지 시간 조회를 위한 교체형 어댑터 인터페이스 (로컬 저장소 ↔ Google Calendar, F3023)."""

    async def get_freeze_windows(self, service_name: str, window_start: str, window_end: str) -> dict[str, Any]:
        """허용 구간과 겹치는 변경 금지 구간 목록을 공통 {ok, data | error} 계약으로 반환한다."""
        ...


class LocalFreezeCalendarAdapter:
    """로컬 JSON 파일 또는 인메모리 저장소 기반 변경 금지 캘린더 어댑터 (Google Calendar 교체용)."""

    def __init__(
        self,
        storage_path: str | Path | None = None,
        initial_windows: dict[str, list[dict[str, str]]] | None = None,
    ) -> None:
        self._path = Path(storage_path) if storage_path else None
        self._windows: dict[str, list[dict[str, str]]] = {}
        self._loaded_mtime_ns: int | None = None
        if initial_windows:
            for svc, items in initial_windows.items():
                self._windows[svc] = [dict(item) for item in items]
        elif self._path and self._path.is_file():
            self._load_from_file()

    def _load_from_file(self) -> None:
        if not self._path or not self._path.is_file():
            self._windows = {}
            self._loaded_mtime_ns = None
            return
        mtime_ns = self._path.stat().st_mtime_ns
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            self._windows = {
                str(svc): [dict(item) for item in items if isinstance(item, dict)]
                for svc, items in raw.items()
                if isinstance(items, list)
            }
        self._loaded_mtime_ns = mtime_ns

    def _refresh_if_file_changed(self) -> None:
        if not self._path:
            return
        if not self._path.is_file():
            if self._loaded_mtime_ns is not None:
                self._windows = {}
                self._loaded_mtime_ns = None
            return
        mtime_ns = self._path.stat().st_mtime_ns
        if self._loaded_mtime_ns != mtime_ns:
            self._load_from_file()

    def _save_to_file(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._windows, ensure_ascii=False, indent=2), encoding="utf-8")
        self._loaded_mtime_ns = self._path.stat().st_mtime_ns

    def add_freeze_window(self, service_name: str, start: str, end: str, reason: str = "") -> dict[str, str]:
        """로컬 저장소에 변경 금지 구간을 추가한다."""
        start_dt, end_dt = parse_rfc3339(start), parse_rfc3339(end)
        if start_dt >= end_dt:
            raise ValueError("변경 금지 시작 시각은 종료 시각보다 이전이어야 합니다.")
        record = {"start": start_dt.isoformat(), "end": end_dt.isoformat()}
        if reason:
            record["reason"] = reason
        self._windows.setdefault(service_name.strip(), []).append(record)
        self._save_to_file()
        return record

    async def get_freeze_windows(self, service_name: str, window_start: str, window_end: str) -> dict[str, Any]:
        """로컬 저장소에서 요청 구간과 겹치는 변경 금지 시간만 필터링해 반환한다."""
        svc = service_name.strip()
        if not svc:
            return _err("INVALID_INPUT", "서비스명이 비어 있습니다.")
        try:
            start_dt, end_dt = parse_rfc3339(window_start), parse_rfc3339(window_end)
        except ValueError as exc:
            return _err("INVALID_INPUT", str(exc))
        if start_dt >= end_dt:
            return _err("INVALID_INPUT", "허용 시작 시각은 종료 시각보다 이전이어야 합니다.")

        try:
            self._refresh_if_file_changed()
        except (OSError, json.JSONDecodeError, ValueError):
            return _err("UPSTREAM_ERROR", "로컬 변경 금지 캘린더 파일을 읽지 못했습니다.")

        overlapping: list[dict[str, str]] = []
        for item in self._windows.get(svc, []):
            if not item.get("start") or not item.get("end"):
                continue
            item_start = parse_rfc3339(item["start"])
            item_end = parse_rfc3339(item["end"])
            if item_start < end_dt and item_end > start_dt:
                overlapping.append({"start": item_start.isoformat(), "end": item_end.isoformat()})

        return _ok({"freezeWindows": overlapping, "adapter": "local"})


class GoogleCalendarFreezeAdapter:
    """Google Calendar FreeBusy API 기반 변경 금지 캘린더 어댑터."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http_client

    async def get_freeze_windows(self, service_name: str, window_start: str, window_end: str) -> dict[str, Any]:
        svc = self._settings.resolve_service_config(service_name)
        if svc is None:
            return _err("INVALID_INPUT", f"등록되지 않은 서비스입니다: {service_name}")
        if not svc.google_calendar_id:
            return _err("INVALID_INPUT", "변경 금지 캘린더 ID가 설정되지 않았습니다.")
        try:
            start_dt, end_dt = parse_rfc3339(window_start), parse_rfc3339(window_end)
        except ValueError as exc:
            return _err("INVALID_INPUT", str(exc))
        if start_dt >= end_dt:
            return _err("INVALID_INPUT", "허용 시작 시각은 종료 시각보다 이전이어야 합니다.")

        token = self._settings.google_calendar_token.get_secret_value()
        if not token:
            return _err("AUTH_FAILED", "Google Calendar 인증 토큰이 설정되지 않았습니다.")

        url = f"{self._settings.google_calendar_base_url.rstrip('/')}/freeBusy"
        payload = {"timeMin": start_dt.isoformat(), "timeMax": end_dt.isoformat(), "items": [{"id": svc.google_calendar_id}]}

        try:
            resp = await self._http.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 429 or (resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0"):
                return _err("RATE_LIMITED", "Google Calendar FreeBusy 호출 한도를 초과했습니다.")
            if resp.status_code in (401, 403):
                return _err("AUTH_FAILED", "Google Calendar FreeBusy 인증 또는 권한 확인에 실패했습니다.")
            if not (200 <= resp.status_code < 300):
                return _err("UPSTREAM_ERROR", f"Google Calendar FreeBusy 요청이 실패했습니다 (HTTP {resp.status_code}).")

            body = resp.json()
            cal = body.get("calendars", {}).get(svc.google_calendar_id)
            if not isinstance(cal, dict):
                return _err("UPSTREAM_ERROR", "Google Calendar FreeBusy 응답에 대상 캘린더가 없습니다.")
            cal_errors = cal.get("errors")
            if cal_errors:
                reasons = {str(e.get("reason", "")) for e in cal_errors if isinstance(e, dict)}
                if reasons & {"notFound", "forbidden", "unauthorized"}:
                    return _err("AUTH_FAILED", "변경 금지 캘린더에 접근할 수 없습니다.")
                return _err("UPSTREAM_ERROR", "변경 금지 캘린더 조회 중 오류가 반환되었습니다.")

            windows = [
                {"start": parse_rfc3339(item["start"]).isoformat(), "end": parse_rfc3339(item["end"]).isoformat()}
                for item in cal.get("busy", [])
                if isinstance(item, dict) and item.get("start") and item.get("end")
            ]
            return _ok({"freezeWindows": windows})
        except httpx.HTTPError:
            return _err("UPSTREAM_ERROR", "Google Calendar API 요청 중 네트워크 오류가 발생했습니다.")
        except (ValueError, KeyError, TypeError):
            return _err("UPSTREAM_ERROR", "Google Calendar 응답을 해석하지 못했습니다.")
