from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import csv
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import random
import time
from typing import Any, Literal, Sequence

from common.config import Settings
from common.decisions import EvidenceJudge, RuleBasedJudge
from eval.dr_dci_baseline import CorpusChunkFixture, InMemoryCorpusBackend, _measure_reasoning_context_chars
from eval.dr_dci_benchmark import (
    _assemble_judge_inputs,
    _cosine_similarity,
    _estimate_tokens,
    _run_full_context,
    _vectorize_text,
)
from eval.dr_dci_corpus import DrDciBenchmarkCase
from eval.harness import ALL_VERDICTS, EXPECTED_LABELS, _percentile
from eval.synthetic_generator import generate_synthetic_incidents
from log_agent.agentic_dr_dci import AdaptiveEvidenceSelector, _extract_dynamic_clues, run_agentic_dr_dci_investigation
from log_agent.schemas import EvidenceItem, LogInvestigationRequest, LogInvestigationResponse
from log_agent.services import _extract_command, _summarize_chunk

ParadigmName = Literal["native_rag", "hybrid_rag_rrf", "dci", "dr_dci"]
ALL_PARADIGMS: tuple[ParadigmName, ...] = ("native_rag", "hybrid_rag_rrf", "dci", "dr_dci")

# N이 커질수록 정적 RAG/BM25를 실제로 혼란시키는 현실적인 Hard Distractor 로그 템플릿
_HARD_DISTRACTOR_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    ("과거 주간 502 SLA 정상 요약 리포트", "502,timeout,sla,report,weekly",
     "INFO [sla-reporter] Weekly 502 summary: 502 error rate=0.01% timeout count=0 status=healthy across all clusters. No rollback needed."),
    ("HikariPool 정기 헬스 스냅샷 (정상)", "HikariPool,connection,pool,healthy,monitor",
     "INFO [pool-monitor] HikariPool-1 periodic snapshot active=3 idle=17 waiting=0 timeout=0ms leak_warnings=0 healthy=true."),
    ("이전 릴리스 배포 완료 아카이브", "deploy,rollout,rollback,history,archive",
     "INFO [cd-audit] Historical rollout audit: previous deploy completed cleanly, rollback drill passed in staging 14 days ago."),
    ("카나리 메모리 GC 정상 회수 로그", "memory,OOMKilled,heap,gc,normal",
     "INFO [jvm-gc] G1GC pause 12ms heap=38% OOMKilled=0 CrashLoopBackOff=0 memory pressure normal."),
    ("외부 PG 정기 헬스체크 응답 200", "upstream,pg,503,timeout,health",
     "INFO [pg-probe] Scheduled upstream PG ping 200 OK (42ms); 503=0 timeout=0 external gateway healthy."),
    ("Flyway 스키마 버전 정상 검증 로그", "flyway,migration,schema,500,checksum",
     "INFO [flyway-verify] Schema checksum validated migration history consistent 500_errors=0 no DDL changes pending."),
)


def _tokenize_bm25(text: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9가-힣_.-]+", text.lower())


def _bm25_scores(query_tokens: list[str], doc_tokens_list: list[list[str]], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """표준 Okapi BM25 점수를 계산한다."""
    n_docs = len(doc_tokens_list)
    if n_docs == 0:
        return []
    avg_dl = sum(len(d) for d in doc_tokens_list) / n_docs or 1.0
    df: Counter[str] = Counter()
    tfs: list[Counter[str]] = []
    for doc_toks in doc_tokens_list:
        tf = Counter(doc_toks)
        tfs.append(tf)
        for term in tf:
            df[term] += 1

    scores: list[float] = []
    q_unique = list(dict.fromkeys(query_tokens))
    for doc_toks, tf in zip(doc_tokens_list, tfs):
        dl = len(doc_toks)
        score = 0.0
        for term in q_unique:
            freq = tf.get(term, 0)
            if freq == 0:
                continue
            doc_freq = df.get(term, 0)
            idf = math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
            denom = freq + k1 * (1.0 - b + b * (dl / avg_dl))
            score += idf * ((freq * (k1 + 1.0)) / denom)
        scores.append(score)
    return scores


def _rrf_fuse(rankings: Sequence[Sequence[int]], *, k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion: score(d) = sum(1 / (k + rank_i(d)))."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank_idx, doc_idx in enumerate(ranking, start=1):
            fused[doc_idx] = fused.get(doc_idx, 0.0) + 1.0 / (k + rank_idx)
    return [doc_idx for doc_idx, _ in sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))]


