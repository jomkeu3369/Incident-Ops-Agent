from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ExpectedVerdict = Literal["SUPPORTED", "NEED_MORE_EVIDENCE", "CONTRADICTED"]
IncidentCategory = Literal[
    "db_connection_pool",
    "memory_oom",
    "external_dependency",
    "schema_migration",
    "telemetry_gap",
    "adversarial_edge",
]


class IncidentEvalCase(BaseModel):
    """P0-1 근거 판정 및 P0-2 DR-DCI 공용 평가 케이스 스키마."""

    case_id: str = Field(min_length=1)
    category: IncidentCategory
    service_name: str = Field(min_length=1)
    expected_verdict: ExpectedVerdict
    proposed_action: str = Field(min_length=1)
    runbook: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    prompt_injection: bool = False
    tags: list[str] = Field(default_factory=list)


class EvalDataset(BaseModel):
    """버전 관리되는 다중 장애 평가 데이터셋 컨테이너."""

    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    cases: list[IncidentEvalCase] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def _ensure_unique_case_ids(cls, cases: list[IncidentEvalCase]) -> list[IncidentEvalCase]:
        seen: set[str] = set()
        for case in cases:
            if case.case_id in seen:
                raise ValueError(f"Duplicate case_id in evaluation dataset: {case.case_id}")
            seen.add(case.case_id)
        return cases


def default_dataset_path() -> Path:
    """기본 내장 다중 장애 평가 데이터셋 JSON 경로를 반환한다."""
    return Path(__file__).resolve().parent / "datasets" / "incident_evidence_v1.json"


def load_eval_dataset(path: str | Path | None = None) -> EvalDataset:
    """JSON 평가 데이터셋을 로드하고 Pydantic 스키마로 검증한다."""
    target = Path(path) if path is not None else default_dataset_path()
    raw = json.loads(target.read_text(encoding="utf-8"))
    return EvalDataset.model_validate(raw)
