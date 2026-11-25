from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import time
from typing import Any, Sequence

from common.config import Settings
from eval.harness import _percentile
from log_agent.schemas import LogInvestigationRequest
from log_agent.services import run_dr_dci_investigation


@dataclass(frozen=True)
class CorpusChunkFixture:
    """오프라인 DR-DCI 기준선 평가용 단일 코퍼스 청크."""

    chunk_id: str
    corpus_type: str  # "LOG" | "RUNBOOK"
    incident_code: str | None
    service_name: str
    title: str
    keywords: str
    content: str
    sequence_order: int


@dataclass(frozen=True)
class DrDciScenario:
    """DR-DCI 인출 전략 기준선 측정용 장애 시나리오."""

    scenario_id: str
    incident_code: str
    service_name: str
    category: str
    query_keywords: tuple[str, ...]
    required_chunk_ids: tuple[str, ...]
    chunks: tuple[CorpusChunkFixture, ...]


@dataclass(frozen=True)
class DrDciRunMetric:
    """단일 (시나리오, budget) 조합의 DR-DCI 실행 실측 지표."""

    strategy: str
    scenario_id: str
    incident_code: str
    category: str
    max_pull_steps: int
    total_corpus_chunks: int
    total_corpus_chars: int
    pulled_chunk_ids: list[str]
    pulled_count: int
    pulled_raw_chars: int
    context_summary_chars: int
    char_reduction_ratio: float
    required_chunk_ids: list[str]
    evidence_recall: float
    citation_validity: float
    search_calls: int
    pull_calls: int
    recommended_command: str | None
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float


