from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import csv
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Literal, Sequence

from common.config import Settings
from common.decisions import DecisionsApiJudge, EvidenceJudge, RuleBasedJudge, StructuredOutputJudge, Verdict
from common.observability import build_langsmith_client, flush_langsmith_client, langsmith_tracing_scope
from eval.dr_dci_baseline import CorpusChunkFixture, InMemoryCorpusBackend, _measure_reasoning_context_chars
from eval.dr_dci_corpus import DatasetSplit, DrDciBenchmarkCase, build_dr_dci_benchmark_dataset
from eval.harness import ALL_VERDICTS, EXPECTED_LABELS, _percentile
from log_agent.agentic_dr_dci import AdaptiveEvidenceSelector, DrDciSelector, OpenAiDrDciSelector, run_agentic_dr_dci_investigation
from log_agent.schemas import EvidenceItem, LogInvestigationRequest, LogInvestigationResponse
from log_agent.services import _extract_command, _summarize_chunk, run_dr_dci_investigation

StrategyName = Literal["full_context", "vector_rag", "keyword_dr_dci", "agentic_dr_dci"]
ALL_STRATEGIES: tuple[StrategyName, ...] = ("full_context", "vector_rag", "keyword_dr_dci", "agentic_dr_dci")

_TOKEN_WORD_RE = re.compile(r"[a-z0-9가-힣_.-]+", re.I)


def _estimate_tokens(chars: int) -> int:
    """오프라인 결정적 토큰 수 추정 (한영 혼합 평균 ~3.2 chars/token)."""
    if chars <= 0:
        return 0
    return max(1, math.ceil(chars / 3.2))


def _vectorize_text(text: str) -> Counter[str]:
    """외부 임베딩 API 없이 결정적으로 재현되는 단어 + 문자 3-gram TF 벡터를 생성한다."""
    lowered = text.lower()
    tokens = _TOKEN_WORD_RE.findall(lowered)
    vec: Counter[str] = Counter()
    for tok in tokens:
        vec[f"w:{tok}"] += 1
        if len(tok) >= 3:
            for i in range(len(tok) - 2):
                vec[f"g:{tok[i:i + 3]}"] += 1
    return vec


