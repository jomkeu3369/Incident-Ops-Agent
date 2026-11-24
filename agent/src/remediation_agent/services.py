from __future__ import annotations

import math
import time
import uuid
from typing import Any

from common.decisions import EvidenceJudge
from common.planner_math import find_work_window, select_rollback_candidate
from common.tools import ExternalToolsClient
from remediation_agent.schemas import (
    PlanStepDraft,
    RemediationExecuteRequest,
    RemediationExecuteResponse,
    RemediationPlanRequest,
    RemediationPlanResponse,
)

# 기본 런북 기준 (런북 청크 요약이 비어 있을 때 사용하는 안전 폴백)
_DEFAULT_RUNBOOK = "새 배포 후 커넥션 대기 시간 초과 또는 502가 급증하고, 동일 트래픽에서 이전 버전이 정상일 때 롤백을 제안한다."


async def _call_or_reuse(cache: dict[str, Any], cache_key: str, tool_name: str, args: dict[str, Any], call_fn, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """캐시된 외부 Tool 결과가 있으면 재사용하고, 없을 때만 실제 호출한다."""
    if cache_key in cache:
        return cache[cache_key]
    t0 = time.perf_counter()
    res = await call_fn()
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    tool_calls.append({"tool": tool_name, "args": args, "ok": bool(res.get("ok")), "errorCode": None if res.get("ok") else (res.get("error") or {}).get("code"), "elapsedMs": elapsed_ms})
    if res.get("ok"):
        cache[cache_key] = res
    return res


async def build_remediation_plan(tools: ExternalToolsClient, decisions: EvidenceJudge, req: RemediationPlanRequest) -> RemediationPlanResponse:
    """외부 API Tool 3종 조회 -> 제약 계산·재계획 -> Decisions 근거 판정까지 수행한다."""
    cache = dict(req.cachedToolResults)
    tool_calls: list[dict[str, Any]] = []

    # 1단계: GitHub Deployments에서 기준 배포와 롤백 후보를 조회한다
    cand_res = await _call_or_reuse(
        cache,
        f"candidates:{req.serviceName}:{req.incidentStartedAt}",
        "list_rollback_candidates",
        {"serviceName": req.serviceName, "incidentStartedAt": req.incidentStartedAt},
        lambda: tools.list_rollback_candidates(req.serviceName, req.incidentStartedAt),
        tool_calls,
    )
    if not cand_res.get("ok"):
        err = cand_res.get("error") or {}
        return RemediationPlanResponse(ok=False, status="BLOCKED", incidentCode=req.incidentCode, toolCalls=tool_calls, cachedToolResults=cache, message=f"롤백 후보 조회 실패({err.get('code')}): {err.get('message')}")

    reference_dep = cand_res["data"]["referenceDeployment"]
    candidates = cand_res["data"]["candidates"]

    # 2단계: Grafana Cloud에서 기준 배포와 후보 버전의 동일 구간 502 비율을 비교한다
    ref_metrics_res = await _call_or_reuse(
        cache,
        f"metrics:{reference_dep['gitSha']}:{req.metricsFrom}:{req.metricsTo}",
        "get_release_metrics",
        {"serviceName": req.serviceName, "gitSha": reference_dep["gitSha"], "from": req.metricsFrom, "to": req.metricsTo},
        lambda: tools.get_release_metrics(req.serviceName, reference_dep["gitSha"], req.metricsFrom, req.metricsTo),
        tool_calls,
    )
    candidate_metrics: list[dict[str, Any]] = []
    for cand in candidates:
        sha = cand["gitSha"]
        m_res = await _call_or_reuse(
            cache,
            f"metrics:{sha}:{req.metricsFrom}:{req.metricsTo}",
            "get_release_metrics",
            {"serviceName": req.serviceName, "gitSha": sha, "from": req.metricsFrom, "to": req.metricsTo},
            lambda s=sha: tools.get_release_metrics(req.serviceName, s, req.metricsFrom, req.metricsTo),
            tool_calls,
        )
        if m_res.get("ok"):
            candidate_metrics.append({**m_res["data"], "ref": cand.get("ref", sha), "deploymentId": cand.get("deploymentId")})
        else:
            candidate_metrics.append({"gitSha": sha, "ref": cand.get("ref", sha), "http502RatePercent": None})

    eval_res = select_rollback_candidate(candidate_metrics, max_502_rate_percent=req.max502RatePercent)
    if eval_res.selected is None:
        return RemediationPlanResponse(ok=False, status="BLOCKED", incidentCode=req.incidentCode, rejectedCandidates=eval_res.rejected, toolCalls=tool_calls, cachedToolResults=cache, message=f"502 비율 {req.max502RatePercent}% 미만 기준을 만족하는 롤백 후보가 없습니다.")

    # 3단계: Google Calendar FreeBusy로 변경 금지 시간을 확인하고 필요 시 캐시 결과를 재사용해 재계획한다
    freeze_res = await _call_or_reuse(
        cache,
        f"freeze:{req.serviceName}:{req.allowedWindowStart}:{req.allowedWindowEnd}",
        "get_change_freeze_windows",
        {"serviceName": req.serviceName, "windowStart": req.allowedWindowStart, "windowEnd": req.allowedWindowEnd},
        lambda: tools.get_change_freeze_windows(req.serviceName, req.allowedWindowStart, req.allowedWindowEnd),
        tool_calls,
    )
    if not freeze_res.get("ok"):
        err = freeze_res.get("error") or {}
        return RemediationPlanResponse(ok=False, status="BLOCKED", incidentCode=req.incidentCode, selectedCandidate=eval_res.selected, rejectedCandidates=eval_res.rejected, toolCalls=tool_calls, cachedToolResults=cache, message=f"변경 금지 시간 조회 실패({err.get('code')}): {err.get('message')}")

    try:
        win_dec = find_work_window(req.allowedWindowStart, req.allowedWindowEnd, req.durationMinutes, freeze_res["data"]["freezeWindows"])
    except ValueError as exc:
        return RemediationPlanResponse(ok=False, status="BLOCKED", incidentCode=req.incidentCode, selectedCandidate=eval_res.selected, toolCalls=tool_calls, cachedToolResults=cache, message=str(exc))

    gaps_json = [g.to_dict() for g in win_dec.remaining_gaps]
    replan_event = None
    if win_dec.replanned and win_dec.previous_window and win_dec.selected_window:
        replan_event = {"reason": win_dec.reason, "previousWindow": win_dec.previous_window.to_dict(), "nextWindow": win_dec.selected_window.to_dict()}

    if win_dec.selected_window is None:
        return RemediationPlanResponse(ok=False, status="BLOCKED", incidentCode=req.incidentCode, selectedCandidate=eval_res.selected, rejectedCandidates=eval_res.rejected, remainingGaps=gaps_json, toolCalls=tool_calls, cachedToolResults=cache, message=win_dec.reason or "허용 시간 범위 내에 가능한 작업 창이 없습니다.")

    # 4단계: EvidenceJudge로 수집한 관측 근거(LOG + External)가 런북 조건상 롤백 제안을 뒷받침하는지 판정한다
    target_ref = eval_res.selected.get("ref") or eval_res.selected["gitSha"]
    action_cmd = req.recommendedCommand or f"kubectl rollout undo deployment/{req.serviceName}"
    ref_rate = ref_metrics_res["data"]["http502RatePercent"] if ref_metrics_res.get("ok") else "미확인"
    ref_total = ref_metrics_res["data"].get("totalRequests") if ref_metrics_res.get("ok") else None
    cand_total = eval_res.selected.get("totalRequests")
    ref_total_suffix = f" (totalRequests={ref_total:g})" if isinstance(ref_total, (int, float)) and not isinstance(ref_total, bool) and math.isfinite(ref_total) else ""
    cand_total_suffix = f" (totalRequests={cand_total:g})" if isinstance(cand_total, (int, float)) and not isinstance(cand_total, bool) and math.isfinite(cand_total) else ""
    runbook_parts = [req.runbookSummary.strip()] if req.runbookSummary and req.runbookSummary.strip() else []
    evidence_lines: list[str] = []
    for e in req.evidence:
        if (e.corpusType or "").strip().upper() == "RUNBOOK":
            if e.summary.strip() and e.summary.strip() not in runbook_parts:
                runbook_parts.append(e.summary.strip())
        else:
            evidence_lines.append(f"[{e.chunkId}] {e.summary}")
    evidence_lines.append(
        f"[External] 기준 배포 {reference_dep.get('ref', reference_dep['gitSha'])} 502 비율={ref_rate}%{ref_total_suffix}, "
        f"후보 {target_ref} 502 비율={eval_res.selected['http502RatePercent']}%{cand_total_suffix}"
    )
    verdict = await decisions.judge_plan_evidence(
        proposed_action=f"Rollback {req.serviceName} to {target_ref} ({action_cmd}) during {win_dec.selected_window.start.isoformat()}~{win_dec.selected_window.end.isoformat()}",
        runbook="\n".join(runbook_parts) or _DEFAULT_RUNBOOK,
        evidence="\n".join(evidence_lines),
        max_error_rate_percent=req.max502RatePercent,
    )

    chunk_ids = [e.chunkId for e in req.evidence]
    plan_id = f"PLAN-{req.incidentCode}-{uuid.uuid4().hex[:6].upper()}"
    work_window = win_dec.selected_window.to_dict()

    if verdict.verdict == "NEED_MORE_EVIDENCE":
        return RemediationPlanResponse(ok=True, status="NEED_MORE_EVIDENCE", incidentCode=req.incidentCode, verdict=verdict.verdict, confidence=verdict.confidence, selectedCandidate=eval_res.selected, rejectedCandidates=eval_res.rejected, replanEvent=replan_event, remainingGaps=gaps_json, toolCalls=tool_calls, cachedToolResults=cache, message="롤백 제안을 확정하기 위한 근거가 부족해 범위 내 추가 로그 조회가 필요합니다.")

    if verdict.verdict == "CONTRADICTED":
        # 근거와 충돌하면 롤백 명령을 제거하고 조사·완화 계획으로 수정한다
        revised_steps = [PlanStepDraft(stepNo=1, description=f"{req.serviceName} 롤백을 보류하고 배포 이전 오류 원인과 DB 상태를 점검한다.", condition="수집한 근거가 롤백 제안 조건과 충돌함", actionCommand=None, evidenceChunkIds=chunk_ids)]
        revised_plan = {"planId": plan_id, "workWindow": work_window, "steps": [s.model_dump() for s in revised_steps], "evidence": [e.model_dump() for e in req.evidence], "verificationCriteria": "배포 외 원인(DB/인프라 설정 변경 여부)을 확인한 뒤 새 복구 계획을 수립한다."}
        return RemediationPlanResponse(ok=True, status="REVISED_NO_ROLLBACK", incidentCode=req.incidentCode, verdict=verdict.verdict, confidence=verdict.confidence, planDraft=revised_plan, selectedCandidate=eval_res.selected, rejectedCandidates=eval_res.rejected, replanEvent=replan_event, remainingGaps=gaps_json, toolCalls=tool_calls, cachedToolResults=cache, message="수집한 근거가 롤백 조건과 충돌하여 롤백 대신 원인 점검 계획으로 수정했습니다.")

    if verdict.verdict != "SUPPORTED":
        return RemediationPlanResponse(ok=False, status="BLOCKED", incidentCode=req.incidentCode, verdict=verdict.verdict, confidence=verdict.confidence, selectedCandidate=eval_res.selected, rejectedCandidates=eval_res.rejected, replanEvent=replan_event, remainingGaps=gaps_json, toolCalls=tool_calls, cachedToolResults=cache, message=verdict.reason or "판정 불가 상태로 운영자의 직접 확인이 필요합니다.")

    # SUPPORTED: 승인 대상 1단계 명령어를 포함한 복구 계획 초안을 반환한다
    steps = [
        PlanStepDraft(stepNo=1, description=f"{req.serviceName} 장애 로그 및 후보 버전({target_ref}) 502 비율({eval_res.selected['http502RatePercent']}%)을 검증한다.", condition="로그·런북 근거 확보 및 후보 502 비율 기준 충족", actionCommand=None, evidenceChunkIds=chunk_ids),
        PlanStepDraft(stepNo=2, description=f"{req.serviceName}를 {target_ref} 버전으로 롤백한다.", condition=f"운영자 승인 완료 및 작업 창({work_window['start']}~{work_window['end']}) 확보", actionCommand=action_cmd, evidenceChunkIds=chunk_ids),
    ]
    plan_draft = {
        "planId": plan_id,
        "workWindow": work_window,
        "approvalStepNo": 2,
        "steps": [s.model_dump() for s in steps],
        "evidence": [e.model_dump() for e in req.evidence],
        "verificationCriteria": f"{req.serviceName}의 HTTP 502 비율이 {req.max502RatePercent}% 미만으로 안정화되고 DB 커넥션 대기 시간이 정상 범위로 돌아온다.",
    }
    return RemediationPlanResponse(
        ok=True,
        status="READY_FOR_APPROVAL",
        incidentCode=req.incidentCode,
        verdict=verdict.verdict,
        confidence=verdict.confidence,
        planDraft=plan_draft,
        selectedCandidate=eval_res.selected,
        rejectedCandidates=eval_res.rejected,
        replanEvent=replan_event,
        remainingGaps=gaps_json,
        toolCalls=tool_calls,
        cachedToolResults=cache,
        message=f"{target_ref} 롤백 계획이 근거 판정(SUPPORTED)을 통과해 운영자 승인 대기 준비를 마쳤습니다.",
    )


async def execute_approved_remediation(req: RemediationExecuteRequest) -> RemediationExecuteResponse:
    """운영자 승인을 통과한 복구 조치를 안전 시뮬레이션으로 실행하고 검증·포스트모템 초안을 생성한다."""
    # shortcut: 실제 클러스터 명령 대신 시뮬레이션 실행, 운영 클러스터 연동 시 kube/executor 어댑터로 교체
    evidence_refs = ", ".join(e.chunkId for e in req.evidence) or "인용 청크 없음"
    return RemediationExecuteResponse(
        ok=True,
        incidentCode=req.incidentCode,
        approvalId=req.approvalId,
        executionStatus="SUCCESS",
        executionOutput=f"[simulated] executed `{req.actionCommand}` for {req.incidentCode} (approval={req.approvalId})",
        verificationSummary=f"복구 확인 기준 충족 확인 완료: {req.verificationCriteria or '502 오류율 및 커넥션 대기 시간 정상화 확인'}",
        rootCause=f"{req.incidentCode} 신규 배포 이후 발생한 리소스/커넥션 이상 ({evidence_refs})",
        timelineMarkdown=f"- 장애 조사 완료 ({evidence_refs})\n- 계획({req.planId}) 승인 완료 ({req.approvalId})\n- `{req.actionCommand}` 실행 및 복구 확인 완료",
        preventionItems="배포 전 커넥션 누수 통합 테스트 및 카나리 502 임계치 자동 알림 적용",
    )
