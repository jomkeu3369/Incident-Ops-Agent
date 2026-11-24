from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Awaitable, Callable, Literal

from openai import AsyncOpenAI

from common.config import Settings

AnomalyStatus = Literal["NORMAL", "ANOMALY", "UNDETERMINED"]
DiagnosisFn = Callable[[str, list[dict[str, Any]], list[dict[str, Any]]], Awaitable[str]]

# A2A 워커 내부에서 정상적으로 호출될 수 있는 하위 외부 Tool 화이트리스트
_KNOWN_SUB_TOOLS = frozenset(
    {
        "list_rollback_candidates",
        "get_release_metrics",
        "get_change_freeze_windows",
    }
)


@dataclass(frozen=True)
class AnomalyFinding:
    """규칙 기반 이상 징후 탐지 결과 단건."""

    rule: Literal["REPEATED_IDENTICAL_CALL", "EXCESSIVE_REPLAN", "UNPLANNED_TOOL_CALL"]
    severity: Literal["WARN", "HIGH"]
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "summary": self.summary,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class AnomalyReport:
    """세션 실행 로그 이상 징후 판단 리포트."""

    session_id: str
    status: AnomalyStatus
    findings: list[AnomalyFinding]
    diagnosis: str | None
    llm_calls: int
    evaluated_event_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
            "diagnosis": self.diagnosis,
            "llmCalls": self.llm_calls,
            "evaluatedEventCount": self.evaluated_event_count,
        }


def _canonical_args(args: Any) -> str:
    """동일 입력 비교를 위해 도구 인자를 정렬된 정규 JSON 문자열로 직렬화한다."""
    if not isinstance(args, dict):
        return json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)


def _trim_to_three_sentences(text: str) -> str:
    """진단 응답을 최대 3문장으로 정규화한다."""
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?다요])\s+", cleaned) if s.strip()]
    return " ".join(sentences[:3])


def _fallback_three_sentence_diagnosis(findings: list[AnomalyFinding]) -> str:
    """LLM 미설정 또는 호출 실패 시 예외를 노출하지 않고 규칙 기반 3문장 진단을 반환한다."""
    rules = ", ".join(sorted({f.rule for f in findings}))
    first_summary = findings[0].summary if findings else "실행 흐름에서 비정상 패턴이 관측되었습니다."
    return (
        f"세션 실행 로그에서 이상 규칙({rules}) 위반이 총 {len(findings)}건 탐지되었습니다. "
        f"주요 징후는 {first_summary} 입니다. "
        "도구 입력 제약과 재계획 조건, 계획 단계의 허용 도구 목록을 점검해 재발을 방지하세요."
    )


def evaluate_session_rules(
    session_id: str,
    events: list[dict[str, Any]],
    *,
    repeat_threshold: int = 2,
    replan_threshold: int = 3,
) -> tuple[AnomalyStatus, list[AnomalyFinding]]:
    """실행 로그를 규칙 기반으로 판정한다(LLM 호출 0회, 근거 부재 시 UNDETERMINED)."""
    if not events:
        return "UNDETERMINED", []

    relevant_types = {"step_started", "step_finished", "tool_call", "replan", "llm_decision"}
    relevant = [e for e in events if isinstance(e, dict) and e.get("eventType") in relevant_types]
    if not relevant:
        return "UNDETERMINED", []

    findings: list[AnomalyFinding] = []
    planned_tools: set[str] = set()
    call_counts: dict[tuple[str, str], int] = {}
    replan_events = 0
    unplanned_calls: list[str] = []

    for ev in relevant:
        ev_type = ev.get("eventType")
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}

        if ev_type == "step_finished" and payload.get("step") == "plan":
            steps = payload.get("steps")
            if isinstance(steps, list):
                planned_tools.update(str(s) for s in steps if s)

        if ev_type == "replan":
            replan_events += 1
        elif ev_type == "llm_decision" and payload.get("decision") == "replan":
            replan_events += 1

        if ev_type == "tool_call":
            tool_name = str(payload.get("tool") or "").strip()
            if not tool_name:
                continue
            key = (tool_name, _canonical_args(payload.get("args", {})))
            call_counts[key] = call_counts.get(key, 0) + 1

            # 계획 단계에서 선언된 도구 목록이 존재할 때만 미계획 Tool 호출을 검사한다
            if planned_tools and tool_name not in planned_tools and tool_name not in _KNOWN_SUB_TOOLS:
                unplanned_calls.append(tool_name)

    for (tool_name, args_json), count in call_counts.items():
        if count >= repeat_threshold:
            findings.append(
                AnomalyFinding(
                    rule="REPEATED_IDENTICAL_CALL",
                    severity="HIGH" if count >= repeat_threshold + 1 else "WARN",
                    summary=f"도구 `{tool_name}`이 동일한 입력으로 {count}회 반복 호출되었습니다",
                    evidence={"tool": tool_name, "count": count, "args": args_json[:200]},
                )
            )

    if replan_events >= replan_threshold:
        findings.append(
            AnomalyFinding(
                rule="EXCESSIVE_REPLAN",
                severity="HIGH",
                summary=f"재계획이 {replan_events}회 발생해 임계치({replan_threshold}회)에 도달했습니다",
                evidence={"replanCount": replan_events, "threshold": replan_threshold},
            )
        )

    if unplanned_calls:
        unique_unplanned = sorted(set(unplanned_calls))
        findings.append(
            AnomalyFinding(
                rule="UNPLANNED_TOOL_CALL",
                severity="HIGH",
                summary=f"계획에 포함되지 않은 도구({', '.join(unique_unplanned)})가 호출되었습니다",
                evidence={"unplannedTools": unique_unplanned, "plannedTools": sorted(planned_tools)},
            )
        )

    if findings:
        return "ANOMALY", findings
    return "NORMAL", []


