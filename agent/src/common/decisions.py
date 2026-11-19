from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal
from openai import APIConnectionError, APIStatusError, OpenAI

from common.config import Settings

Verdict = Literal["SUPPORTED", "NEED_MORE_EVIDENCE", "CONTRADICTED", "UNDECIDABLE"]
_VALID_VERDICTS = {"SUPPORTED", "NEED_MORE_EVIDENCE", "CONTRADICTED"}


@dataclass(frozen=True)
class DecisionResult:
    """OpenAI Decisions API 근거 판정 결과."""

    verdict: Verdict
    confidence: float | None
    probabilities: dict[str, float]
    provider: str
    model: str
    input_tokens: int | None = None
    reason: str | None = None


class DecisionsClient:
    """OpenAI Decisions API(gpt-6-luna choice) 판정 어댑터."""

    def __init__(self, settings: Settings, openai_client: Any = None) -> None:
        self._settings = settings
        self._client = openai_client

    async def judge_plan_evidence(self, *, proposed_action: str, runbook: str, evidence: str) -> DecisionResult:
        """수집된 근거가 런북 조건상 제안 조치를 뒷받침하는지 판정한다."""
        model = self._settings.decisions_model
        api_key = self._settings.openai_api_key.get_secret_value().strip()
        if self._client is None and (not api_key or api_key == "your-openai-api-key-here"):
            return DecisionResult(verdict="UNDECIDABLE", confidence=None, probabilities={}, provider="openai", model=model, reason="OPENAI_API_KEY가 설정되지 않아 판정 API를 호출할 수 없습니다.")

        return await asyncio.to_thread(self._call_sync, api_key=api_key, model=model, proposed_action=proposed_action, runbook=runbook, evidence=evidence)

    def _call_sync(self, *, api_key: str, model: str, proposed_action: str, runbook: str, evidence: str) -> DecisionResult:
        """동기 OpenAI Decisions SDK 호출을 안전하게 감싸 판정값으로 변환한다."""
        payload_input = f"Proposed action: {proposed_action}\nRunbook: {runbook}\nEvidence: {evidence}"
        questions = [
            {
                "type": "choice",
                "name": "evidence_support",
                "instructions": "Which choice best describes whether the evidence meets the runbook conditions for proposing this remediation action?",
                "choices": [
                    {"value": "SUPPORTED", "description": "The evidence meets the runbook conditions."},
                    {"value": "NEED_MORE_EVIDENCE", "description": "Information needed to apply the runbook is missing."},
                    {"value": "CONTRADICTED", "description": "The evidence conflicts with the runbook conditions."},
                ],
            }
        ]

        try:
            if self._client is not None:
                decision = self._client.decisions.create(model=model, input=payload_input, questions=questions)
            else:
                with OpenAI(api_key=api_key, base_url="https://api.openai.com/v1", timeout=self._settings.http_timeout_seconds, max_retries=0) as client:
                    decision = client.decisions.create(model=model, input=payload_input, questions=questions)
        except (APIConnectionError, APIStatusError, Exception):
            # 판정 API 장애 시 예외를 전파하지 않고 운영자 확인이 필요한 판정 불가로 처리한다
            return DecisionResult(verdict="UNDECIDABLE", confidence=None, probabilities={}, provider="openai", model=model, reason="판정 API 호출에 실패해 운영자 확인이 필요합니다.")

        answers = getattr(decision, "answers", None) or []
        if not answers or getattr(answers[0], "type", None) != "choice":
            return DecisionResult(verdict="UNDECIDABLE", confidence=None, probabilities={}, provider="openai", model=model, reason="판정 API가 유효한 선택지를 반환하지 않았습니다.")

        answer = answers[0]
        choice = str(getattr(answer, "choice", ""))
        if choice not in _VALID_VERDICTS:
            return DecisionResult(verdict="UNDECIDABLE", confidence=None, probabilities={}, provider="openai", model=model, reason="알 수 없는 판정 값이 반환되었습니다.")

        probs = {str(opt.value): float(opt.probability) for opt in (getattr(answer, "probabilities", None) or [])}
        usage = getattr(decision, "usage", None)
        return DecisionResult(
            verdict=choice,  # type: ignore[arg-type]
            confidence=float(answer.confidence) if getattr(answer, "confidence", None) is not None else None,
            probabilities=probs,
            provider="openai",
            model=model,
            input_tokens=int(usage.input_tokens) if getattr(usage, "input_tokens", None) is not None else None,
        )
