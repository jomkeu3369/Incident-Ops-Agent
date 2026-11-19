from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class EvidenceSummary(BaseModel):
    """복구 계획에 인용할 근거 청크 요약."""

    chunkId: str
    summary: str
    corpusType: str = "LOG"


class RemediationPlanRequest(BaseModel):
    """복구 계획 수립 A2A 요청 스키마."""

    action: Literal["plan"] = "plan"
    incidentCode: str = Field(..., min_length=1)
    serviceName: str = Field(..., min_length=1)
    incidentStartedAt: str = Field(..., min_length=1)
    metricsFrom: str = Field(..., min_length=1)
    metricsTo: str = Field(..., min_length=1)
    allowedWindowStart: str = Field(..., min_length=1)
    allowedWindowEnd: str = Field(..., min_length=1)
    durationMinutes: int = Field(default=5, ge=1)
    max502RatePercent: float = Field(default=1.0, gt=0.0)
    evidence: list[EvidenceSummary] = Field(default_factory=list)
    runbookSummary: str = ""
    recommendedCommand: str | None = None
    cachedToolResults: dict[str, Any] = Field(default_factory=dict)


class RemediationExecuteRequest(BaseModel):
    """승인 완료된 복구 조치 실행 A2A 요청 스키마."""

    action: Literal["execute"] = "execute"
    incidentCode: str = Field(..., min_length=1)
    approvalId: str = Field(..., min_length=1)
    planId: str = Field(..., min_length=1)
    actionCommand: str = Field(..., min_length=1)
    verificationCriteria: str = ""
    evidence: list[EvidenceSummary] = Field(default_factory=list)


class PlanStepDraft(BaseModel):
    """복구 계획 단계 초안 (Spring RemediationPlanStep 계약 일치)."""

    stepNo: int
    description: str
    condition: str
    actionCommand: str | None = None
    evidenceChunkIds: list[str] = Field(default_factory=list)


class RemediationPlanResponse(BaseModel):
    """복구 계획 수립 A2A 응답 스키마."""

    ok: bool
    status: Literal["READY_FOR_APPROVAL", "NEED_MORE_EVIDENCE", "REVISED_NO_ROLLBACK", "BLOCKED"]
    incidentCode: str
    verdict: str | None = None
    confidence: float | None = None
    planDraft: dict[str, Any] | None = None
    selectedCandidate: dict[str, Any] | None = None
    rejectedCandidates: list[dict[str, Any]] = Field(default_factory=list)
    replanEvent: dict[str, Any] | None = None
    remainingGaps: list[dict[str, str]] = Field(default_factory=list)
    toolCalls: list[dict[str, Any]] = Field(default_factory=list)
    cachedToolResults: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class RemediationExecuteResponse(BaseModel):
    """승인 조치 실행 결과 및 포스트모템 초안 스키마."""

    ok: bool
    incidentCode: str
    approvalId: str
    executionStatus: Literal["SUCCESS", "FAILED"]
    executionOutput: str
    verificationSummary: str
    rootCause: str
    timelineMarkdown: str
    preventionItems: str
