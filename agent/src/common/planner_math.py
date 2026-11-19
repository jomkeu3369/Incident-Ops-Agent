from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class TimeWindow:
    """RFC 3339 반개구간 [start, end) 시간 창."""

    start: datetime
    end: datetime

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True)
class CandidateEvaluation:
    """후보 버전 502 비율 비교 결과."""

    selected: dict[str, Any] | None
    rejected: list[dict[str, Any]]
    threshold_percent: float


@dataclass(frozen=True)
class WorkWindowDecision:
    """변경 금지 구간을 반영한 작업 창 계산·재계획 결과."""

    selected_window: TimeWindow | None
    previous_window: TimeWindow | None
    replanned: bool
    reason: str | None
    remaining_gaps: list[TimeWindow]


def parse_rfc3339(value: str | datetime) -> datetime:
    """시간대 오프셋이 포함된 RFC 3339 시각을 파싱한다."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    else:
        raise ValueError("시각 문자열이 비어 있습니다.")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("RFC 3339 시간대 오프셋이 필요합니다.")
    return dt


def select_rollback_candidate(candidate_metrics: list[dict[str, Any]], *, max_502_rate_percent: float = 1.0) -> CandidateEvaluation:
    """502 비율이 기준 미만인 첫 번째 롤백 후보를 선택하고 제외 사유를 기록한다."""
    selected: dict[str, Any] | None = None
    rejected: list[dict[str, Any]] = []

    for item in candidate_metrics:
        sha = item.get("gitSha", "unknown")
        rate = item.get("http502RatePercent")
        if rate is None:
            rejected.append({"gitSha": sha, "reason": "502 지표가 부족해 판단할 수 없습니다."})
            continue
        if float(rate) < max_502_rate_percent:
            if selected is None:
                selected = item
        else:
            rejected.append({"gitSha": sha, "http502RatePercent": float(rate), "reason": f"502 비율({float(rate):.2f}%)이 기준({max_502_rate_percent:.2f}%) 이상입니다."})

    return CandidateEvaluation(selected=selected, rejected=rejected, threshold_percent=max_502_rate_percent)


def _overlaps(a: TimeWindow, b: TimeWindow) -> bool:
    """반개구간 [start, end) 기준 시간 겹침 여부를 판정한다."""
    return a.start < b.end and b.start < a.end


def _merge_windows(windows: list[TimeWindow]) -> list[TimeWindow]:
    """인접하거나 겹치는 변경 금지 구간을 하나로 병합한다."""
    if not windows:
        return []
    ordered = sorted(windows, key=lambda w: (w.start, w.end))
    merged = [ordered[0]]
    for cur in ordered[1:]:
        prev = merged[-1]
        if cur.start <= prev.end:
            merged[-1] = TimeWindow(prev.start, max(prev.end, cur.end))
        else:
            merged.append(cur)
    return merged


def _compute_free_gaps(allowed: TimeWindow, freezes: list[TimeWindow]) -> list[TimeWindow]:
    """허용 범위에서 변경 금지 구간을 제외한 가용 시간대 목록을 계산한다."""
    clipped = [TimeWindow(max(allowed.start, f.start), min(allowed.end, f.end)) for f in freezes if _overlaps(allowed, f)]
    merged = _merge_windows(clipped)
    gaps: list[TimeWindow] = []
    cursor = allowed.start
    for busy in merged:
        if cursor < busy.start:
            gaps.append(TimeWindow(cursor, busy.start))
        cursor = max(cursor, busy.end)
    if cursor < allowed.end:
        gaps.append(TimeWindow(cursor, allowed.end))
    return gaps


def find_work_window(allowed_start: str | datetime, allowed_end: str | datetime, duration_minutes: int, freeze_windows: list[dict[str, str] | TimeWindow]) -> WorkWindowDecision:
    """조회된 변경 금지 구간을 재사용해 겹치지 않는 가장 빠른 작업 창을 계산한다."""
    if duration_minutes <= 0:
        raise ValueError("작업 시간(분)은 1 이상이어야 합니다.")

    start_dt, end_dt = parse_rfc3339(allowed_start), parse_rfc3339(allowed_end)
    if start_dt >= end_dt:
        raise ValueError("허용 시작 시각은 종료 시각보다 이전이어야 합니다.")

    duration = timedelta(minutes=duration_minutes)
    allowed = TimeWindow(start_dt, end_dt)
    freezes = [w if isinstance(w, TimeWindow) else TimeWindow(parse_rfc3339(w["start"]), parse_rfc3339(w["end"])) for w in freeze_windows]
    gaps = _compute_free_gaps(allowed, freezes)

    if start_dt + duration > end_dt:
        return WorkWindowDecision(selected_window=None, previous_window=None, replanned=False, reason="허용 시간 범위가 필요한 작업 시간보다 짧습니다.", remaining_gaps=gaps)

    draft = TimeWindow(start_dt, start_dt + duration)
    if not any(_overlaps(draft, f) for f in freezes):
        return WorkWindowDecision(selected_window=draft, previous_window=None, replanned=False, reason=None, remaining_gaps=gaps)

    # 초안 작업 창이 변경 금지 구간과 겹치면 가용 시간대에서 가장 이른 슬롯으로 재계획한다
    for gap in gaps:
        if gap.end - gap.start >= duration:
            return WorkWindowDecision(selected_window=TimeWindow(gap.start, gap.start + duration), previous_window=draft, replanned=True, reason="변경 금지 시간과 겹칩니다.", remaining_gaps=gaps)

    return WorkWindowDecision(selected_window=None, previous_window=draft, replanned=True, reason="허용 범위 내에 변경 금지 시간과 겹치지 않는 작업 창이 없습니다.", remaining_gaps=gaps)
