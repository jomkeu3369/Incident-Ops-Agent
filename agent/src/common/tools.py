from __future__ import annotations

import asyncio
import math
import re
from typing import Any
import httpx

from common.calendar_adapter import FreezeCalendarAdapter, GoogleCalendarFreezeAdapter, LocalFreezeCalendarAdapter
from common.config import Settings
from common.planner_math import parse_rfc3339

# PromQL 라벨 인젝션 방지를 위한 허용 문자 정규식
_PROM_LABEL_SAFE = re.compile(r"^[A-Za-z0-9._:/-]+$")


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    """공통 성공 응답 봉투를 생성한다."""
    return {"ok": True, "data": data}


def _err(code: str, message: str) -> dict[str, Any]:
    """공통 실패 응답 봉투를 생성한다."""
    return {"ok": False, "error": {"code": code, "message": message}}


def get_external_tool_specs() -> list[dict[str, Any]]:
    """에이전트가 선택·호출할 외부 API Tool 3종 명세(F3001)를 반환한다."""
    return [
        {
            "name": "list_rollback_candidates",
            "kind": "external_api",
            "description": "장애가 발생한 환경에서 최근 성공 배포와 이전 롤백 후보 배포 목록을 찾을 때 호출한다.",
            "input_schema": {
                "type": "object",
                "properties": {"serviceName": {"type": "string"}, "incidentStartedAt": {"type": "string", "format": "date-time"}, "environment": {"type": "string"}},
                "required": ["serviceName", "incidentStartedAt"],
            },
        },
        {
            "name": "get_release_metrics",
            "kind": "external_api",
            "description": "기준 배포와 롤백 후보의 502 오류 비율을 같은 관측 구간에서 비교할 때 호출한다.",
            "input_schema": {
                "type": "object",
                "properties": {"serviceName": {"type": "string"}, "gitSha": {"type": "string"}, "from": {"type": "string", "format": "date-time"}, "to": {"type": "string", "format": "date-time"}},
                "required": ["serviceName", "gitSha", "from", "to"],
            },
        },
        {
            "name": "get_change_freeze_windows",
            "kind": "external_api",
            "description": "허용된 작업 시간 범위에서 복구 조치와 겹치는 변경 금지 시간이 있는지 확인할 때 호출한다.",
            "input_schema": {
                "type": "object",
                "properties": {"serviceName": {"type": "string"}, "windowStart": {"type": "string", "format": "date-time"}, "windowEnd": {"type": "string", "format": "date-time"}},
                "required": ["serviceName", "windowStart", "windowEnd"],
            },
        },
    ]