class InMemoryCorpusBackend:
    """Spring MCP searchCorpusIndex / pullCorpusChunk 계약을 인메모리로 재현하는 스텁."""

    def __init__(self, chunks: Sequence[CorpusChunkFixture]) -> None:
        self._chunks = list(chunks)
        self._by_id = {c.chunk_id: c for c in chunks}

    async def search_corpus_index(
        self,
        incident_code: str,
        corpus_type: str,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        kw = (keyword or "").strip().lower()
        results: list[dict[str, Any]] = []
        for chunk in self._chunks:
            if chunk.corpus_type != corpus_type:
                continue
            if corpus_type == "LOG" and chunk.incident_code != incident_code:
                continue
            if kw:
                haystack = f"{chunk.title} {chunk.keywords} {chunk.service_name}".lower()
                if kw not in haystack:
                    continue
            results.append({
                "chunkId": chunk.chunk_id,
                "corpusType": chunk.corpus_type,
                "incidentCode": chunk.incident_code,
                "serviceName": chunk.service_name,
                "title": chunk.title,
                "keywords": chunk.keywords,
                "sequenceOrder": chunk.sequence_order,
            })
        results.sort(key=lambda r: int(r["sequenceOrder"]))
        return results

    async def pull_corpus_chunk(self, incident_code: str, chunk_id: str) -> dict[str, Any]:
        chunk = self._by_id.get(chunk_id)
        if chunk is None:
            raise KeyError(f"chunk not found: {chunk_id}")
        if chunk.corpus_type == "LOG" and chunk.incident_code != incident_code:
            raise KeyError(f"chunk out of incident scope: {chunk_id}")
        return {
            "chunkId": chunk.chunk_id,
            "corpusType": chunk.corpus_type,
            "incidentCode": chunk.incident_code,
            "serviceName": chunk.service_name,
            "title": chunk.title,
            "keywords": chunk.keywords,
            "content": chunk.content,
            "sequenceOrder": chunk.sequence_order,
        }


def build_default_baseline_scenarios() -> list[DrDciScenario]:
    """db/demo/data.sql과 대표 장애 원형을 반영한 오프라인 DR-DCI 기준선 시나리오 5종을 반환한다."""
    payment_chunks = (
        CorpusChunkFixture(
            "LOG-101", "LOG", "INC-2026-001", "payment-service",
            "13:50-13:55 정상 트래픽 및 헬스체크 로그", "healthcheck,200,normal",
            "2026-11-18 13:50:12 INFO [http-nio-8080-exec-1] GET /actuator/health 200 OK (4ms)\n"
            "2026-11-18 13:52:40 INFO [http-nio-8080-exec-3] POST /api/v1/payments txId=TX9001 200 OK (48ms)", 1,
        ),
        CorpusChunkFixture(
            "LOG-102", "LOG", "INC-2026-001", "payment-service",
            "13:56-13:59 배포 롤아웃 시작 이벤트", "deploy,rollout,v2.4.1",
            "2026-11-18 13:56:05 INFO [k8s-agent] Rolling update started for deployment/payment-service image=payment-service:v2.4.1\n"
            "2026-11-18 13:58:19 INFO [main] Started PaymentApplication in 6.412 seconds", 2,
        ),
        CorpusChunkFixture(
            "LOG-103", "LOG", "INC-2026-001", "payment-service",
            "14:00-14:01 외부 PG사 웹훅 수신 로그", "webhook,pg,callback",
            "2026-11-18 14:00:11 INFO [http-nio-8080-exec-7] POST /api/v1/webhooks/pg-confirm 200 OK (31ms)\n"
            "2026-11-18 14:00:49 WARN [http-nio-8080-exec-9] Slow external rate query took 820ms", 3,
        ),
        CorpusChunkFixture(
            "LOG-104", "LOG", "INC-2026-001", "payment-service",
            "14:01-14:02 HikariPool 커넥션 타임아웃 급증", "HikariPool,Connection,timeout,30000ms,SQLTransientConnectionException",
            "2026-11-18 14:01:33 ERROR [http-nio-8080-exec-12] c.z.h.p.HikariPool : HikariPool-1 - Connection is not available, request timed out after 30000ms (total=20, active=20, idle=0, waiting=47)\n"
            "2026-11-18 14:01:34 ERROR [http-nio-8080-exec-14] java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms", 4,
        ),
        CorpusChunkFixture(
            "LOG-105", "LOG", "INC-2026-001", "payment-service",
            "14:02 커넥션 누수 감지 스택트레이스 (v2.4.1)", "leak,Connection,PaymentLedgerService,v2.4.1",
            "2026-11-18 14:02:05 WARN [HikariPool-1 housekeeper] c.z.h.p.ProxyLeakTask : Connection leak detection triggered for conn0 on thread http-nio-8080-exec-4, stack trace follows\n"
            "java.lang.Exception: Apparent connection leak detected\n"
            "\tat com.incidentops.payment.service.PaymentLedgerService.recordAsyncAudit(PaymentLedgerService.java:118)", 5,
        ),
        CorpusChunkFixture(
            "LOG-106", "LOG", "INC-2026-001", "payment-service",
            "14:02-14:03 게이트웨이 502 Upstream 응답 기록", "502,Bad Gateway,upstream",
            "2026-11-18 14:02:18 ERROR [caddy] upstream http://payment-service:8080 returned 502 Bad Gateway after 30005ms\n"
            "2026-11-18 14:02:45 ERROR [caddy] upstream http://payment-service:8080 returned 502 Bad Gateway after 30002ms", 6,
        ),
        CorpusChunkFixture(
            "RUNBOOK-01", "RUNBOOK", None, "payment-service",
            "DB 커넥션 풀 고갈 및 누수 긴급 복구 런북", "HikariPool,leak,rollback,kubectl",
            "1. 증상: HikariPool active=total, idle=0 상태가 지속되며 SQLTransientConnectionException 발생\n"
            "2. 원인 판별: ProxyLeakTask 로그가 최근 배포 직후 발생했다면 신규 버전의 트랜잭션/커넥션 미반환 버그임\n"
            "3. 표준 조치: 즉시 이전 안정 버전으로 롤백 실행\n"
            "kubectl rollout undo deployment/payment-service\n"
            "4. 검증: 롤백 후 2분 내 HikariPool active 커넥션이 5 이하로 감소하고 502 에러율이 0% 로 복귀하는지 확인", 1,
        ),
        CorpusChunkFixture(
            "RUNBOOK-02", "RUNBOOK", None, "payment-service",
            "단순 트래픽 폭증 시 HikariPool 증설 가이드", "traffic,spike,maximum-pool-size",
            "1. 조건: 커넥션 누수(ProxyLeakTask)가 없고 순수 TPS 증가로 인한 대기열 발생 시에만 적용\n"
            "2. 조치: DB max_connections 여유분 확인 후 maximum-pool-size 를 20에서 40으로 상향 조정", 2,
        ),
    )

    oom_chunks = (
        CorpusChunkFixture(
            "OOM-101", "LOG", "INC-2026-002", "checkout-worker",
            "09:10 정상 배치 처리 로그", "batch,200,normal",
            "09:10:01 INFO processed 1200 orders in 320ms heap=42%", 1,
        ),
        CorpusChunkFixture(
            "OOM-102", "LOG", "INC-2026-002", "checkout-worker",
            "09:15 v3.8.0 배포 완료", "deploy,rollout,v3.8.0",
            "09:15:10 INFO deployment/checkout-worker rolled out image=checkout-worker:v3.8.0", 2,
        ),
        CorpusChunkFixture(
            "OOM-103", "LOG", "INC-2026-002", "checkout-worker",
            "09:18 캐시 워밍업 로그", "cache,warmup",
            "09:18:02 INFO cache warmup completed keys=8200", 3,
        ),
        CorpusChunkFixture(
            "OOM-104", "LOG", "INC-2026-002", "checkout-worker",
            "09:20 OutOfMemoryError 및 OOMKilled 반복", "OOMKilled,memory,error,heap,leak",
            "09:20:11 ERROR java.lang.OutOfMemoryError: Java heap space in InvoiceBufferCache\n"
            "09:20:14 ERROR pod checkout-worker-7c9f terminated reason=OOMKilled exitCode=137", 4,
        ),
        CorpusChunkFixture(
            "OOM-105", "LOG", "INC-2026-002", "checkout-worker",
            "09:21 CrashLoopBackOff 이벤트", "CrashLoopBackOff,restart,error",
            "09:21:05 WARN Back-off restarting failed container checkout-worker (restarts=4)", 5,
        ),
        CorpusChunkFixture(
            "OOM-RB-01", "RUNBOOK", None, "checkout-worker",
            "배포 직후 OOMKilled 긴급 롤백 런북", "OOMKilled,memory,rollback,kubectl",
            "1. 증상: 배포 직후 힙 누수로 OOMKilled/CrashLoopBackOff 발생\n"
            "2. 조치:\nkubectl rollout undo deployment/checkout-worker", 1,
        ),
    )

    ext_dep_chunks = (
        CorpusChunkFixture(
            "EXT-101", "LOG", "INC-2026-003", "billing-gateway",
            "11:00 정상 결제 승인 로그", "billing,200,normal",
            "11:00:04 INFO PG approval ok latency=110ms", 1,
        ),
        CorpusChunkFixture(
            "EXT-102", "LOG", "INC-2026-003", "billing-gateway",
            "11:05 최근 배포 없음 확인", "audit,no-deploy",
            "11:05:00 INFO last deployment was 9 days ago (v1.12.0)", 2,
        ),
        CorpusChunkFixture(
            "EXT-103", "LOG", "INC-2026-003", "billing-gateway",
            "11:10 외부 PG 업스트림 503 타임아웃 급증", "upstream,pg,503,timeout,error",
            "11:10:19 ERROR upstream PG provider https://pg.vendor.example returned 503 Service Unavailable (timeout 10000ms)\n"
            "11:10:22 ERROR vendor status page reports regional PG outage", 3,
        ),
        CorpusChunkFixture(
            "EXT-104", "LOG", "INC-2026-003", "billing-gateway",
            "11:11 서킷 브레이커 오픈", "circuit-breaker,502,error",
            "11:11:02 WARN circuit breaker pg-primary transitioned to OPEN state", 4,
        ),
        CorpusChunkFixture(
            "EXT-RB-01", "RUNBOOK", None, "billing-gateway",
            "외부 PG 장애 시 롤백 금지 및 우회 가이드", "pg,upstream,rollback,failover",
            "1. 외부 PG 업스트림 장애는 애플리케이션 롤백으로 해결되지 않으므로 롤백 금지\n"
            "2. 보조 PG 라우팅 전환 절차를 따른다.", 1,
        ),
    )

    schema_chunks = (
        CorpusChunkFixture(
            "SCH-101", "LOG", "INC-2026-004", "order-service",
            "16:00 Flyway V18 마이그레이션 적용", "flyway,migration,deploy,schema",
            "16:00:12 INFO Flyway applied V18__drop_legacy_status_column.sql (DROP COLUMN legacy_status)", 1,
        ),
        CorpusChunkFixture(
            "SCH-102", "LOG", "INC-2026-004", "order-service",
            "16:02 주문 조회 500 오류 급증", "500,error,SQLSyntaxErrorException",
            "16:02:18 ERROR Unknown column 'legacy_status' in 'field list' during rollback canary test", 2,
        ),
        CorpusChunkFixture(
            "SCH-103", "LOG", "INC-2026-004", "order-service",
            "16:03 일반 트래픽 로그", "http,info",
            "16:03:00 INFO GET /health 200 OK", 3,
        ),
        CorpusChunkFixture(
            "SCH-RB-01", "RUNBOOK", None, "order-service",
            "비가역 스키마 마이그레이션 장애 대응 런북", "migration,schema,rollback",
            "1. DROP COLUMN 등 비가역 마이그레이션 이후 구버전 롤백은 하위 호환 불가로 금지\n"
            "2. 핫픽스 패치(forward fix)로 복구한다.", 1,
        ),
    )

    telemetry_chunks = (
        CorpusChunkFixture(
            "TEL-101", "LOG", "INC-2026-005", "search-api",
            "18:00 배포 이벤트", "deploy,v5.2.0",
            "18:00:05 INFO deployment/search-api v5.2.0 rollout complete", 1,
        ),
        CorpusChunkFixture(
            "TEL-102", "LOG", "INC-2026-005", "search-api",
            "18:04 Prometheus 스크래핑 지연 경고", "prometheus,metrics,scrape,timeout",
            "18:04:10 WARN metrics scrape delayed 180s; candidate release telemetry missing", 2,
        ),
        CorpusChunkFixture(
            "TEL-103", "LOG", "INC-2026-005", "search-api",
            "18:05 간헐적 502 로그 2건", "502,error",
            "18:05:11 ERROR 502 Bad Gateway count=2 sample_size=12", 3,
        ),
        CorpusChunkFixture(
            "TEL-RB-01", "RUNBOOK", None, "search-api",
            "검색 API 502 롤백 검증 런북", "502,rollback,metrics,kubectl",
            "1. 최소 100건 이상 표본과 이전 버전 정상 지표 확인 후에만 롤백\n"
            "kubectl rollout undo deployment/search-api", 1,
        ),
    )

    return [
        DrDciScenario(
            scenario_id="DRDCI-BASE-01",
            incident_code="INC-2026-001",
            service_name="payment-service",
            category="db_connection_pool",
            query_keywords=("HikariPool", "timeout", "leak", "502"),
            required_chunk_ids=("LOG-104", "LOG-105", "RUNBOOK-01"),
            chunks=payment_chunks,
        ),
        DrDciScenario(
            scenario_id="DRDCI-BASE-02",
            incident_code="INC-2026-002",
            service_name="checkout-worker",
            category="memory_oom",
            query_keywords=("OOMKilled", "memory", "CrashLoopBackOff"),
            required_chunk_ids=("OOM-104", "OOM-RB-01"),
            chunks=oom_chunks,
        ),
        DrDciScenario(
            scenario_id="DRDCI-BASE-03",
            incident_code="INC-2026-003",
            service_name="billing-gateway",
            category="external_dependency",
            query_keywords=("upstream", "pg", "503", "timeout"),
            required_chunk_ids=("EXT-103", "EXT-RB-01"),
            chunks=ext_dep_chunks,
        ),
        DrDciScenario(
            scenario_id="DRDCI-BASE-04",
            incident_code="INC-2026-004",
            service_name="order-service",
            category="schema_migration",
            query_keywords=("flyway", "migration", "500"),
            required_chunk_ids=("SCH-101", "SCH-102", "SCH-RB-01"),
            chunks=schema_chunks,
        ),
        DrDciScenario(
            scenario_id="DRDCI-BASE-05",
            incident_code="INC-2026-005",
            service_name="search-api",
            category="telemetry_gap",
            query_keywords=("metrics", "502", "deploy"),
            required_chunk_ids=("TEL-102", "TEL-RB-01"),
            chunks=telemetry_chunks,
        ),
    ]


def _measure_reasoning_context_chars(res: Any) -> int:
    """remediation_agent.build_remediation_plan과 동일한 조립·중복제거 규칙으로 추론 컨텍스트 문자 수를 측정한다."""
    runbook_parts = [res.runbookSummary.strip()] if res.runbookSummary and res.runbookSummary.strip() else []
    evidence_lines: list[str] = []
    for item in res.evidence:
        if (item.corpusType or "").strip().upper() == "RUNBOOK":
            summary = item.summary.strip()
            if summary and summary not in runbook_parts:
                runbook_parts.append(summary)
        else:
            evidence_lines.append(f"[{item.chunkId}] {item.summary}")
    return len("\n".join(runbook_parts)) + len("\n".join(evidence_lines))


async def benchmark_keyword_dr_dci(
    scenarios: Sequence[DrDciScenario] | None = None,
    *,
    budgets: Sequence[int] = (1, 2, 3, 4, 5),
    repeats: int = 5,
    max_chunk_chars: int = 4000,
) -> dict[str, Any]:
    """기존 키워드 기반 DR-DCI(log_agent.services.run_dr_dci_investigation)를 예산별로 실측한다."""
    items = list(scenarios) if scenarios is not None else build_default_baseline_scenarios()
    if not items:
        raise ValueError("At least one DR-DCI scenario is required.")
    if repeats < 1:
        raise ValueError("repeats must be >= 1")

    runs: list[DrDciRunMetric] = []
    pooled_latencies_by_budget: dict[int, list[float]] = {}
    for budget in budgets:
        pooled_latencies: list[float] = []
        settings = Settings(
            _env_file=None,
            dr_dci_max_pull_steps=budget,
            dr_dci_max_chunk_chars=max_chunk_chars,
        )
        for sc in items:
            backend = InMemoryCorpusBackend(sc.chunks)
            req = LogInvestigationRequest(
                incidentCode=sc.incident_code,
                serviceName=sc.service_name,
                keywords=list(sc.query_keywords),
                maxPulls=budget,
            )

            latencies: list[float] = []
            last_res = None
            for _ in range(repeats):
                t0 = time.perf_counter()
                last_res = await run_dr_dci_investigation(backend, settings, req)
                latencies.append(round((time.perf_counter() - t0) * 1000.0, 3))

            assert last_res is not None and last_res.ok
            pooled_latencies.extend(latencies)
            latencies.sort()

            total_chars = sum(len(c.content) for c in sc.chunks)
            pulled_ids = list(last_res.evidenceChunkIds)
            valid_chunk_ids = {c.chunk_id for c in sc.chunks}
            pulled_raw_chars = sum(
                int(entry.get("chars") or 0)
                for entry in last_res.toolTrace
                if entry.get("tool") == "pullCorpusChunk" and entry.get("ok")
            )
            context_summary_chars = _measure_reasoning_context_chars(last_res)
            reduction = round(1.0 - (context_summary_chars / total_chars), 4) if total_chars > 0 else 0.0

            required = set(sc.required_chunk_ids)
            recalled = len(required.intersection(pulled_ids))
            recall = round(recalled / len(required), 4) if required else 1.0
            citation_validity = (
                round(sum(1 for cid in pulled_ids if cid in valid_chunk_ids) / len(pulled_ids), 4)
                if pulled_ids
                else 1.0
            )
            search_calls = sum(1 for e in last_res.toolTrace if e.get("tool") == "searchCorpusIndex")
            pull_calls = sum(1 for e in last_res.toolTrace if e.get("tool") == "pullCorpusChunk")

            runs.append(DrDciRunMetric(
                strategy="keyword_dr_dci",
                scenario_id=sc.scenario_id,
                incident_code=sc.incident_code,
                category=sc.category,
                max_pull_steps=budget,
                total_corpus_chunks=len(sc.chunks),
                total_corpus_chars=total_chars,
                pulled_chunk_ids=pulled_ids,
                pulled_count=len(pulled_ids),
                pulled_raw_chars=pulled_raw_chars,
                context_summary_chars=context_summary_chars,
                char_reduction_ratio=reduction,
                required_chunk_ids=list(sc.required_chunk_ids),
                evidence_recall=recall,
                citation_validity=citation_validity,
                search_calls=search_calls,
                pull_calls=pull_calls,
                recommended_command=last_res.recommendedCommand,
                latency_p50_ms=_percentile(latencies, 50.0),
                latency_p95_ms=_percentile(latencies, 95.0),
                latency_p99_ms=_percentile(latencies, 99.0),
            ))
        pooled_latencies.sort()
        pooled_latencies_by_budget[budget] = pooled_latencies

    by_budget: list[dict[str, Any]] = []
    for budget in budgets:
        subset = [r for r in runs if r.max_pull_steps == budget]
        n = len(subset)
        pooled = pooled_latencies_by_budget[budget]
        by_budget.append({
            "strategy": "keyword_dr_dci",
            "max_pull_steps": budget,
            "scenarios": n,
            "mean_evidence_recall": round(sum(r.evidence_recall for r in subset) / n, 4),
            "mean_citation_validity": round(sum(r.citation_validity for r in subset) / n, 4),
            "mean_context_chars": round(sum(r.context_summary_chars for r in subset) / n, 1),
            "mean_total_corpus_chars": round(sum(r.total_corpus_chars for r in subset) / n, 1),
            "mean_char_reduction_ratio": round(sum(r.char_reduction_ratio for r in subset) / n, 4),
            "mean_pull_calls": round(sum(r.pull_calls for r in subset) / n, 2),
            "latency_p50_ms": _percentile(pooled, 50.0),
            "latency_p95_ms": _percentile(pooled, 95.0),
            "latency_p99_ms": _percentile(pooled, 99.0),
        })

    return {
        "schema_version": "dr-dci-baseline-v1",
        "strategy": "keyword_dr_dci",
        "scenario_count": len(items),
        "budgets": list(budgets),
        "repeats_per_case": repeats,
        "summary_by_budget": by_budget,
        "runs": [asdict(r) for r in runs],
    }


def write_dr_dci_csv(payload: dict[str, Any], output_path: str | Path) -> Path:
    """DR-DCI 기준선 실행 결과를 케이스별 CSV로 저장한다."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "strategy",
        "scenario_id",
        "incident_code",
        "category",
        "max_pull_steps",
        "total_corpus_chunks",
        "total_corpus_chars",
        "pulled_count",
        "pulled_chunk_ids",
        "pulled_raw_chars",
        "context_summary_chars",
        "char_reduction_ratio",
        "required_chunk_ids",
        "evidence_recall",
        "citation_validity",
        "search_calls",
        "pull_calls",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
    ]
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for row in payload["runs"]:
            flat = dict(row)
            flat["pulled_chunk_ids"] = ";".join(row["pulled_chunk_ids"])
            flat["required_chunk_ids"] = ";".join(row["required_chunk_ids"])
            flat.pop("recommended_command", None)
            writer.writerow(flat)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline keyword DR-DCI baseline benchmark.")
    parser.add_argument("--budgets", default="1,2,3,4,5", help="Comma-separated max_pull_steps budgets")
    parser.add_argument("--repeats", type=int, default=5, help="Timing repeats per (scenario, budget)")
    parser.add_argument("--output-json", required=True, help="Output JSON path")
    parser.add_argument("--output-csv", default=None, help="Optional output CSV path")
    args = parser.parse_args(argv)

    budgets = [int(x.strip()) for x in args.budgets.split(",") if x.strip()]
    payload = asyncio.run(benchmark_keyword_dr_dci(budgets=budgets, repeats=args.repeats))

    out_json = Path(args.output_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.output_csv:
        write_dr_dci_csv(payload, args.output_csv)

    print(f"Saved DR-DCI baseline ({len(payload['runs'])} runs) to {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
