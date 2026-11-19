from __future__ import annotations

from typing import Any

from common.backend_client import BackendApiError, BackendClient
from common.config import Settings
from log_agent.cruds import CorpusCrud
from log_agent.schemas import EvidenceItem, LogInvestigationRequest, LogInvestigationResponse

# 로그 우선순위 산정 시 가중치를 부여할 핵심 장애 키워드
_PRIORITY_TOKENS = ("hikaripool", "timeout", "connection", "leak", "502", "rollback", "deploy", "error", "커넥션", "누수", "롤백", "배포")


def _score_chunk(meta: dict[str, Any], user_keywords: list[str]) -> tuple[int, int]:
    """사용자 키워드와 핵심 장애 토큰 일치 수를 기준으로 정렬 키를 계산한다."""
    haystack = " ".join(str(meta.get(f) or "") for f in ("title", "keywords", "chunkId")).lower()
    score = sum(3 for kw in user_keywords if kw and kw.lower() in haystack)
    score += sum(1 for tok in _PRIORITY_TOKENS if tok in haystack)
    return (-score, int(meta.get("sequenceOrder") or 0))


def _summarize_chunk(content: str, max_chars: int) -> str:
    """인출한 원문을 지정 길이 이하로 정제해 컨텍스트 폭발을 방지한다."""
    cleaned = " ".join(line.strip() for line in content.splitlines() if line.strip())
    return cleaned if len(cleaned) <= max_chars else cleaned[: max(1, max_chars - 3)].rstrip() + "..."


def _extract_command(text: str) -> str | None:
    """런북 본문에서 실행 가능한 복구 명령어 후보를 추출한다."""
    for line in text.splitlines():
        stripped = line.strip().strip("`")
        if stripped.startswith(("kubectl ", "helm ", "systemctl ")):
            return stripped
    return None


async def run_dr_dci_investigation(backend: BackendClient, settings: Settings, req: LogInvestigationRequest) -> LogInvestigationResponse:
    """DR-DCI 루프: 메타데이터 색인 조회 -> 우선순위 선별 인출 -> 근거 요약 반환."""
    crud = CorpusCrud(backend)
    kw_list = [k.strip() for k in req.keywords if k and k.strip()]
    excluded = set(req.excludeChunkIds)
    budget = min(req.maxPulls or settings.dr_dci_max_pull_steps, settings.dr_dci_max_pull_steps)
    max_chars = max(120, settings.dr_dci_max_chunk_chars)
    tool_trace: list[dict[str, Any]] = []

    try:
        # 1단계: 메타데이터 색인으로 후보 청크만 좁힌다
        log_index: list[dict[str, Any]] = []
        primary_kw = kw_list[0] if kw_list else None
        if primary_kw:
            log_index = await crud.search_index(req.incidentCode, "LOG", primary_kw)
            tool_trace.append({"tool": "searchCorpusIndex", "args": {"incidentCode": req.incidentCode, "corpusType": "LOG", "keyword": primary_kw}, "count": len(log_index)})
        if not log_index:
            log_index = await crud.search_index(req.incidentCode, "LOG", None)
            tool_trace.append({"tool": "searchCorpusIndex", "args": {"incidentCode": req.incidentCode, "corpusType": "LOG", "keyword": None}, "count": len(log_index)})

        runbook_index = await crud.search_index(req.incidentCode, "RUNBOOK", None)
        tool_trace.append({"tool": "searchCorpusIndex", "args": {"incidentCode": req.incidentCode, "corpusType": "RUNBOOK", "keyword": None}, "count": len(runbook_index)})
    except BackendApiError as exc:
        return LogInvestigationResponse(ok=False, incidentCode=req.incidentCode, toolTrace=tool_trace, error={"code": "BACKEND_ERROR", "status": exc.status_code, "message": exc.message})

    ranked_logs = sorted([m for m in log_index if m.get("chunkId") and m["chunkId"] not in excluded], key=lambda m: _score_chunk(m, kw_list))
    ranked_runbooks = sorted([m for m in runbook_index if m.get("chunkId") and m["chunkId"] not in excluded], key=lambda m: _score_chunk(m, kw_list))

    # 런북이 있으면 최소 1개 슬롯을 런북 인출에 배정한다
    targets: list[dict[str, Any]] = []
    if ranked_runbooks and budget > 0:
        targets.extend(ranked_logs[: max(0, budget - 1)])
        targets.append(ranked_runbooks[0])
        if len(targets) < budget:
            targets.extend(ranked_runbooks[1 : 1 + (budget - len(targets))])
    else:
        targets.extend(ranked_logs[:budget])

    target_ids = {t.get("chunkId") for t in targets}
    remaining_ids = [str(m["chunkId"]) for m in (ranked_logs + ranked_runbooks) if m.get("chunkId") not in target_ids]
    evidence: list[EvidenceItem] = []
    runbook_summaries: list[str] = []
    recommended_command: str | None = None

    # 2단계: 선별한 청크만 단건 인출 후 요약만 남기고 원문은 폐기한다
    for meta in targets:
        cid = str(meta["chunkId"])
        try:
            chunk = await crud.pull_chunk(req.incidentCode, cid)
        except BackendApiError as exc:
            tool_trace.append({"tool": "pullCorpusChunk", "args": {"incidentCode": req.incidentCode, "chunkId": cid}, "ok": False, "error": exc.message})
            continue

        raw_content = str(chunk.get("content") or "")
        corpus_type = str(chunk.get("corpusType") or meta.get("corpusType") or "LOG")
        tool_trace.append({"tool": "pullCorpusChunk", "args": {"incidentCode": req.incidentCode, "chunkId": cid}, "ok": True, "chars": len(raw_content)})
        evidence.append(EvidenceItem(chunkId=cid, corpusType=corpus_type, title=str(chunk.get("title") or meta.get("title") or cid), summary=_summarize_chunk(raw_content, min(max_chars, 400))))
        if corpus_type == "RUNBOOK":
            runbook_summaries.append(_summarize_chunk(raw_content, max_chars))
            if recommended_command is None:
                recommended_command = _extract_command(raw_content)

    return LogInvestigationResponse(
        ok=True,
        incidentCode=req.incidentCode,
        evidence=evidence,
        evidenceChunkIds=[item.chunkId for item in evidence],
        runbookSummary="\n".join(runbook_summaries),
        recommendedCommand=recommended_command,
        remainingChunkIds=remaining_ids,
        pulledCount=len(evidence),
        toolTrace=tool_trace,
    )