class SessionAnomalyAnalyzer:
    """규칙 기반 이상 징후 판정 후 이상 건에만 LLM 3문장 진단을 수행하는 분석기."""

    def __init__(
        self,
        settings: Settings | None = None,
        diagnosis_fn: DiagnosisFn | None = None,
        *,
        repeat_threshold: int = 2,
        replan_threshold: int = 3,
    ) -> None:
        self._settings = settings
        self._diagnosis_fn = diagnosis_fn
        self._repeat_threshold = repeat_threshold
        self._replan_threshold = replan_threshold

    async def analyze(self, session_id: str, events: list[dict[str, Any]]) -> AnomalyReport:
        """세션 로그를 평가하고, ANOMALY일 때만 LLM 원인 진단을 1회 수행한다."""
        status, findings = evaluate_session_rules(
            session_id,
            events,
            repeat_threshold=self._repeat_threshold,
            replan_threshold=self._replan_threshold,
        )
        if status != "ANOMALY":
            # 정상(NORMAL) 및 근거 부재(UNDETERMINED)에서는 LLM 호출 0회 보장
            return AnomalyReport(
                session_id=session_id,
                status=status,
                findings=findings,
                diagnosis=None,
                llm_calls=0,
                evaluated_event_count=len(events),
            )

        findings_payload = [f.to_dict() for f in findings]
        diagnosis, llm_calls = await self._diagnose_anomaly(session_id, findings, findings_payload, events)
        return AnomalyReport(
            session_id=session_id,
            status="ANOMALY",
            findings=findings,
            diagnosis=_trim_to_three_sentences(diagnosis),
            llm_calls=llm_calls,
            evaluated_event_count=len(events),
        )

    async def _diagnose_anomaly(
        self,
        session_id: str,
        findings: list[AnomalyFinding],
        findings_payload: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> tuple[str, int]:
        if self._diagnosis_fn is not None:
            try:
                text = await self._diagnosis_fn(session_id, findings_payload, events)
                if text and text.strip():
                    return text, 1
            except Exception:
                return _fallback_three_sentence_diagnosis(findings), 1
            return _fallback_three_sentence_diagnosis(findings), 1

        api_key = self._settings.openai_api_key.get_secret_value() if self._settings else ""
        if not api_key:
            return _fallback_three_sentence_diagnosis(findings), 0

        try:
            client = AsyncOpenAI(api_key=api_key, timeout=self._settings.http_timeout_seconds if self._settings else 15.0)
            model = self._settings.openai_model if self._settings else "gpt-4o-mini"
            prompt = (
                "다음은 장애 대응 에이전트 세션에서 규칙 기반으로 탐지된 이상 징후입니다. "
                "한국어로 정확히 3문장 이내의 원인 진단과 개선 방향을 작성하세요.\n"
                f"세션 ID: {session_id}\n"
                f"탐지 항목: {json.dumps(findings_payload, ensure_ascii=False)}"
            )
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "당신은 SRE 에이전트 실행 로그 감사 전문가입니다. 간결하게 3문장 이내로 답하세요."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=220,
                temperature=0.0,
            )
            content = (resp.choices[0].message.content or "").strip() if resp.choices else ""
            return (content or _fallback_three_sentence_diagnosis(findings)), 1
        except Exception:
            return _fallback_three_sentence_diagnosis(findings), 1
