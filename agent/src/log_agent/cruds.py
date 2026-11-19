from __future__ import annotations

from typing import Any, Literal

from common.backend_client import BackendClient


class CorpusCrud:
    """Spring MCP 코퍼스 도구 호출을 담당하는 CRUD 계층."""

    def __init__(self, backend: BackendClient) -> None:
        self._backend = backend

    async def search_index(self, incident_code: str, corpus_type: Literal["LOG", "RUNBOOK"], keyword: str | None = None) -> list[dict[str, Any]]:
        """장애 범위의 로그 또는 서비스 런북 메타데이터 목록을 조회한다."""
        return await self._backend.search_corpus_index(incident_code, corpus_type, keyword)

    async def pull_chunk(self, incident_code: str, chunk_id: str) -> dict[str, Any]:
        """지정한 청크 ID의 본문을 Spring MCP로 단건 인출한다."""
        return await self._backend.pull_corpus_chunk(incident_code, chunk_id)