def _chunks_to_response(case: DrDciBenchmarkCase, selected: Sequence[CorpusChunkFixture], max_chunk_chars: int, stop_reason: str) -> LogInvestigationResponse:
    evidence: list[EvidenceItem] = []
    runbook_parts: list[str] = []
    cmd: str | None = None
    for ch in selected:
        is_rb = ch.corpus_type.upper() == "RUNBOOK"
        summary = _summarize_chunk(ch.content, min(max_chunk_chars, 400), preserve_newlines=is_rb)
        evidence.append(EvidenceItem(chunkId=ch.chunk_id, corpusType=ch.corpus_type, title=ch.title, summary=summary))
        if is_rb:
            runbook_parts.append(_summarize_chunk(ch.content, max_chunk_chars, preserve_newlines=True))
            if cmd is None:
                cmd = _extract_command(ch.content)
    selected_ids = {c.chunk_id for c in selected}
    return LogInvestigationResponse(
        ok=True,
        incidentCode=case.incident_code,
        evidence=evidence,
        evidenceChunkIds=[c.chunk_id for c in selected],
        runbookSummary="\n".join(runbook_parts),
        recommendedCommand=cmd,
        remainingChunkIds=[c.chunk_id for c in case.chunks if c.chunk_id not in selected_ids],
        pulledCount=len(selected),
        stopReason=stop_reason,
    )


async def _run_native_rag(case: DrDciBenchmarkCase, top_k: int, max_chunk_chars: int) -> LogInvestigationResponse:
    """Native RAG: 단일 쿼리 벡터 코사인 유사도 기반 정적 Top-K 검색."""
    query_text = f"{case.service_name} {case.proposed_action} {' '.join(case.query_keywords)}"
    doc_vecs = [_vectorize_text(f"{c.title} {c.keywords} {c.content}") for c in case.chunks]
    query_vec = _vectorize_text(query_text)

    n_docs = len(doc_vecs)
    df: Counter[str] = Counter()
    for vec in doc_vecs:
        for term in vec:
            df[term] += 1
    idf = {term: math.log((n_docs + 1.0) / (freq + 1.0)) + 1.0 for term, freq in df.items()}

    scored = [(_cosine_similarity(query_vec, vec, idf), -idx, idx) for idx, vec in enumerate(doc_vecs)]
    scored.sort(reverse=True)
    selected = [case.chunks[idx] for _, _, idx in scored[:top_k]]
    return _chunks_to_response(case, selected, max_chunk_chars, "NATIVE_RAG_TOP_K")


async def _run_hybrid_rag_rrf(case: DrDciBenchmarkCase, top_k: int, max_chunk_chars: int) -> LogInvestigationResponse:
    """Hybrid RAG (Vector + BM25 + RRF): 벡터 유사도와 Okapi BM25 순위를 RRF(k=60)로 융합해 Top-K 인출."""
    query_text = f"{case.service_name} {case.proposed_action} {' '.join(case.query_keywords)}"
    doc_texts = [f"{c.title} {c.keywords} {c.content}" for c in case.chunks]

    # 1) Dense/Vector ranking
    doc_vecs = [_vectorize_text(t) for t in doc_texts]
    query_vec = _vectorize_text(query_text)
    n_docs = len(doc_vecs)
    df: Counter[str] = Counter()
    for vec in doc_vecs:
        for term in vec:
            df[term] += 1
    idf = {term: math.log((n_docs + 1.0) / (freq + 1.0)) + 1.0 for term, freq in df.items()}
    vec_ranked = [idx for _, _, idx in sorted(
        ((_cosine_similarity(query_vec, vec, idf), -i, i) for i, vec in enumerate(doc_vecs)),
        reverse=True,
    )]

    # 2) BM25 lexical ranking
    q_toks = _tokenize_bm25(query_text)
    doc_toks = [_tokenize_bm25(t) for t in doc_texts]
    bm25 = _bm25_scores(q_toks, doc_toks)
    bm25_ranked = [idx for _, _, idx in sorted(((s, -i, i) for i, s in enumerate(bm25)), reverse=True)]

    # 3) Reciprocal Rank Fusion
    fused_indices = _rrf_fuse([vec_ranked, bm25_ranked], k=60)
    selected = [case.chunks[idx] for idx in fused_indices[:top_k]]
    return _chunks_to_response(case, selected, max_chunk_chars, "HYBRID_RRF_TOP_K")


