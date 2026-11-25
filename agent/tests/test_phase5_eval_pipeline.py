from __future__ import annotations

import csv
import json
from pathlib import Path

from eval.dr_dci_baseline import (
    CorpusChunkFixture,
    DrDciScenario,
    _measure_reasoning_context_chars,
    benchmark_keyword_dr_dci,
    main as dr_dci_cli_main,
)
from eval.harness import main as harness_cli_main
from eval.plot import generate_judge_charts, main as plot_cli_main
from log_agent.schemas import EvidenceItem, LogInvestigationResponse


def test_harness_cli_outputs_p99_json_and_csv(tmp_path: Path):
    """평가 하네스 CLI가 latency_p99_ms와 케이스별 원시 CSV를 정상 출력한다."""
    out_json = tmp_path / "judge.json"
    out_csv = tmp_path / "judge.csv"
    out_md = tmp_path / "judge.md"

    code = harness_cli_main([
        "--judges", "rule_based",
        "--max-fsr", "0.05",
        "--output-json", str(out_json),
        "--output-csv", str(out_csv),
        "--output-md", str(out_md),
    ])
    assert code == 0

    reports = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(reports) == 1
    rep = reports[0]
    assert "latency_p99_ms" in rep
    assert rep["latency_p99_ms"] >= rep["latency_p95_ms"] >= rep["latency_p50_ms"] >= 0.0
    assert "p50 / p95 / p99 (ms)" in out_md.read_text(encoding="utf-8")

    with out_csv.open("r", encoding="utf-8", newline="") as fp:
        rows = list(csv.DictReader(fp))
    assert len(rows) == 30
    assert rows[0]["judge_name"] == "rule_based"
    assert rows[0]["case_id"] == "INC-EVAL-001"


async def test_keyword_dr_dci_baseline_measures_recall_and_context_reduction():
    """키워드 DR-DCI 기준선 러너가 예산 증가에 따른 회수율·압축률·p99를 계산한다."""
    payload = await benchmark_keyword_dr_dci(budgets=(1, 3, 5), repeats=2)
    assert payload["schema_version"] == "dr-dci-baseline-v1"
    assert payload["scenario_count"] == 5
    assert len(payload["runs"]) == 15

    by_budget = {row["max_pull_steps"]: row for row in payload["summary_by_budget"]}
    assert by_budget[5]["mean_evidence_recall"] >= by_budget[1]["mean_evidence_recall"]
    assert by_budget[5]["mean_citation_validity"] == 1.0
    assert 0.0 <= by_budget[3]["mean_char_reduction_ratio"] < 1.0
    assert by_budget[3]["latency_p99_ms"] >= by_budget[3]["latency_p50_ms"] >= 0.0


def test_plot_cli_generates_png_charts_for_single_and_multi_judge_and_dr_dci(tmp_path: Path):
    """matplotlib/seaborn 플로팅 CLI가 단일·복수 판정기 및 DR-DCI 예산 곡선 PNG를 생성한다."""
    judge_json = tmp_path / "judge.json"
    dr_dci_json = tmp_path / "dr_dci.json"
    dr_dci_csv = tmp_path / "dr_dci.csv"
    charts_dir = tmp_path / "charts"

    assert harness_cli_main(["--judges", "rule_based", "--output-json", str(judge_json)]) == 0
    assert dr_dci_cli_main([
        "--budgets", "1,2,3,4,5",
        "--repeats", "2",
        "--output-json", str(dr_dci_json),
        "--output-csv", str(dr_dci_csv),
    ]) == 0
    assert dr_dci_csv.is_file()

    assert plot_cli_main([
        "--judge-json", str(judge_json),
        "--dr-dci-json", str(dr_dci_json),
        "--output-dir", str(charts_dir),
        "--prefix", "test5",
    ]) == 0

    for filename in (
        "test5-confusion-matrix.png",
        "test5-category-metrics.png",
        "test5-latency-distribution.png",
        "test5-dr-dci-budget-curve.png",
    ):
        target = charts_dir / filename
        assert target.is_file()
        assert target.stat().st_size > 5_000

    # 복수 판정기 JSON 입력 시 종합 비교 차트도 추가 생성되어야 한다
    single = json.loads(judge_json.read_text(encoding="utf-8"))
    clone = dict(single[0])
    clone["judge_name"] = "mock_llm"
    clone["model"] = "gpt-4o-mini"
    multi_json = tmp_path / "multi.json"
    multi_json.write_text(json.dumps([single[0], clone], ensure_ascii=False), encoding="utf-8")

    generated = generate_judge_charts(multi_json, tmp_path / "multi_charts", prefix="cmp")
    assert any(p.name == "cmp-judge-comparison.png" and p.stat().st_size > 5_000 for p in generated)


