from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Literal
import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client

from common.config import AGENT_ROLE_HEADER, INTERNAL_TOKEN_HEADER, Settings
from common.observability import end_span, traced_span

McpCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
McpResourceReader = Callable[[str | None], Awaitable[Any]]
READ_ONLY_AGENT_ROLES = frozenset({"investigator"})
READ_ONLY_MCP_TOOLS = frozenset({"searchCorpusIndex", "pullCorpusChunk", "getIncidentDetail"})
EVIDENCE_SUMMARY_RESOURCE_URI = "incident://evidence-summary/v1"


class BackendApiError(Exception):
    """Spring REST 또는 MCP 백엔드 호출 실패를 표현하는 안전한 예외."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class BackendClient:
    """X-Internal-Token 인증 및 X-Agent-Role 권한 분리로 Spring MCP(HTTP+SSE)와 도메인 REST API를 호출한다."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient,
        *,
        mcp_caller: McpCaller | None = None,
        mcp_resource_reader: McpResourceReader | None = None,
        agent_role: str = "supervisor",
    ) -> None:
        self._settings = settings
        self._http = http_client
        self._mcp_caller = mcp_caller
        self._mcp_resource_reader = mcp_resource_reader
        self.agent_role = (agent_role or "supervisor").strip().lower()

    def _auth_headers(self) -> dict[str, str]:
        """백엔드 내부 토큰 및 에이전트 역할 헤더를 구성한다 (토큰 미설정 시 fail-closed)."""
        token = (
            self._settings.internal_investigator_token.get_secret_value().strip()
            if self.agent_role in READ_ONLY_AGENT_ROLES
            else ""
        ) or self._settings.internal_service_token.get_secret_value().strip()
        if not token:
            raise BackendApiError(401, "INTERNAL_SERVICE_TOKEN이 설정되지 않았습니다.")
        return {INTERNAL_TOKEN_HEADER: token, AGENT_ROLE_HEADER: self.agent_role}

    def _require_write_permission(self, action: str) -> None:
        """읽기 전용 조사 워커(investigator)가 쓰기·완료 경로를 호출하지 못하도록 이중 차단한다."""
        if self.agent_role in READ_ONLY_AGENT_ROLES:
            raise BackendApiError(403, f"읽기 전용 조사 워커({self.agent_role})는 쓰기/완료 작업을 호출할 수 없습니다: {action}")

    async def call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Spring AI MCP 서버의 도구를 SSE 세션으로 호출하고 JSON 텍스트를 파싱한다."""
        if tool_name not in READ_ONLY_MCP_TOOLS:
            self._require_write_permission(tool_name)
        clean_args = {k: v for k, v in arguments.items() if v is not None}
        async with traced_span(
            f"mcp.{tool_name}",
            run_type="tool",
            inputs={"tool": tool_name, "arguments": clean_args},
            tags=["mcp"],
        ) as span:
            if self._mcp_caller is not None:
                res = await self._mcp_caller(tool_name, arguments)
                end_span(span, outputs={"ok": True, "result_type": type(res).__name__})
                return res

            headers = self._auth_headers()
            try:
                async with (
                    sse_client(self._settings.spring_mcp_sse_url, headers=headers) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    result = await session.call_tool(tool_name, clean_args)
            except BackendApiError as exc:
                end_span(span, error=f"HTTP {exc.status_code}: {exc.message}")
                raise
            except Exception as exc:
                end_span(span, error=f"Spring MCP call failed: {tool_name}")
                raise BackendApiError(502, f"Spring MCP 호출에 실패했습니다: {tool_name}") from exc

            text_parts = [getattr(item, "text", "") for item in (getattr(result, "content", None) or []) if getattr(item, "text", None)]
            raw_text = "\n".join(text_parts).strip()

            # 입력 검증 또는 상태 검사 실패 시 Spring MCP는 is_error(구 isError)=True를 반환한다
            if getattr(result, "is_error", False) or getattr(result, "isError", False):
                end_span(span, error=raw_text or f"MCP rejected: {tool_name}")
                raise BackendApiError(409, raw_text or f"MCP 도구 실행이 거절되었습니다: {tool_name}")

            if not raw_text:
                end_span(span, outputs={"ok": True})
                return {}
            try:
                parsed = json.loads(raw_text)
                end_span(span, outputs={"ok": True, "result_type": type(parsed).__name__})
                return parsed
            except json.JSONDecodeError:
                end_span(span, outputs={"ok": True, "result_chars": len(raw_text)})
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
        self._require_write_permission("completeIncidentWithPostMortem")
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

    # --- Spring MCP 읽기 전용 리소스 탐색·조회 ---

    async def list_mcp_resources(self) -> list[dict[str, Any]]:
        """Spring MCP 서버가 노출하는 읽기 전용 리소스 목록을 조회한다."""
        async with traced_span("mcp.list_resources", run_type="tool", inputs={"role": self.agent_role}, tags=["mcp", "resource"]) as span:
            if self._mcp_resource_reader is not None:
                res = await self._mcp_resource_reader(None)
                items = res if isinstance(res, list) else []
                end_span(span, outputs={"count": len(items)})
                return items
            headers = self._auth_headers()
            try:
                async with (
                    sse_client(self._settings.spring_mcp_sse_url, headers=headers) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    listed = await session.list_resources()
            except BackendApiError:
                raise
            except Exception as exc:
                end_span(span, error="Spring MCP list_resources failed")
                raise BackendApiError(502, "Spring MCP 리소스 목록 조회에 실패했습니다.") from exc

            out = [
                {
                    "uri": str(getattr(r, "uri", "")),
                    "name": str(getattr(r, "name", "")),
                    "description": str(getattr(r, "description", "") or ""),
                    "mimeType": str(getattr(r, "mimeType", "") or ""),
                }
                for r in (getattr(listed, "resources", None) or [])
            ]
            end_span(span, outputs={"count": len(out)})
            return out

    async def read_mcp_resource(self, uri: str = EVIDENCE_SUMMARY_RESOURCE_URI) -> dict[str, Any]:
        """버전 있는 읽기 전용 근거 요약 리소스를 MCP 표준 프로토콜로 조회한다."""
        async with traced_span("mcp.read_resource", run_type="tool", inputs={"uri": uri, "role": self.agent_role}, tags=["mcp", "resource"]) as span:
            if self._mcp_resource_reader is not None:
                res = await self._mcp_resource_reader(uri)
                parsed = res if isinstance(res, dict) else {"uri": uri, "content": res}
                end_span(span, outputs={"ok": True, "uri": uri})
                return parsed
            headers = self._auth_headers()
            try:
                async with (
                    sse_client(self._settings.spring_mcp_sse_url, headers=headers) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    result = await session.read_resource(uri)
            except BackendApiError:
                raise
            except Exception as exc:
                end_span(span, error=f"Spring MCP read_resource failed: {uri}")
                raise BackendApiError(502, f"Spring MCP 리소스 조회에 실패했습니다: {uri}") from exc

            contents = getattr(result, "contents", None) or []
            raw_text = "\n".join(str(getattr(c, "text", "") or "") for c in contents).strip()
            if not raw_text:
                end_span(span, outputs={"ok": True, "empty": True})
                return {}
            try:
                parsed = json.loads(raw_text)
                end_span(span, outputs={"ok": True, "uri": uri})
                return parsed if isinstance(parsed, dict) else {"uri": uri, "data": parsed}
            except json.JSONDecodeError:
                end_span(span, outputs={"ok": True, "uri": uri})
                return {"uri": uri, "text": raw_text}

    # --- Spring 도메인 REST API (MCP 미노출 경로) ---

    async def get_incident_detail(self, incident_code: str) -> dict[str, Any]:
        """REST API로 장애 상세와 계획·승인·실행·포스트모템을 조회한다."""
        return await self._rest("GET", f"/api/incidents/{incident_code}")

    async def create_plan(self, incident_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        """복구 계획을 백엔드에 등록한다."""
        self._require_write_permission("create_plan")
        return await self._rest("POST", f"/api/incidents/{incident_code}/plans", json=payload)

    async def create_approval(self, incident_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        """승인 대기 요청(HITL)을 백엔드에 생성한다."""
        self._require_write_permission("create_approval")
        return await self._rest("POST", f"/api/incidents/{incident_code}/approvals", json=payload)

    async def decide_approval(self, incident_code: str, approval_id: str, *, approved_by: str, decision: Literal["APPROVED", "REJECTED"]) -> dict[str, Any]:
        """운영자의 승인 또는 거절 결정을 백엔드 원장에 기록한다."""
        self._require_write_permission("decide_approval")
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