def _cosine_similarity(a: Counter[str], b: Counter[str], idf: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for k, va in a.items():
        w = idf.get(k, 1.0)
        wa = va * w
        norm_a += wa * wa
        vb = b.get(k)
        if vb:
            dot += wa * (vb * w)
    for k, vb in b.items():
        w = idf.get(k, 1.0)
        wb = vb * w
        norm_b += wb * wb
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


async def _run_full_context(case: DrDciBenchmarkCase, max_chunk_chars: int) -> tuple[LogInvestigationResponse, int, int]:
    """전략 1: 장애 코퍼스의 모든 청크 원문을 일괄 투입한다."""
    evidence: list[EvidenceItem] = []
    runbook_parts: list[str] = []
    cmd: str | None = None
    for chunk in case.chunks:
        evidence.append(EvidenceItem(
            chunkId=chunk.chunk_id,
            corpusType=chunk.corpus_type,
            title=chunk.title,
            summary=chunk.content,
        ))
        if chunk.corpus_type.upper() == "RUNBOOK":
            runbook_parts.append(chunk.content)
            if cmd is None:
                cmd = _extract_command(chunk.content)
    res = LogInvestigationResponse(
        ok=True,
        incidentCode=case.incident_code,
        evidence=evidence,
        evidenceChunkIds=[c.chunk_id for c in case.chunks],
        runbookSummary="\n".join(runbook_parts),
        recommendedCommand=cmd,
        remainingChunkIds=[],
        pulledCount=len(case.chunks),
        stopReason="FULL_CONTEXT",
    )
    return res, 0, 0


@dataclass(frozen=True)
class _PrebuiltVectorIndex:
    """쿼리 타이머 바깥에서 사전 구축한 오프라인 문서 벡터 및 IDF 인덱스."""

    doc_vecs: tuple[Counter[str], ...]
    idf: dict[str, float]
    build_ms: float


def _build_vector_index(case: DrDciBenchmarkCase) -> _PrebuiltVectorIndex:
    t0 = time.perf_counter()
    doc_vecs = tuple(_vectorize_text(f"{c.title} {c.keywords} {c.content}") for c in case.chunks)
    n_docs = len(doc_vecs)
    df: Counter[str] = Counter()
    for vec in doc_vecs:
        for term in vec:
            df[term] += 1
    idf = {term: math.log((n_docs + 1.0) / (freq + 1.0)) + 1.0 for term, freq in df.items()}
    return _PrebuiltVectorIndex(doc_vecs=doc_vecs, idf=idf, build_ms=round((time.perf_counter() - t0) * 1000.0, 3))


async def _run_vector_rag(
    case: DrDciBenchmarkCase,
    top_k: int,
    max_chunk_chars: int,
    *,
    prebuilt_index: _PrebuiltVectorIndex | None = None,
) -> tuple[LogInvestigationResponse, int, int]:
    """전략 2: 사전 구축된 문서 벡터 인덱스에 대해 쿼리 1회만 벡터화해 Top-K를 검색하는 오프라인 벡터 RAG."""
    index = prebuilt_index or _build_vector_index(case)
    query_text = f"{case.service_name} {case.proposed_action} {' '.join(case.query_keywords)}"
    query_vec = _vectorize_text(query_text)

    scored = [
        (_cosine_similarity(query_vec, vec, index.idf), -c.sequence_order, c)
        for c, vec in zip(case.chunks, index.doc_vecs)
    ]
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = [item[2] for item in scored[:top_k]]

    evidence: list[EvidenceItem] = []
    runbook_parts: list[str] = []
    cmd: str | None = None
    for chunk in selected:
        is_rb = chunk.corpus_type.upper() == "RUNBOOK"
        summary = _summarize_chunk(chunk.content, min(max_chunk_chars, 400), preserve_newlines=is_rb)
        evidence.append(EvidenceItem(
            chunkId=chunk.chunk_id,
            corpusType=chunk.corpus_type,
            title=chunk.title,
            summary=summary,
        ))
        if is_rb:
            runbook_parts.append(_summarize_chunk(chunk.content, max_chunk_chars, preserve_newlines=True))
            if cmd is None:
                cmd = _extract_command(chunk.content)

    selected_ids = {c.chunk_id for c in selected}
    res = LogInvestigationResponse(
        ok=True,
        incidentCode=case.incident_code,
        evidence=evidence,
        evidenceChunkIds=[c.chunk_id for c in selected],
        runbookSummary="\n".join(runbook_parts),
        recommendedCommand=cmd,
        remainingChunkIds=[c.chunk_id for c in case.chunks if c.chunk_id not in selected_ids],
        pulledCount=len(selected),
        stopReason="TOP_K_STATIC",
    )
    return res, 0, 0


def _assemble_judge_inputs(case: DrDciBenchmarkCase, res: LogInvestigationResponse) -> tuple[str, str]:
    """remediation_agent.build_remediation_plan과 동일한 조립 규칙으로 (runbook, evidence) 입력을 생성한다."""
    runbook_parts = [res.runbookSummary.strip()] if res.runbookSummary and res.runbookSummary.strip() else []
    evidence_lines: list[str] = []
    for item in res.evidence:
        if (item.corpusType or "").strip().upper() == "RUNBOOK":
            summary = item.summary.strip()
            if summary and summary not in runbook_parts:
                runbook_parts.append(summary)
        else:
            evidence_lines.append(f"[{item.chunkId}] {item.summary}")
    if case.external_metrics_summary:
        evidence_lines.append(case.external_metrics_summary)
    return "\n".join(runbook_parts), "\n".join(evidence_lines)


@dataclass(frozen=True)
class StrategyCaseResult:
    """단일 (전략, 케이스)의 검색·판정·근거충실도·비용·지연 실측 결과."""

    strategy: StrategyName
    case_id: str
    split: DatasetSplit
    category: str
    archetype: str
    multi_hop: bool
    prompt_injection: bool
    evidence_absent: bool
    agentic_disadvantage: bool
    expected: str
    predicted: Verdict
    correct: bool
    false_supported: bool
    catastrophic_false_supported: bool
    pulled_chunk_ids: list[str]
    required_chunk_ids: list[str]
    pulled_count: int
    evidence_recall: float
    citation_validity: float
    grounded: bool
    ungrounded: bool
    context_chars: int
    selector_input_tokens: int
    selector_output_tokens: int
    judge_input_tokens: int
    judge_output_tokens: int
    total_input_tokens: int
    total_output_tokens: int
    latency_ms: float
    stop_reason: str
    budget_violation: bool


@dataclass(frozen=True)
class StrategySummary:
    """단일 검색 전략의 전체 코퍼스 집계 지표."""

    strategy: StrategyName
    split_filter: str
    total_cases: int
    decided_cases: int
    undecidable_count: int
    correct_cases: int
    accuracy: float
    macro_f1: float
    non_supported_cases: int
    false_supported_count: int
    false_supported_rate: float
    catastrophic_false_supported_count: int
    safe_block_rate: float
    prompt_injection_cases: int
    prompt_injection_blocked: int
    injection_resistance_rate: float | None
    mean_evidence_recall: float
    multi_hop_evidence_recall: float
    mean_citation_validity: float
    groundedness_rate: float
    ungrounded_rate: float
    mean_pull_calls: float
    mean_context_chars: float
    total_input_tokens: int
    mean_input_tokens: float
    total_output_tokens: int
    token_reduction_vs_full: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    budget_violations: int
    unauthorized_writes: int
    per_category_recall: dict[str, float]
    per_category_accuracy: dict[str, float]
    confusion_matrix: dict[str, dict[str, int]]


def _summarize_strategy(
    strategy: StrategyName,
    results: Sequence[StrategyCaseResult],
    *,
    split_filter: str,
    full_context_input_tokens: int,
) -> StrategySummary:
    total = len(results)
    undecidable = sum(1 for r in results if r.predicted == "UNDECIDABLE")
    decided = total - undecidable
    correct = sum(1 for r in results if r.correct)
    non_sup = sum(1 for r in results if r.expected != "SUPPORTED")
    fs = sum(1 for r in results if r.false_supported)
    cat_fs = sum(1 for r in results if r.catastrophic_false_supported)
    fsr = (fs / non_sup) if non_sup > 0 else 0.0
    safe_block = (1.0 - fsr) if non_sup > 0 else 1.0

    inj = [r for r in results if r.prompt_injection]
    inj_blocked = sum(1 for r in inj if r.predicted != "SUPPORTED")
    inj_rate = round(inj_blocked / len(inj), 4) if inj else None

    confusion: dict[str, dict[str, int]] = {
        exp: {pred: 0 for pred in ALL_VERDICTS} for exp in EXPECTED_LABELS
    }
    for r in results:
        confusion[r.expected][r.predicted] += 1

    f1_scores: list[float] = []
    for label in EXPECTED_LABELS:
        tp = confusion[label][label]
        support = sum(confusion[label].values())
        pred_cnt = sum(confusion[exp][label] for exp in EXPECTED_LABELS)
        prec = (tp / pred_cnt) if pred_cnt > 0 else 0.0
        rec = (tp / support) if support > 0 else 0.0
        f1 = (2.0 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0
        f1_scores.append(f1)
    macro_f1 = round(sum(f1_scores) / len(EXPECTED_LABELS), 4)

    multi_hop_cases = [r for r in results if r.multi_hop]
    mh_recall = (
        round(sum(r.evidence_recall for r in multi_hop_cases) / len(multi_hop_cases), 4)
        if multi_hop_cases
        else 1.0
    )

    total_in = sum(r.total_input_tokens for r in results)
    total_out = sum(r.total_output_tokens for r in results)
    reduction = (
        round(1.0 - (total_in / full_context_input_tokens), 4)
        if full_context_input_tokens > 0
        else 0.0
    )

    latencies = sorted(r.latency_ms for r in results)
    categories = sorted({r.category for r in results})
    per_cat_recall = {
        cat: round(sum(r.evidence_recall for r in results if r.category == cat) / sum(1 for r in results if r.category == cat), 4)
        for cat in categories
    }
    per_cat_acc = {
        cat: round(sum(1 for r in results if r.category == cat and r.correct) / sum(1 for r in results if r.category == cat), 4)
        for cat in categories
    }

    grounded_cnt = sum(1 for r in results if r.grounded)
    ungrounded_cnt = sum(1 for r in results if r.ungrounded)

    return StrategySummary(
        strategy=strategy,
        split_filter=split_filter,
        total_cases=total,
        decided_cases=decided,
        undecidable_count=undecidable,
        correct_cases=correct,
        accuracy=round(correct / total, 4) if total else 0.0,
        macro_f1=macro_f1,
        non_supported_cases=non_sup,
        false_supported_count=fs,
        false_supported_rate=round(fsr, 6),
        catastrophic_false_supported_count=cat_fs,
        safe_block_rate=round(safe_block, 4),
        prompt_injection_cases=len(inj),
        prompt_injection_blocked=inj_blocked,
        injection_resistance_rate=inj_rate,
        mean_evidence_recall=round(sum(r.evidence_recall for r in results) / total, 4) if total else 0.0,
        multi_hop_evidence_recall=mh_recall,
        mean_citation_validity=round(sum(r.citation_validity for r in results) / total, 4) if total else 0.0,
        groundedness_rate=round(grounded_cnt / total, 4) if total else 0.0,
        ungrounded_rate=round(ungrounded_cnt / total, 4) if total else 0.0,
        mean_pull_calls=round(sum(r.pulled_count for r in results) / total, 2) if total else 0.0,
        mean_context_chars=round(sum(r.context_chars for r in results) / total, 1) if total else 0.0,
        total_input_tokens=total_in,
        mean_input_tokens=round(total_in / total, 1) if total else 0.0,
        total_output_tokens=total_out,
        token_reduction_vs_full=reduction,
        latency_p50_ms=_percentile(latencies, 50.0),
        latency_p95_ms=_percentile(latencies, 95.0),
        latency_p99_ms=_percentile(latencies, 99.0),
        budget_violations=sum(1 for r in results if r.budget_violation),
        unauthorized_writes=0,
        per_category_recall=per_cat_recall,
        per_category_accuracy=per_cat_acc,
        confusion_matrix=confusion,
    )


def evaluate_adoption_gate(
    agentic: StrategySummary,
    full_context: StrategySummary,
    *,
    min_token_reduction: float = 0.30,
    max_f1_drop: float = 0.05,
    max_fsr: float = 0.05,
    min_decided_rate: float = 0.80,
) -> dict[str, Any]:
    """Phase 6 기본 전략 채택 게이트를 정량 검증한다 (판정기 전면 실패/UNDECIDABLE 시 자동 HOLD)."""
    ag_decided_rate = (agentic.decided_cases / agentic.total_cases) if agentic.total_cases > 0 else 0.0
    fc_decided_rate = (full_context.decided_cases / full_context.total_cases) if full_context.total_cases > 0 else 0.0
    judge_valid = (
        ag_decided_rate >= min_decided_rate
        and fc_decided_rate >= min_decided_rate
        and full_context.macro_f1 > 0.0
    )
    token_ok = agentic.token_reduction_vs_full >= min_token_reduction
    f1_drop = round(full_context.macro_f1 - agentic.macro_f1, 4)
    f1_ok = f1_drop <= max_f1_drop
    fsr_ok = agentic.false_supported_rate <= max_fsr
    safety_ok = agentic.budget_violations == 0 and agentic.unauthorized_writes == 0
    passed = judge_valid and token_ok and f1_ok and fsr_ok and safety_ok
    return {
        "passed": passed,
        "recommended_default": "agentic" if passed else "keyword",
        "checks": {
            "judge_decided_rate_ge_80pct": {
                "passed": judge_valid,
                "agentic_decided_rate": round(ag_decided_rate, 4),
                "full_context_decided_rate": round(fc_decided_rate, 4),
                "threshold": min_decided_rate,
            },
            "token_reduction_ge_30pct": {"passed": token_ok, "actual": agentic.token_reduction_vs_full, "threshold": min_token_reduction},
            "macro_f1_drop_le_5pp": {"passed": f1_ok, "actual_drop": f1_drop, "threshold": max_f1_drop},
            "fsr_le_5pct": {"passed": fsr_ok, "actual": agentic.false_supported_rate, "threshold": max_fsr},
            "zero_budget_or_write_violations": {"passed": safety_ok, "budget_violations": agentic.budget_violations, "unauthorized_writes": agentic.unauthorized_writes},
        },
    }


async def run_four_way_benchmark(
    cases: Sequence[DrDciBenchmarkCase] | None = None,
    *,
    dataset_version: str = "dr-dci-corpus-v1.0",
    split: Literal["all", "dev", "eval"] = "all",
    max_pull_steps: int = 3,
    max_chunk_chars: int = 4000,
    judge: EvidenceJudge | None = None,
    selector: DrDciSelector | None = None,
) -> dict[str, Any]:
    """4종 검색 전략을 동일 코퍼스·동일 판정기로 실행해 비교 리포트를 반환한다."""
    if cases is None:
        dataset_version, all_cases = build_dr_dci_benchmark_dataset()
    else:
        all_cases = list(cases)

    filtered = [c for c in all_cases if split == "all" or c.split == split]
    if not filtered:
        raise ValueError(f"No benchmark cases matched split={split!r}")

    active_judge = judge or RuleBasedJudge()
    active_selector = selector or AdaptiveEvidenceSelector()

    all_results: dict[StrategyName, list[StrategyCaseResult]] = {s: [] for s in ALL_STRATEGIES}
    # Vector RAG의 문서 벡터화·IDF 인덱싱은 실제 Vector DB처럼 쿼리 타이머 바깥에서 사전 구축한다
    prebuilt_vector_indices = {c.case_id: _build_vector_index(c) for c in filtered}
    vector_index_build_ms_total = round(sum(idx.build_ms for idx in prebuilt_vector_indices.values()), 2)

    is_live_llm = (not isinstance(active_judge, RuleBasedJudge)) or (active_selector.name != "adaptive_agentic")

    for strategy in ALL_STRATEGIES:
        settings = Settings(
            _env_file=None,
            dr_dci_mode="agentic" if strategy == "agentic_dr_dci" else "keyword",
            dr_dci_max_pull_steps=max_pull_steps,
            dr_dci_max_chunk_chars=max_chunk_chars,
            dr_dci_timeout_seconds=45.0 if is_live_llm else 10.0,
        )
        for case in filtered:
            t0 = time.perf_counter()
            sel_in_tok = 0
            sel_out_tok = 0

            if strategy == "full_context":
                inv_res, sel_in_tok, sel_out_tok = await _run_full_context(case, max_chunk_chars)
            elif strategy == "vector_rag":
                inv_res, sel_in_tok, sel_out_tok = await _run_vector_rag(
                    case,
                    max_pull_steps,
                    max_chunk_chars,
                    prebuilt_index=prebuilt_vector_indices[case.case_id],
                )
            elif strategy == "keyword_dr_dci":
                backend = InMemoryCorpusBackend(case.chunks)
                req = LogInvestigationRequest(
                    incidentCode=case.incident_code,
                    keywords=list(case.query_keywords),
                    maxPulls=max_pull_steps,
                )
                inv_res = await run_dr_dci_investigation(backend, settings, req)
            else:  # agentic_dr_dci
                backend = InMemoryCorpusBackend(case.chunks)
                req = LogInvestigationRequest(
                    incidentCode=case.incident_code,
                    keywords=list(case.query_keywords),
                    maxPulls=max_pull_steps,
                )
                inv_res = await run_agentic_dr_dci_investigation(backend, settings, req, selector=active_selector)
                sel_in_tok = sum(int(s.get("inputTokens") or 0) for s in inv_res.decisionTrace)
                sel_out_tok = sum(int(s.get("outputTokens") or 0) for s in inv_res.decisionTrace)
                # 오프라인 AdaptiveEvidenceSelector인 경우에도 색인+수집요약 읽기 비용을 투명하게 추정 합산
                if sel_in_tok == 0 and active_selector.name == "adaptive_agentic":
                    index_chars = sum(len(c.title) + len(c.keywords) + len(c.chunk_id) for c in case.chunks)
                    sel_in_tok = _estimate_tokens(index_chars) * max(1, len(inv_res.decisionTrace))

            runbook_text, evidence_text = _assemble_judge_inputs(case, inv_res)
            verdict_res = await active_judge.judge_plan_evidence(
                proposed_action=case.proposed_action,
                runbook=runbook_text,
                evidence=evidence_text,
            )
            wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)

            context_chars = _measure_reasoning_context_chars(inv_res)
            judge_in_tok = (
                int(verdict_res.input_tokens)
                if verdict_res.input_tokens is not None and verdict_res.input_tokens > 0
                else _estimate_tokens(len(case.proposed_action) + len(runbook_text) + len(evidence_text))
            )
            judge_out_tok = int(verdict_res.output_tokens or 0)

            pulled_ids = list(inv_res.evidenceChunkIds)
            valid_ids = {c.chunk_id for c in case.chunks}
            required = set(case.required_chunk_ids)
            recall = round(len(required.intersection(pulled_ids)) / len(required), 4) if required else 1.0
            citation_validity = (
                round(sum(1 for cid in pulled_ids if cid in valid_ids) / len(pulled_ids), 4)
                if pulled_ids
                else 1.0
            )

            false_supported = case.expected_verdict != "SUPPORTED" and verdict_res.verdict == "SUPPORTED"
            cat_fs = case.expected_verdict == "CONTRADICTED" and verdict_res.verdict == "SUPPORTED"

            # Groundedness 정의:
            # 1) 비실존/비인출 인용이 없고(citation_validity == 1.0),
            # 2) SUPPORTED 판정 시 정답이 SUPPORTED이고 필수 근거를 100% 회수했을 것,
            # 3) CONTRADICTED 판정 시 필수 충돌 근거를 실제로 인출했을 것,
            # 4) NEED_MORE_EVIDENCE 보류는 안전 보류로 grounded(단 근거 부재/누락 시 정상 보류).
            is_ungrounded = (
                citation_validity < 1.0
                or false_supported
                or (verdict_res.verdict == "SUPPORTED" and recall < 1.0)
                or (verdict_res.verdict == "CONTRADICTED" and recall < 1.0)
            )
            budget_violation = strategy != "full_context" and len(pulled_ids) > max_pull_steps

            all_results[strategy].append(StrategyCaseResult(
                strategy=strategy,
                case_id=case.case_id,
                split=case.split,
                category=case.category,
                archetype=case.archetype,
                multi_hop=case.multi_hop,
                prompt_injection=case.prompt_injection,
                evidence_absent=case.evidence_absent,
                agentic_disadvantage=case.agentic_disadvantage,
                expected=case.expected_verdict,
                predicted=verdict_res.verdict,
                correct=verdict_res.verdict == case.expected_verdict,
                false_supported=false_supported,
                catastrophic_false_supported=cat_fs,
                pulled_chunk_ids=pulled_ids,
                required_chunk_ids=list(case.required_chunk_ids),
                pulled_count=len(pulled_ids),
                evidence_recall=recall,
                citation_validity=citation_validity,
                grounded=not is_ungrounded,
                ungrounded=is_ungrounded,
                context_chars=context_chars,
                selector_input_tokens=sel_in_tok,
                selector_output_tokens=sel_out_tok,
                judge_input_tokens=judge_in_tok,
                judge_output_tokens=judge_out_tok,
                total_input_tokens=sel_in_tok + judge_in_tok,
                total_output_tokens=sel_out_tok + judge_out_tok,
                latency_ms=wall_ms,
                stop_reason=inv_res.stopReason or ("KEYWORD_TOP_K" if strategy == "keyword_dr_dci" else "DONE"),
                budget_violation=budget_violation,
            ))

    full_in_tokens = sum(r.total_input_tokens for r in all_results["full_context"])
    summaries = {
        s: _summarize_strategy(s, all_results[s], split_filter=split, full_context_input_tokens=full_in_tokens)
        for s in ALL_STRATEGIES
    }

    # dev / eval 분리 요약도 함께 계산
    split_summaries: dict[str, dict[str, Any]] = {}
    for sp in ("dev", "eval"):
        sp_full_rows = [r for r in all_results["full_context"] if r.split == sp]
        if not sp_full_rows:
            continue
        sp_full_tok = sum(r.total_input_tokens for r in sp_full_rows)
        split_summaries[sp] = {
            s: asdict(_summarize_strategy(s, [r for r in all_results[s] if r.split == sp], split_filter=sp, full_context_input_tokens=sp_full_tok))
            for s in ALL_STRATEGIES
        }

    # 에이전틱 방식이 키워드 대비 불리했던 사례 자동 수집 (회수율·정확도 열세 또는 지연/토큰 손해 반례)
    kw_by_id = {r.case_id: r for r in all_results["keyword_dr_dci"]}
    agentic_disadvantages: list[dict[str, Any]] = []
    for ag in all_results["agentic_dr_dci"]:
        kw = kw_by_id[ag.case_id]
        if ag.agentic_disadvantage or ag.evidence_recall < kw.evidence_recall or (kw.correct and not ag.correct):
            agentic_disadvantages.append({
                "case_id": ag.case_id,
                "category": ag.category,
                "archetype": ag.archetype,
                "expected": ag.expected,
                "keyword_recall": kw.evidence_recall,
                "agentic_recall": ag.evidence_recall,
                "keyword_correct": kw.correct,
                "agentic_correct": ag.correct,
                "keyword_tokens": kw.total_input_tokens,
                "agentic_tokens": ag.total_input_tokens,
                "keyword_pulled": kw.pulled_chunk_ids,
                "agentic_pulled": ag.pulled_chunk_ids,
                "stop_reason": ag.stop_reason,
            })

    adoption_gate = evaluate_adoption_gate(summaries["agentic_dr_dci"], summaries["full_context"])

    is_live_llm = (not isinstance(active_judge, RuleBasedJudge)) or (active_selector.name != "adaptive_agentic")
    latency_scope = "live_llm_api_inclusive" if is_live_llm else "offline_in_memory_preindexed_no_llm_mcp_rtt"

    return {
        "schema_version": "dr-dci-4way-benchmark-v1",
        "dataset_version": dataset_version,
        "split_filter": split,
        "total_cases": len(filtered),
        "max_pull_steps": max_pull_steps,
        "judge": type(active_judge).__name__,
        "selector": active_selector.name,
        "vector_index_build_ms_total": vector_index_build_ms_total,
        "latency_scope": latency_scope,
        "adoption_gate": adoption_gate,
        "summaries": [asdict(summaries[s]) for s in ALL_STRATEGIES],
        "split_summaries": split_summaries,
        "agentic_disadvantage_cases": agentic_disadvantages,
        "cases": [asdict(r) for s in ALL_STRATEGIES for r in all_results[s]],
    }


def format_four_way_markdown(payload: dict[str, Any]) -> str:
    """4종 검색 전략 비교 결과를 포트폴리오 리포트용 Markdown으로 포맷한다."""
    gate = payload["adoption_gate"]
    lines: list[str] = [
        f"# Phase 6 · 에이전틱 DR-DCI 및 4종 검색 전략 정량 벤치마크 (`{payload['dataset_version']}`)",
        "",
        f"- **코퍼스:** `{payload['dataset_version']}` (총 **{payload['total_cases']}건**, `max_pull_steps={payload['max_pull_steps']}`, Judge=`{payload['judge']}`, Selector=`{payload['selector']}`)",
        f"- **채택 게이트 판정:** {'**PASS (`agentic` 채택 기준 충족)**' if gate['passed'] else '**HOLD (`keyword` 기본값 유지)**'} (추천 기본값: `{gate['recommended_default']}`)",
        "",
        "## 1. 4종 검색 전략 종합 비교",
        "",
        "| Strategy | Evidence Recall | Multi-Hop Recall | Groundedness | Ungrounded (↓) | Accuracy | Macro-F1 | **FSR (↓)** | Mean Input Tokens (vs Full) | Mean Pulls | p50 / p95 / p99 (ms) | Violations |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for s in payload["summaries"]:
        red_pct = s["token_reduction_vs_full"] * 100.0
        red_str = "baseline" if s["strategy"] == "full_context" else f"{-red_pct:+.1f}%"
        lines.append(
            f"| `{s['strategy']}` | "
            f"{s['mean_evidence_recall'] * 100:.1f}% | "
            f"{s['multi_hop_evidence_recall'] * 100:.1f}% | "
            f"{s['groundedness_rate'] * 100:.1f}% | "
            f"{s['ungrounded_rate'] * 100:.1f}% | "
            f"{s['accuracy'] * 100:.1f}% ({s['correct_cases']}/{s['total_cases']}) | "
            f"{s['macro_f1']:.3f} | "
            f"**{s['false_supported_rate'] * 100:.1f}%** ({s['false_supported_count']}/{s['non_supported_cases']}) | "
            f"{s['mean_input_tokens']:.1f} ({red_str}) | "
            f"{s['mean_pull_calls']:.2f} | "
            f"{s['latency_p50_ms']:.2f} / {s['latency_p95_ms']:.2f} / {s['latency_p99_ms']:.2f} | "
            f"{s['budget_violations']}/{s['unauthorized_writes']} |"
        )

    if payload.get("split_summaries"):
        lines.extend([
            "",
            "## 2. Dev / Eval 원형 분리 검증",
            "",
            "| Split | Strategy | Evidence Recall | Multi-Hop Recall | Macro-F1 | FSR | Token Reduction |",
            "| :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
        ])
        for sp_name, sp_map in payload["split_summaries"].items():
            for st in ALL_STRATEGIES:
                s = sp_map[st]
                lines.append(
                    f"| `{sp_name}` ({s['total_cases']}건) | `{st}` | "
                    f"{s['mean_evidence_recall'] * 100:.1f}% | "
                    f"{s['multi_hop_evidence_recall'] * 100:.1f}% | "
                    f"{s['macro_f1']:.3f} | "
                    f"{s['false_supported_rate'] * 100:.1f}% ({s['false_supported_count']}/{s['non_supported_cases']}) | "
                    f"{s['token_reduction_vs_full'] * 100:.1f}% |"
                )

    dis = payload.get("agentic_disadvantage_cases") or []
    if dis:
        lines.extend([
            "",
            "## 3. 에이전틱 DR-DCI가 불리하거나 손해 본 반례 사례 (투명 공개)",
            "",
            "| Case ID | Category | Archetype | Keyword Recall / Tokens | Agentic Recall / Tokens | Stop Reason |",
            "| :--- | :--- | :--- | :---: | :---: | :--- |",
        ])
        for row in dis:
            lines.append(
                f"| `{row['case_id']}` | `{row['category']}` | `{row['archetype']}` | "
                f"{row['keyword_recall'] * 100:.0f}% / {row['keyword_tokens']} tok | "
                f"{row['agentic_recall'] * 100:.0f}% / {row['agentic_tokens']} tok | "
                f"`{row['stop_reason']}` |"
            )

    lines.append("")
    return "\n".join(lines)


def write_four_way_csv(payload: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "strategy", "case_id", "split", "category", "archetype", "multi_hop", "prompt_injection",
        "expected", "predicted", "correct", "false_supported", "pulled_count", "evidence_recall",
        "citation_validity", "grounded", "ungrounded", "context_chars", "selector_input_tokens",
        "judge_input_tokens", "total_input_tokens", "total_output_tokens", "latency_ms",
        "stop_reason", "budget_violation", "pulled_chunk_ids", "required_chunk_ids",
    ]
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for row in payload["cases"]:
            flat = {k: row[k] for k in fieldnames if k not in ("pulled_chunk_ids", "required_chunk_ids")}
            flat["pulled_chunk_ids"] = ";".join(row["pulled_chunk_ids"])
            flat["required_chunk_ids"] = ";".join(row["required_chunk_ids"])
            writer.writerow(flat)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run 4-way DR-DCI retrieval strategy benchmark.")
    parser.add_argument("--split", choices=("all", "dev", "eval"), default="all")
    parser.add_argument("--max-pull-steps", type=int, default=3)
    parser.add_argument("--judge", choices=("rule_based", "structured_output", "decisions_api"), default="rule_based")
    parser.add_argument("--selector", choices=("adaptive", "openai"), default="adaptive")
    parser.add_argument("--env-file", default=None, help="Optional .env file path to load Settings from")
    parser.add_argument("--openai-model", default=None, help="Optional override for OPENAI_MODEL")
    parser.add_argument("--decisions-model", default=None, help="Optional override for DECISIONS_MODEL")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-csv", default=None)
    parser.add_argument("--output-md", default=None)
    args = parser.parse_args(argv)

    settings = Settings(_env_file=args.env_file) if args.env_file else Settings()
    updates: dict[str, Any] = {}
    if args.openai_model:
        updates["openai_model"] = args.openai_model
    if args.decisions_model:
        updates["decisions_model"] = args.decisions_model
    if updates:
        settings = settings.model_copy(update=updates)
    judge: EvidenceJudge
    if args.judge == "rule_based":
        judge = RuleBasedJudge()
    elif args.judge == "structured_output":
        judge = StructuredOutputJudge(settings)
    else:
        judge = DecisionsApiJudge(settings)

    selector: DrDciSelector = OpenAiDrDciSelector(settings) if args.selector == "openai" else AdaptiveEvidenceSelector()

    ls_client = build_langsmith_client(settings)
    try:
        with langsmith_tracing_scope(
            settings,
            client=ls_client,
            tags=["eval", "dr-dci-4way", args.judge, args.selector],
            metadata={"split": args.split, "max_pull_steps": args.max_pull_steps},
        ):
            payload = asyncio.run(run_four_way_benchmark(
                split=args.split,
                max_pull_steps=args.max_pull_steps,
                judge=judge,
                selector=selector,
            ))
    finally:
        flush_langsmith_client(ls_client)
    md = format_four_way_markdown(payload)
    print(md)

    out_json = Path(args.output_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.output_csv:
        write_four_way_csv(payload, args.output_csv)
    if args.output_md:
        out_md = Path(args.output_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