async def test_dr_dci_baseline_counts_long_runbook_summary_and_deduplicates():
    """400자 초과 긴 런북의 runbookSummary와 중복/비중복 요약이 추론 컨텍스트 문자 수에 정확히 반영된다."""
    long_runbook = "1. 증상 확인 및 롤백 절차: " + ("A" * 650)
    scenario = DrDciScenario(
        scenario_id="LONG-RB-01",
        incident_code="INC-LONG-1",
        service_name="payment-service",
        category="db_connection_pool",
        query_keywords=("rollback",),
        required_chunk_ids=("RB-LONG",),
        chunks=(
            CorpusChunkFixture("RB-LONG", "RUNBOOK", None, "payment-service", "긴 런북", "rollback", long_runbook, 1),
        ),
    )
    payload = await benchmark_keyword_dr_dci([scenario], budgets=(1,), repeats=1, max_chunk_chars=4000)
    run = payload["runs"][0]
    # runbookSummary(전체 ~666자) + 축약된 evidence summary(400자, 내용이 다르므로 append) 모두 반영되어야 함
    assert run["context_summary_chars"] > len(long_runbook)

    # 동일 요약인 경우 중복 추가되지 않아야 함
    same_res = LogInvestigationResponse(
        ok=True,
        incidentCode="INC-SAME",
        evidence=[
            EvidenceItem(chunkId="LOG-1", corpusType="LOG", title="t", summary="error 502"),
            EvidenceItem(chunkId="RB-1", corpusType="RUNBOOK", title="rb", summary="short runbook"),
        ],
        evidenceChunkIds=["LOG-1", "RB-1"],
        runbookSummary="short runbook",
    )
    assert _measure_reasoning_context_chars(same_res) == len("short runbook") + len("[LOG-1] error 502")


async def test_dr_dci_baseline_pools_raw_latency_samples_across_scenarios(monkeypatch):
    """예산별 지연 백분위는 시나리오별 백분위의 백분위가 아니라 전체 원시 샘플을 풀링해 계산한다."""
    import eval.dr_dci_baseline as mod

    scenarios = [
        DrDciScenario(
            scenario_id=f"S-{i}",
            incident_code=f"INC-{i}",
            service_name="svc",
            category="db_connection_pool",
            query_keywords=("err",),
            required_chunk_ids=(f"L-{i}",),
            chunks=(CorpusChunkFixture(f"L-{i}", "LOG", f"INC-{i}", "svc", "err", "err", "err", 1),),
        )
        for i in range(5)
    ]

    # 4개 시나리오는 1ms, 마지막 1개 시나리오는 100ms가 걸리도록 perf_counter를 모킹 (repeats=4 -> 총 20개 샘플 중 4개가 100ms)
    ticks = iter(
        val
        for dur in ([0.001] * 16 + [0.100] * 4)
        for val in (0.0, dur)
    )
    monkeypatch.setattr(mod.time, "perf_counter", lambda: next(ticks))

    payload = await benchmark_keyword_dr_dci(scenarios, budgets=(1,), repeats=4)
    summary = payload["summary_by_budget"][0]
    # 풀링된 20개 샘플: 1.0 * 16개, 100.0 * 4개 -> p95(rank=18.05)와 p99(rank=18.81)는 모두 100.0ms여야 함
    assert summary["latency_p50_ms"] == 1.0
    assert summary["latency_p95_ms"] == 100.0
    assert summary["latency_p99_ms"] == 100.0