class ExternalToolsClient:
    """docs/05-external-api-tools.md 계약을 따르는 외부 API Tool 3종 클라이언트."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient,
        calendar_adapter: FreezeCalendarAdapter | None = None,
    ) -> None:
        self._settings = settings
        self._http = http_client
        if calendar_adapter is not None:
            self._calendar_adapter: FreezeCalendarAdapter = calendar_adapter
        elif settings.calendar_adapter_mode.strip().lower() == "local":
            self._calendar_adapter = LocalFreezeCalendarAdapter(settings.local_freeze_calendar_path or None)
        else:
            self._calendar_adapter = GoogleCalendarFreezeAdapter(settings, http_client)

    async def list_rollback_candidates(self, service_name: str, incident_started_at: str, environment: str | None = None) -> dict[str, Any]:
        """GitHub Deployments에서 장애 기준 시각 이전의 기준 배포와 롤백 후보를 조회한다."""
        svc = self._settings.resolve_service_config(service_name)
        if svc is None:
            return _err("INVALID_INPUT", f"등록되지 않은 서비스입니다: {service_name}")
        if not svc.github_repo or "/" not in svc.github_repo:
            return _err("INVALID_INPUT", "서비스의 GitHub 저장소(owner/repo) 설정이 없습니다.")
        try:
            started_dt = parse_rfc3339(incident_started_at)
        except ValueError as exc:
            return _err("INVALID_INPUT", str(exc))

        env = (environment or svc.github_environment or "").strip()
        if not env:
            return _err("INVALID_INPUT", "배포 환경(environment)이 비어 있습니다.")

        base = self._settings.github_api_base_url.rstrip("/")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = self._settings.github_token.get_secret_value()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            dep_resp = await self._http.get(f"{base}/repos/{svc.github_repo}/deployments", params={"environment": env, "per_page": 30}, headers=headers)
            failure = _map_http_failure(dep_resp, "GitHub Deployments")
            if failure:
                return failure
            deployments = dep_resp.json()
            if not isinstance(deployments, list):
                return _err("UPSTREAM_ERROR", "GitHub 배포 목록 응답 형식이 올바르지 않습니다.")

            valid_deps: list[tuple[int, str, str]] = []
            for dep in deployments:
                dep_id, sha = dep.get("id"), dep.get("sha")
                if dep_id is None or not sha:
                    continue
                valid_deps.append((int(dep_id), str(sha), str(dep.get("ref") or "")))

            sem = asyncio.Semaphore(6)

            async def _fetch_statuses(dep_id: int) -> httpx.Response:
                async with sem:
                    return await self._http.get(
                        f"{base}/repos/{svc.github_repo}/deployments/{dep_id}/statuses",
                        params={"per_page": 30},
                        headers=headers,
                    )

            status_resps = await asyncio.gather(*(_fetch_statuses(dep_id) for dep_id, _, _ in valid_deps))
            succeeded: list[dict[str, Any]] = []
            for (dep_id, sha, ref), st_resp in zip(valid_deps, status_resps):
                failure = _map_http_failure(st_resp, "GitHub Deployment Statuses")
                if failure:
                    return failure
                statuses = st_resp.json()
                if not isinstance(statuses, list):
                    continue
                # 배포 생성일이 아니라 장애 시작 시각 이전(<=)의 state=success 기록 중 최신 시각을 사용한다
                eligible_times = [ts for st in statuses if st.get("state") == "success" and st.get("created_at") and (ts := parse_rfc3339(st["created_at"])) <= started_dt]
                if eligible_times:
                    succeeded.append({"deploymentId": dep_id, "gitSha": sha, "ref": ref, "succeededAt": max(eligible_times)})
        except httpx.HTTPError:
            return _err("UPSTREAM_ERROR", "GitHub API 요청 중 네트워크 오류가 발생했습니다.")
        except (ValueError, KeyError, TypeError):
            return _err("UPSTREAM_ERROR", "GitHub API 응답을 해석하지 못했습니다.")

        if not succeeded:
            return _err("NO_RESULT", "장애 기준 시각 이전에 성공한 배포 이력이 없습니다.")

        succeeded.sort(key=lambda item: item["succeededAt"], reverse=True)
        reference = succeeded[0]

        # 기준 배포와 다른 SHA만 남기고 동일 SHA는 최신 성공 기록만 유지한다
        seen_shas = {reference["gitSha"]}
        candidates: list[dict[str, Any]] = []
        for item in succeeded[1:]:
            if item["gitSha"] in seen_shas:
                continue
            seen_shas.add(item["gitSha"])
            candidates.append(_serialize_deployment(item))

        if not candidates:
            return _err("NO_RESULT", "기준 배포와 다른 커밋 SHA를 가진 롤백 후보가 없습니다.")

        return _ok({"referenceDeployment": _serialize_deployment(reference), "candidates": candidates})

    async def get_release_metrics(self, service_name: str, git_sha: str, from_time: str, to_time: str) -> dict[str, Any]:
        """Grafana Cloud Prometheus에서 특정 git_sha의 관측 구간 502 비율을 조회한다."""
        svc = self._settings.resolve_service_config(service_name)
        if svc is None:
            return _err("INVALID_INPUT", f"등록되지 않은 서비스입니다: {service_name}")
        if not svc.grafana_prometheus_url:
            return _err("INVALID_INPUT", "Grafana Prometheus 엔드포인트가 설정되지 않았습니다.")
        sha = git_sha.strip()
        if not sha or not _PROM_LABEL_SAFE.match(sha) or not _PROM_LABEL_SAFE.match(svc.grafana_service_label):
            return _err("INVALID_INPUT", "gitSha 또는 서비스 라벨 형식이 올바르지 않습니다.")
        try:
            from_dt, to_dt = parse_rfc3339(from_time), parse_rfc3339(to_time)
        except ValueError as exc:
            return _err("INVALID_INPUT", str(exc))
        if from_dt >= to_dt:
            return _err("INVALID_INPUT", "관측 시작 시각(from)은 종료 시각(to)보다 이전이어야 합니다.")

        token = self._settings.grafana_metrics_token.get_secret_value()
        if not token:
            return _err("AUTH_FAILED", "Grafana Cloud metrics 토큰이 설정되지 않았습니다.")

        window_sec = max(1, int((to_dt - from_dt).total_seconds()))
        metric = self._settings.grafana_metric_name
        total_q = f'sum(increase({metric}{{service="{svc.grafana_service_label}",git_sha="{sha}"}}[{window_sec}s]))'
        err502_q = f'sum(increase({metric}{{service="{svc.grafana_service_label}",git_sha="{sha}",status="502"}}[{window_sec}s]))'

        url = f"{svc.grafana_prometheus_url.rstrip('/')}/api/v1/query"
        auth = (self._settings.grafana_instance_id, token) if self._settings.grafana_instance_id else None
        headers = {} if auth else {"Authorization": f"Bearer {token}"}
        eval_ts = str(to_dt.timestamp())

        try:
            total_val, total_err = await self._query_prom_scalar(url, total_q, eval_ts, auth, headers)
            if total_err:
                return total_err
            err_val, err_err = await self._query_prom_scalar(url, err502_q, eval_ts, auth, headers)
            if err_err:
                return err_err
        except httpx.HTTPError:
            return _err("UPSTREAM_ERROR", "Grafana Cloud API 요청 중 네트워크 오류가 발생했습니다.")

        # 시계열 누락, 음수 또는 요청 수 0은 오류율 0%와 구분해 INSUFFICIENT_DATA로 반환한다
        if total_val is None or err_val is None or total_val <= 0 or err_val < 0:
            return _err("INSUFFICIENT_DATA", "관측 구간에 필요한 시계열 또는 요청 데이터가 없어 오류율을 계산할 수 없습니다.")

        return _ok({"gitSha": sha, "from": from_dt.isoformat(), "to": to_dt.isoformat(), "totalRequests": round(total_val, 2), "http502Requests": round(err_val, 2), "http502RatePercent": round((err_val / total_val) * 100.0, 4)})

    async def _query_prom_scalar(self, url: str, query: str, eval_ts: str, auth: tuple[str, str] | None, headers: dict[str, str]) -> tuple[float | None, dict[str, Any] | None]:
        """Prometheus instant query를 호출해 단일 수치 결과를 추출한다."""
        resp = await self._http.get(url, params={"query": query, "time": eval_ts}, auth=auth, headers=headers)
        failure = _map_http_failure(resp, "Grafana Cloud Metrics")
        if failure:
            return None, failure
        try:
            body = resp.json()
            if body.get("status") != "success":
                return None, _err("UPSTREAM_ERROR", "Grafana Prometheus 쿼리 실행에 실패했습니다.")
            results = body.get("data", {}).get("result", [])
            if not results:
                return None, None
            val = float(results[0]["value"][1])
            if not math.isfinite(val):
                return None, _err("INSUFFICIENT_DATA", "관측 구간 지표에 유효하지 않은 수치(NaN/Inf)가 포함되어 있습니다.")
            return val, None
        except (ValueError, KeyError, IndexError, TypeError):
            return None, _err("UPSTREAM_ERROR", "Grafana 지표 응답을 해석하지 못했습니다.")

    async def get_change_freeze_windows(self, service_name: str, window_start: str, window_end: str) -> dict[str, Any]:
        """교체형 캘린더 어댑터(Google Calendar 또는 로컬 저장소)로 변경 금지 구간 목록을 조회한다."""
        return await self._calendar_adapter.get_freeze_windows(service_name, window_start, window_end)


def _serialize_deployment(item: dict[str, Any]) -> dict[str, Any]:
    """내부 배포 딕셔너리를 RFC 3339 출력 포맷으로 직렬화한다."""
    return {"deploymentId": item["deploymentId"], "gitSha": item["gitSha"], "ref": item["ref"], "succeededAt": item["succeededAt"].isoformat()}


def _map_http_failure(resp: httpx.Response, provider: str) -> dict[str, Any] | None:
    """외부 API HTTP 오류 코드를 공통 실패 코드로 변환한다."""
    if 200 <= resp.status_code < 300:
        return None
    if resp.status_code == 429 or (resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0"):
        return _err("RATE_LIMITED", f"{provider} 호출 한도를 초과했습니다.")
    if resp.status_code in (401, 403):
        return _err("AUTH_FAILED", f"{provider} 인증 또는 권한 확인에 실패했습니다.")
    return _err("UPSTREAM_ERROR", f"{provider} 요청이 실패했습니다 (HTTP {resp.status_code}).")
