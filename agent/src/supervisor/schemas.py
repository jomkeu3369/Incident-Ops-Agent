from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class ApprovalDecisionInput(BaseModel):
    """AG-UI forwardedProps.approvalDecision 입력 스키마."""

    approvalId: str = Field(..., min_length=1)
    decision: Literal["APPROVED", "REJECTED"]


class ForwardedPropsInput(BaseModel):
    """AG-UI RunAgentInput.forwardedProps 검증 스키마 (docs/09-frontend-api-spec.md)."""

    incidentCode: str = Field(..., min_length=1)
    incidentStartedAt: str | None = None
    approvalDecision: ApprovalDecisionInput | None = None
    restore: bool = False

    @model_validator(mode="after")
    def validate_exclusive_flags(self) -> ForwardedPropsInput:
        """restore 요청과 approvalDecision은 동시에 보낼 수 없다."""
        if self.restore and self.approvalDecision is not None:
            raise ValueError("restore와 approvalDecision은 함께 보낼 수 없습니다.")
        return self


class SessionSummary(BaseModel):
    """세션 실행 로그 목록 항목 스키마."""

    sessionId: str
    eventCount: int
    startedAt: str | None = None
    lastEventAt: str | None = None


class SessionLogsResponse(BaseModel):
    """특정 세션의 실행 로그 상세 조회 응답 스키마."""

    sessionId: str
    events: list[dict[str, Any]] = Field(default_factory=list)


class AnomalyFindingItem(BaseModel):
    """규칙 기반 이상 징후 탐지 항목 스키마."""

    rule: Literal["REPEATED_IDENTICAL_CALL", "EXCESSIVE_REPLAN", "UNPLANNED_TOOL_CALL"]
    severity: Literal["WARN", "HIGH"]
    summary: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class SessionAnomalyResponse(BaseModel):
    """특정 세션의 이상 징후 분석 결과 응답 스키마."""

    sessionId: str
    status: Literal["NORMAL", "ANOMALY", "UNDETERMINED"]
    findings: list[AnomalyFindingItem] = Field(default_factory=list)
    diagnosis: str | None = None
    llmCalls: int = 0
    evaluatedEventCount: int = 0


class ManagedPlanCreate(BaseModel):
    """플래너 작업 계획 생성 요청 스키마."""

    planId: str | None = None
    incidentCode: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    serviceName: str = Field(default="payment-service", min_length=1)
    planDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    startTime: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    endTime: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    actionCommand: str | None = None
    evidenceChunkIds: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def validate_time_order(self) -> ManagedPlanCreate:
        """시작 시각은 종료 시각보다 이전이어야 한다."""
        if self.startTime >= self.endTime:
            raise ValueError("startTime은 endTime보다 이전이어야 합니다.")
        return self


class ManagedPlanUpdate(BaseModel):
    """플래너 작업 계획 수정 요청 스키마."""

    title: str | None = None
    planDate: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    startTime: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    endTime: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    actionCommand: str | None = None
    notes: str | None = None


class ManagedPlanItem(BaseModel):
    """저장된 플래너 작업 계획 항목 스키마."""

    planId: str
    incidentCode: str
    title: str
    serviceName: str
    planDate: str
    startTime: str
    endTime: str
    actionCommand: str | None = None
    evidenceChunkIds: list[str] = Field(default_factory=list)
    notes: str = ""
    createdAt: str
    updatedAt: str


class ManagedPlanMutationResponse(BaseModel):
    """계획 저장·수정 응답 (같은 날짜 시간 겹침 경고 포함)."""

    plan: ManagedPlanItem
    overlappingPlans: list[ManagedPlanItem] = Field(default_factory=list)
    warning: str | None = None