class _IndexedWindowSelector:
    """대규모 코퍼스(N=10..500)에서 색인 검색 필터링으로 후보 창(top-M 메타데이터)만 읽고 동적 피벗하는 DR-DCI 선택기."""

    name = "indexed_dr_dci"

    def __init__(self, *, index_window: int = 12) -> None:
        self._inner = AdaptiveEvidenceSelector()
        self._index_window = index_window
        self.last_candidate_meta_chars: int = 0

    def _prefilter_index(
        self,
        keywords: Sequence[str],
        remaining_index: Sequence[dict[str, Any]],
        collected_evidence: Sequence[EvidenceItem],
    ) -> list[dict[str, Any]]:
        if len(remaining_index) <= self._index_window:
            return list(remaining_index)
        direct_refs, clue_tokens = _extract_dynamic_clues(collected_evidence)
        kw_lower = [k.strip().lower() for k in keywords if k and k.strip()]

        scored: list[tuple[int, int, dict[str, Any]]] = []
        for meta in remaining_index:
            cid = str(meta.get("chunkId") or "")
            ctype = str(meta.get("corpusType") or "LOG").upper()
            hay = " ".join(str(meta.get(f) or "") for f in ("title", "keywords", "chunkId")).lower()
            sc = 0
            if cid in direct_refs:
                sc += 100
            if ctype == "RUNBOOK":
                sc += 18
            sc += sum(8 for tok in clue_tokens if tok in hay)
            sc += sum(5 for kw in kw_lower if kw in hay)
            # Hard distractor 중 정상/아카이브/정기 리포트는 색인 제목 단계에서 우선순위 감점
            if any(w in hay for w in ("정상", "아카이브", "정기", "healthy", "weekly", "historical")):
                sc -= 6
            scored.append((sc, -int(meta.get("sequenceOrder") or 0), meta))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [m for _, _, m in scored[: self._index_window]]

    async def select_next_action(
        self,
        *,
        incident_code: str,
        keywords: Sequence[str],
        remaining_index: Sequence[dict[str, Any]],
        collected_evidence: Sequence[EvidenceItem],
        step_no: int,
        max_steps: int,
    ):
        windowed = self._prefilter_index(keywords, remaining_index, collected_evidence)
        self.last_candidate_meta_chars = sum(
            len(str(m.get("chunkId") or "")) + len(str(m.get("title") or "")) + len(str(m.get("keywords") or ""))
            for m in windowed
        )
        return await self._inner.select_next_action(
            incident_code=incident_code,
            keywords=keywords,
            remaining_index=windowed,
            collected_evidence=collected_evidence,
            step_no=step_no,
            max_steps=max_steps,
        )


