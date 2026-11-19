from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Literal
import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client

from common.config import INTERNAL_TOKEN_HEADER, Settings

McpCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


class BackendApiError(Exception):
    """Spring REST 또는 MCP 백엔드 호출 실패를 표현하는 안전한 예외."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class BackendClient:
    """X-Internal-Token 인증으로 Spring MCP(HTTP+SSE)와 도메인 REST API를 호출한다."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient, *, mcp_caller: McpCaller | None = None) -> None:
        self._settings = settings
        self._http = http_client
        self._mcp_caller = mcp_caller

    def _auth_headers(self) -> dict[str, str]:
        """백엔드 내부 토큰 헤더를 구성한다 (미설정 시 fail-closed)."""
        token = self._settings.internal_service_token.get_secret_value().strip()
        if not token:
            raise BackendApiError(401, "INTERNAL_SERVICE_TOKEN이 설정되지 않았습니다.")
        return {INTERNAL_TOKEN_HEADER: token}

    async def call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Spring AI MCP 서버의 도구를 SSE 세션으로 호출하고 JSON 텍스트를 파싱한다."""
        if self._mcp_caller is not None:
            return await self._mcp_caller(tool_name, arguments)

        headers = self._auth_headers()
        clean_args = {k: v for k, v in arguments.items() if v is not None}
        try:
            async with (
                sse_client(self._settings.spring_mcp_sse_url, headers=headers) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool(tool_name, clean_args)
        except BackendApiError:
            raise
        except Exception as exc:
            raise BackendApiError(502, f"Spring MCP 호출에 실패했습니다: {tool_name}") from exc

        text_parts = [getattr(item, "text", "") for item in (getattr(result, "content", None) or []) if getattr(item, "text", None)]
        raw_text = "\n".join(text_parts).strip()

        # 입력 검증 또는 상태 검사 실패 시 Spring MCP는 is_error(구 isError)=True를 반환한다
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            raise BackendApiError(409, raw_text or f"MCP 도구 실행이 거절되었습니다: {tool_name}")

        if not raw_text:
            return {}
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            return raw_text

    # --- Spring MCP 도구 4종 ---

    async def search_corpus_index(self, incident_code: str, corpus_type: Literal["LOG", "RUNBOOK"], keyword: str | None = None) -> list[dict[str, Any]]:
        """장애 범위의 로그 또는 서비스 런북 메타데이터 목록을 검색한다."""
        res = await self.call_mcp_tool("searchCorpusIndex", {"incidentCode": incident_code, "corpusType": corpus_type, "keyword": keyword})
        return res if isinstance(res, list) else []

    async def pull_corpus_chunk(self, incident_code: str, chunk_id: str) -> dict[str, Any]:
        """지정 청크 ID의 원문을 단건 인출한다."""
        res = await self.call_mcp_tool("pullCorpusChunk", {"incidentCode": incident_code, "chunkId": chunk_id})
        return res if isinstance(res, dict) else {"chunkId": chunk_id, "content": str(res)}

    async def get_incident_detail_mcp(self, incident_code: str) -> dict[str, Any]:
        """MCP 도구로 장애 상세 정보를 조회한다."""
        res = await self.call_mcp_tool("getIncidentDetail", {"incidentCode": incident_code})
        return res if isinstance(res, dict) else {}

    async def complete_incident_with_postmortem(self, *, approval_id: str, incident_code: str, action_command: str, executed_by: str, execution_status: Literal["SUCCESS", "FAILED"], execution_output: str | None = None, verification_summary: str | None = None, root_cause: str | None = None, timeline_markdown: str | None = None, prevention_items: str | None = None) -> dict[str, Any]:
        """승인된 복구 실행 결과와 포스트모템을 Spring MCP로 원자 반영한다."""
        res = await self.call_mcp_tool(
            "completeIncidentWithPostMortem",
            {
                "approvalId": approval_id,
                "incidentCode": incident_code,
                "actionCommand": action_command,
                "executedBy": executed_by,
                "executionStatus": execution_status,
                "executionOutput": execution_output,
                "verificationSummary": verification_summary,
                "rootCause": root_cause,
                "timelineMarkdown": timeline_markdown,
                "preventionItems": prevention_items,
            },
        )
        return res if isinstance(res, dict) else {}

    # --- Spring 도메인 REST API (MCP 미노출 경로) ---

    async def get_incident_detail(self, incident_code: str) -> dict[str, Any]:
        """REST API로 장애 상세와 계획·승인·실행·포스트모템을 조회한다."""
        return await self._rest("GET", f"/api/incidents/{incident_code}")

    async def create_plan(self, incident_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        """복구 계획을 백엔드에 등록한다."""
        return await self._rest("POST", f"/api/incidents/{incident_code}/plans", json=payload)

    async def create_approval(self, incident_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        """승인 대기 요청(HITL)을 백엔드에 생성한다."""
        return await self._rest("POST", f"/api/incidents/{incident_code}/approvals", json=payload)

    async def decide_approval(self, incident_code: str, approval_id: str, *, approved_by: str, decision: Literal["APPROVED", "REJECTED"]) -> dict[str, Any]:
        """운영자의 승인 또는 거절 결정을 백엔드 원장에 기록한다."""
        return await self._rest("PATCH", f"/api/incidents/{incident_code}/approvals/{approval_id}", json={"approvedBy": approved_by, "decision": decision})

    async def _rest(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        """내부 토큰을 실어 Spring REST 요청을 실행하고 오류를 정규화한다."""
        headers = self._auth_headers()
        url = f"{self._settings.backend_base_url.rstrip('/')}{path}"
        try:
            resp = await self._http.request(method, url, headers=headers, json=json)
        except httpx.HTTPError as exc:
            raise BackendApiError(502, "Spring 백엔드 연결에 실패했습니다.") from exc

        if 200 <= resp.status_code < 300:
            return resp.json() if resp.content else {}

        msg = f"Spring 백엔드 요청 실패 (HTTP {resp.status_code})"
        try:
            err_body = resp.json()
            if isinstance(err_body, dict) and err_body.get("message"):
                msg = str(err_body["message"])
        except ValueError:
            pass
        raise BackendApiError(resp.status_code, msg)
