from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class LogInvestigationRequest(BaseModel):
    """로그 분석 A2A 요청 스키마."""

    incidentCode: str = Field(..., min_length=1)
    keywords: list[str] = Field(default_factory=list)
    excludeChunkIds: list[str] = Field(default_factory=list)
    maxPulls: int | None = Field(default=None, ge=1)
    idempotencyKey: str | None = Field(default=None, max_length=128)
    traceId: str | None = Field(default=None, max_length=128)


class EvidenceItem(BaseModel):
    """DR-DCI로 추출한 근거 청크 요약."""

    chunkId: str
    corpusType: str
    title: str
    summary: str


class LogInvestigationResponse(BaseModel):
    """로그 분석 A2A 응답 스키마."""

    ok: bool
    incidentCode: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    evidenceChunkIds: list[str] = Field(default_factory=list)
    runbookSummary: str = ""
    recommendedCommand: str | None = None
    remainingChunkIds: list[str] = Field(default_factory=list)
    pulledCount: int = 0
    toolTrace: list[dict[str, Any]] = Field(default_factory=list)
    decisionTrace: list[dict[str, Any]] = Field(default_factory=list)
    stopReason: str | None = None
    error: dict[str, Any] | None = None