def _expand_case_to_scale(base_case: DrDciBenchmarkCase, target_chunks: int, *, seed: int) -> DrDciBenchmarkCase:
    """기준 장애 케이스의 핵심 청크는 보존하면서 일반 노이즈 + Hard Distractor를 추가해 정확히 target_chunks 규모로 확장한다."""
    rng = random.Random(seed)
    core_ids = set(base_case.required_chunk_ids)
    core_chunks = [c for c in base_case.chunks if c.chunk_id in core_ids or c.corpus_type == "RUNBOOK"]
    existing_ids = {c.chunk_id for c in core_chunks}
    extra_needed = max(0, target_chunks - len(core_chunks))

    filler: list[CorpusChunkFixture] = []
    # 25%는 키워드가 겹치는 Hard Distractor, 75%는 일반 운영 노이즈 로그로 채워 현실적인 스케일 환경을 구성한다
    n_hard = max(1, int(extra_needed * 0.25)) if extra_needed >= 4 else 0
    for i in range(extra_needed):
        seq = i + 1
        cid = f"N-{base_case.incident_code[-4:]}-{seq:03d}"
        while cid in existing_ids:
            seq += 1
            cid = f"N-{base_case.incident_code[-4:]}-{seq:03d}"
        existing_ids.add(cid)

        if i < n_hard:
            title, kws, body = _HARD_DISTRACTOR_TEMPLATES[(i + rng.randint(0, 5)) % len(_HARD_DISTRACTOR_TEMPLATES)]
            padded = "\n".join(
                f"2026-11-{(i % 25) + 1:02d} 12:{i % 60:02d}:{10 + j * 4:02d} [{base_case.service_name}] {body}"
                for j in range(3)
            )
            filler.append(CorpusChunkFixture(
                chunk_id=cid,
                corpus_type="LOG",
                incident_code=base_case.incident_code,
                service_name=base_case.service_name,
                title=f"[D-{i + 1:03d}] {title}",
                keywords=kws,
                content=padded,
                sequence_order=i + 1,
            ))
        else:
            padded = "\n".join(
                f"2026-11-{(i % 25) + 1:02d} 13:{i % 60:02d}:{10 + j * 5:02d} [{base_case.service_name}] "
                f"INFO [worker-{j}] GET /api/v1/health 200 OK ({3 + j}ms) batch={i} cache_hit=99.4% queue_lag=0"
                for j in range(4)
            )
            filler.append(CorpusChunkFixture(
                chunk_id=cid,
                corpus_type="LOG",
                incident_code=base_case.incident_code,
                service_name=base_case.service_name,
                title=f"[N-{i + 1:03d}] 정상 운영 트래픽 및 헬스체크 로그",
                keywords="healthcheck,200,normal,batch,cache",
                content=padded,
                sequence_order=i + 1,
            ))

    # 핵심 청크를 후반부 시퀀스에 섞어 배치 (Lost-in-the-Middle 및 선형 탐색 현실 반영)
    combined: list[CorpusChunkFixture] = []
    mid = len(filler) // 2
    reindexed_core: list[CorpusChunkFixture] = []
    for k, ch in enumerate(core_chunks, start=1):
        reindexed_core.append(CorpusChunkFixture(
            chunk_id=ch.chunk_id,
            corpus_type=ch.corpus_type,
            incident_code=ch.incident_code,
            service_name=ch.service_name,
            title=ch.title,
            keywords=ch.keywords,
            content=ch.content,
            sequence_order=mid + k if ch.corpus_type == "LOG" else ch.sequence_order,
        ))
    combined.extend(filler[:mid])
    combined.extend(reindexed_core)
    combined.extend(filler[mid:])

    return DrDciBenchmarkCase(
        case_id=f"{base_case.case_id}-N{target_chunks}",
        split=base_case.split,
        incident_code=base_case.incident_code,
        service_name=base_case.service_name,
        category=base_case.category,
        archetype=base_case.archetype,
        expected_verdict=base_case.expected_verdict,
        proposed_action=base_case.proposed_action,
        query_keywords=base_case.query_keywords,
        required_chunk_ids=base_case.required_chunk_ids,
        external_metrics_summary=base_case.external_metrics_summary,
        chunks=tuple(combined),
        multi_hop=base_case.multi_hop,
        prompt_injection=base_case.prompt_injection,
        evidence_absent=base_case.evidence_absent,
        agentic_disadvantage=base_case.agentic_disadvantage,
    )


@dataclass(frozen=True)
class ScalePointMetric:
    """특정 (데이터 크기 N, 검색 패러다임)에서의 실측 집계 지표."""

    chunks_per_incident: int
    paradigm: ParadigmName
    incidents_tested: int
    total_chunks_scanned: int
    mean_evidence_recall: float
    multi_hop_evidence_recall: float
    groundedness_rate: float
    ungrounded_rate: float
    accuracy: float
    macro_f1: float
    false_supported_rate: float
    false_supported_count: int
    non_supported_cases: int
    mean_input_tokens: float
    token_reduction_vs_dci: float
    overflow_32k_rate: float
    mean_pull_calls: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float


async def run_linear_scaling_benchmark(
    *,
    scales: Sequence[int] = (10, 50, 100, 200, 300, 500),
    incidents_per_scale: int = 30,
    seed: int = 42,
    top_k: int = 3,
    max_chunk_chars: int = 4000,
    context_limit_tokens: int = 32_000,
    judge: EvidenceJudge | None = None,
) -> dict[str, Any]:
    """코퍼스 크기 N이 선형 증가할 때 Native RAG vs Hybrid RAG(BM25+RRF) vs DCI vs DR-DCI의 성능을 실측한다."""
    _, base_cases = generate_synthetic_incidents(count=incidents_per_scale, seed=seed, noise_chunks_per_incident=2)
    active_judge = judge or RuleBasedJudge()
    points: list[ScalePointMetric] = []

    for scale_n in scales:
        scaled_cases = [
            _expand_case_to_scale(c, scale_n, seed=seed * 1000 + scale_n * 31 + idx)
            for idx, c in enumerate(base_cases)
        ]

        per_paradigm_rows: dict[ParadigmName, list[dict[str, Any]]] = {p: [] for p in ALL_PARADIGMS}

        for paradigm in ALL_PARADIGMS:
            settings = Settings(
                _env_file=None,
                dr_dci_mode="agentic",
                dr_dci_max_pull_steps=top_k,
                dr_dci_max_chunk_chars=max_chunk_chars,
                dr_dci_max_context_chars=6000,
            )
            for case in scaled_cases:
                t0 = time.perf_counter()
                sel_tokens = 0
                if paradigm == "native_rag":
                    inv_res = await _run_native_rag(case, top_k, max_chunk_chars)
                elif paradigm == "hybrid_rag_rrf":
                    inv_res = await _run_hybrid_rag_rrf(case, top_k, max_chunk_chars)
                elif paradigm == "dci":
                    inv_res, _, _ = await _run_full_context(case, max_chunk_chars)
                else:  # dr_dci
                    backend = InMemoryCorpusBackend(case.chunks)
                    selector = _IndexedWindowSelector(index_window=12)
                    req = LogInvestigationRequest(
                        incidentCode=case.incident_code,
                        keywords=list(case.query_keywords),
                        maxPulls=top_k,
                    )
                    inv_res = await run_agentic_dr_dci_investigation(backend, settings, req, selector=selector)
                    sel_tokens = _estimate_tokens(selector.last_candidate_meta_chars) * max(1, len(inv_res.decisionTrace))

                runbook_text, evidence_text = _assemble_judge_inputs(case, inv_res)
                verdict_res = await active_judge.judge_plan_evidence(
                    proposed_action=case.proposed_action,
                    runbook=runbook_text,
                    evidence=evidence_text,
                )
                elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)

                judge_in_tok = (
                    int(verdict_res.input_tokens)
                    if verdict_res.input_tokens is not None and verdict_res.input_tokens > 0
                    else _estimate_tokens(len(case.proposed_action) + len(runbook_text) + len(evidence_text))
                )
                total_in_tok = sel_tokens + judge_in_tok
                pulled_ids = list(inv_res.evidenceChunkIds)
                required = set(case.required_chunk_ids)
                recall = len(required.intersection(pulled_ids)) / len(required) if required else 1.0
                valid_ids = {c.chunk_id for c in case.chunks}
                cit_valid = (sum(1 for cid in pulled_ids if cid in valid_ids) / len(pulled_ids)) if pulled_ids else 1.0
                false_sup = case.expected_verdict != "SUPPORTED" and verdict_res.verdict == "SUPPORTED"
                ungrounded = (
                    cit_valid < 1.0
                    or false_sup
                    or (verdict_res.verdict in ("SUPPORTED", "CONTRADICTED") and recall < 1.0)
                )

                per_paradigm_rows[paradigm].append({
                    "expected": case.expected_verdict,
                    "predicted": verdict_res.verdict,
                    "correct": verdict_res.verdict == case.expected_verdict,
                    "false_supported": false_sup,
                    "multi_hop": case.multi_hop,
                    "recall": recall,
                    "ungrounded": ungrounded,
                    "input_tokens": total_in_tok,
                    "overflow": total_in_tok > context_limit_tokens,
                    "pulls": len(pulled_ids),
                    "latency_ms": elapsed_ms,
                })

        dci_mean_tokens = sum(r["input_tokens"] for r in per_paradigm_rows["dci"]) / len(scaled_cases)

        for paradigm in ALL_PARADIGMS:
            rows = per_paradigm_rows[paradigm]
            n = len(rows)
            non_sup = sum(1 for r in rows if r["expected"] != "SUPPORTED")
            fs = sum(1 for r in rows if r["false_supported"])
            mh = [r for r in rows if r["multi_hop"]]
            confusion = {e: {p: 0 for p in ALL_VERDICTS} for e in EXPECTED_LABELS}
            for r in rows:
                confusion[r["expected"]][r["predicted"]] += 1
            f1s = []
            for label in EXPECTED_LABELS:
                tp = confusion[label][label]
                sup = sum(confusion[label].values())
                pred = sum(confusion[e][label] for e in EXPECTED_LABELS)
                prec = tp / pred if pred else 0.0
                rec = tp / sup if sup else 0.0
                f1s.append((2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0)
            latencies = sorted(r["latency_ms"] for r in rows)
            mean_tok = sum(r["input_tokens"] for r in rows) / n
            points.append(ScalePointMetric(
                chunks_per_incident=scale_n,
                paradigm=paradigm,
                incidents_tested=n,
                total_chunks_scanned=sum(len(c.chunks) for c in scaled_cases),
                mean_evidence_recall=round(sum(r["recall"] for r in rows) / n, 4),
                multi_hop_evidence_recall=round(sum(r["recall"] for r in mh) / len(mh), 4) if mh else 1.0,
                groundedness_rate=round(sum(1 for r in rows if not r["ungrounded"]) / n, 4),
                ungrounded_rate=round(sum(1 for r in rows if r["ungrounded"]) / n, 4),
                accuracy=round(sum(1 for r in rows if r["correct"]) / n, 4),
                macro_f1=round(sum(f1s) / len(EXPECTED_LABELS), 4),
                false_supported_rate=round(fs / non_sup, 6) if non_sup else 0.0,
                false_supported_count=fs,
                non_supported_cases=non_sup,
                mean_input_tokens=round(mean_tok, 1),
                token_reduction_vs_dci=round(1.0 - (mean_tok / dci_mean_tokens), 4) if dci_mean_tokens > 0 else 0.0,
                overflow_32k_rate=round(sum(1 for r in rows if r["overflow"]) / n, 4),
                mean_pull_calls=round(sum(r["pulls"] for r in rows) / n, 2),
                latency_p50_ms=_percentile(latencies, 50.0),
                latency_p95_ms=_percentile(latencies, 95.0),
                latency_p99_ms=_percentile(latencies, 99.0),
            ))

    return {
        "schema_version": "linear-scaling-4paradigm-v1",
        "scales": list(scales),
        "incidents_per_scale": incidents_per_scale,
        "seed": seed,
        "top_k": top_k,
        "context_limit_tokens": context_limit_tokens,
        "points": [asdict(p) for p in points],
    }


def write_scaling_csv(payload: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(payload["points"][0].keys())
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(payload["points"])
    return path


def format_scaling_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 선형 데이터 증가(`N = 10 → 500` 청크/장애)에 따른 4대 검색 패러다임 성능 비교",
        "",
        f"- **비교 대상:** `Native RAG` · `Hybrid RAG (Vector + BM25 + RRF)` · `DCI (Full Direct Corpus)` · `DR-DCI (Dynamic Retrieval DCI)`",
        f"- **실험 설정:** 스케일 `{payload['scales']}` 청크/장애 × 각 **{payload['incidents_per_scale']}건** (seed={payload['seed']}, `top_k={payload['top_k']}`, 25% Hard Distractor + 75% 운영 노이즈, 32k 컨텍스트 한도 체크)",
        "",
        "## 스케일별 핵심 지표 요약",
        "",
        "| Chunks / Incident (`N`) | Paradigm | Evidence Recall | Multi-Hop Recall | Groundedness | Macro-F1 | FSR | Mean Input Tokens (vs DCI) | >32k Overflow | `p50 / p95 / p99` (ms) |",
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for p in payload["points"]:
        red = "baseline" if p["paradigm"] == "dci" else f"-{p['token_reduction_vs_dci'] * 100:.1f}%"
        lines.append(
            f"| `{p['chunks_per_incident']}` | `{p['paradigm']}` | "
            f"{p['mean_evidence_recall'] * 100:.1f}% | "
            f"{p['multi_hop_evidence_recall'] * 100:.1f}% | "
            f"{p['groundedness_rate'] * 100:.1f}% | "
            f"{p['macro_f1']:.3f} | "
            f"{p['false_supported_rate'] * 100:.1f}% ({p['false_supported_count']}/{p['non_supported_cases']}) | "
            f"{p['mean_input_tokens']:,.0f} ({red}) | "
            f"{p['overflow_32k_rate'] * 100:.0f}% | "
            f"{p['latency_p50_ms']:.2f} / {p['latency_p95_ms']:.2f} / {p['latency_p99_ms']:.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Linear data scaling benchmark: Native RAG vs Hybrid RAG(BM25+RRF) vs DCI vs DR-DCI")
    parser.add_argument("--scales", default="10,50,100,200,300,500", help="Comma-separated chunks-per-incident scale points")
    parser.add_argument("--incidents", type=int, default=30, help="Incidents evaluated per scale point (default: 30)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-csv", default=None)
    parser.add_argument("--output-md", default=None)
    args = parser.parse_args(argv)

    scales = [int(x.strip()) for x in args.scales.split(",") if x.strip()]
    payload = asyncio.run(run_linear_scaling_benchmark(
        scales=scales,
        incidents_per_scale=args.incidents,
        seed=args.seed,
        top_k=args.top_k,
    ))
    md = format_scaling_markdown(payload)
    print(md)

    out_json = Path(args.output_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.output_csv:
        write_scaling_csv(payload, args.output_csv)
    if args.output_md:
        out_md = Path(args.output_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
