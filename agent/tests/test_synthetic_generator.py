from __future__ import annotations

import json
from pathlib import Path
import re

from eval.plot import generate_scaling_charts
from eval.scaling_benchmark import main as scaling_cli_main
from eval.synthetic_generator import (
    generate_synthetic_incidents,
    main as synthetic_cli_main,
    write_mysql_seed_sql,
    write_synthetic_jsonl,
)


def test_synthetic_generator_is_deterministic_and_respects_mysql_column_limits():
    """동일 시드는 완전히 동일한 코퍼스를 생성하며, 모든 ID·문자열 길이가 MySQL Flyway 스키마 제약을 지킨다."""
    v1, c1 = generate_synthetic_incidents(count=200, seed=77, noise_chunks_per_incident=6)
    v2, c2 = generate_synthetic_incidents(count=200, seed=77, noise_chunks_per_incident=6)

    assert v1 == v2 == "dr-dci-synthetic-n200-s77"
    assert len(c1) == 200
    assert c1 == c2

    incident_codes = {c.incident_code for c in c1}
    assert len(incident_codes) == 200
    assert all(len(code) <= 32 for code in incident_codes)

    all_chunk_ids: set[str] = set()
    dev_arch: set[str] = set()
    eval_arch: set[str] = set()
    for case in c1:
        if case.split == "dev":
            dev_arch.add(case.archetype)
        else:
            eval_arch.add(case.archetype)
        for ch in case.chunks:
            assert len(ch.chunk_id) <= 32
            assert len(ch.service_name) <= 100
            assert len(ch.title) <= 200
            assert len(ch.keywords) <= 255
            assert ch.chunk_id not in all_chunk_ids, f"duplicate global chunk_id: {ch.chunk_id}"
            all_chunk_ids.add(ch.chunk_id)

    assert dev_arch.isdisjoint(eval_arch)


def test_synthetic_jsonl_and_mysql_seed_sql_generation(tmp_path: Path):
    """JSONL 코퍼스 및 MySQL 8.0 벌크 시드 SQL이 멱등 구문과 유효한 JSON steps로 생성된다."""
    _, cases = generate_synthetic_incidents(count=120, seed=42, noise_chunks_per_incident=5)

    jsonl_path = write_synthetic_jsonl(cases, tmp_path / "syn.jsonl")
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 120
    first = json.loads(lines[0])
    assert first["incident_code"] == "INC-SYN-0001"
    assert len(first["chunks"]) >= 3

    sql_path = tmp_path / "syn-seed.sql"
    stats = write_mysql_seed_sql(cases, sql_path, batch_size=50)
    assert stats["incidents"] == 120
    assert stats["corpus_chunks"] == sum(len(c.chunks) for c in cases)
    assert stats["remediation_plans"] == 40
    assert stats["remediation_approvals"] == 40

    sql_text = sql_path.read_text(encoding="utf-8")
    assert sql_text.startswith("-- Auto-generated")
    assert "START TRANSACTION;" in sql_text
    assert "COMMIT;" in sql_text
    assert "INSERT INTO incidents" in sql_text
    assert "INSERT INTO corpus_chunks" in sql_text
    assert "INSERT INTO remediation_plans" in sql_text
    assert "INSERT INTO remediation_approvals" in sql_text
    assert "ON DUPLICATE KEY UPDATE id = id;" in sql_text
    assert re.search(r"PLAN-SYN-0003", sql_text)
    assert re.search(r"APR-SYN-0003", sql_text)


def test_synthetic_cli_runs_end_to_end_with_benchmark_summary(tmp_path: Path):
    """CLI가 50건 합성 코퍼스 + JSONL + SQL + 경량 벤치마크 요약 JSON/MD를 오류 없이 생성한다."""
    out_jsonl = tmp_path / "c50.jsonl"
    out_sql = tmp_path / "c50.sql"
    out_summary = tmp_path / "summary.json"
    out_md = tmp_path / "summary.md"

    code = synthetic_cli_main([
        "--count", "50",
        "--seed", "123",
        "--noise-chunks", "4",
        "--output-jsonl", str(out_jsonl),
        "--output-sql", str(out_sql),
        "--run-benchmark",
        "--max-pull-steps", "3",
        "--benchmark-summary-json", str(out_summary),
        "--benchmark-md", str(out_md),
    ])
    assert code == 0
    assert out_jsonl.is_file()
    assert out_sql.is_file()
    assert out_md.is_file()

    summary = json.loads(out_summary.read_text(encoding="utf-8"))
    assert summary["total_cases"] == 50
    assert summary["total_corpus_chunks"] > 250
    assert len(summary["summaries"]) == 4
    assert summary["adoption_gate"]["passed"] is True


def test_linear_scaling_4paradigm_cli_and_charts(tmp_path: Path):
    """선형 스케일링 벤치마크(Native RAG / RAG+BM25+RRF / DCI / DR-DCI) CLI와 2종 차트 생성을 검증한다."""
    out_json = tmp_path / "scaling.json"
    out_csv = tmp_path / "scaling.csv"
    out_md = tmp_path / "scaling.md"

    code = scaling_cli_main([
        "--scales", "10,30",
        "--incidents", "6",
        "--seed", "42",
        "--top-k", "3",
        "--output-json", str(out_json),
        "--output-csv", str(out_csv),
        "--output-md", str(out_md),
    ])
    assert code == 0
    assert out_csv.is_file()
    assert out_md.is_file()

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(payload["points"]) == 8  # 2 scales * 4 paradigms
    p30 = {row["paradigm"]: row for row in payload["points"] if row["chunks_per_incident"] == 30}
    assert p30["dr_dci"]["mean_input_tokens"] < p30["dci"]["mean_input_tokens"]
    assert p30["dr_dci"]["mean_evidence_recall"] >= p30["native_rag"]["mean_evidence_recall"]

    charts = generate_scaling_charts(out_json, tmp_path / "charts", prefix="sc")
    assert len(charts) == 2
    for ch in charts:
        assert ch.is_file()
        assert ch.stat().st_size > 5_000


async def test_gpt6_models_omit_temperature_zero_for_structured_judge_and_selector():
    """gpt-6 계열 모델은 temperature=0을 거부하므로 StructuredOutputJudge와 OpenAiDrDciSelector가 temperature를 생략해 호출한다."""
    from types import SimpleNamespace
    from pydantic import SecretStr
    from common.config import Settings
    from common.decisions import StructuredOutputJudge
    from log_agent.agentic_dr_dci import OpenAiDrDciSelector

    seen_kwargs: list[dict] = []

    def fake_parse(**kwargs):
        seen_kwargs.append(dict(kwargs))
        if kwargs["response_format"].__name__ == "_StructuredVerdictSchema":
            parsed = SimpleNamespace(verdict="SUPPORTED", confidence=0.95, reason="ok")
        else:
            parsed = SimpleNamespace(action="STOP_SUFFICIENT", chunk_id=None, reason="done")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed, content=None, refusal=None))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=fake_parse)))
    cfg = Settings(_env_file=None, openai_api_key=SecretStr("sk-test"), openai_model="gpt-6.1-terra")

    res = await StructuredOutputJudge(cfg, openai_client=fake_client).judge_plan_evidence(
        proposed_action="rollback", runbook="rollback on 502", evidence="502 spike"
    )
    assert res.verdict == "SUPPORTED"

    dec = await OpenAiDrDciSelector(cfg, openai_client=fake_client).select_next_action(
        incident_code="INC-1",
        keywords=["502"],
        remaining_index=[],
        collected_evidence=[],
        step_no=1,
        max_steps=3,
    )
    assert dec.action == "STOP_SUFFICIENT"
    assert len(seen_kwargs) == 2
    assert all("temperature" not in kw for kw in seen_kwargs)


async def test_scaling_preserves_adversarial_decoys_and_checks_per_request_overflow():
    """스케일 확장 시 반례 경고/미끼 청크가 보존되고, 32k overflow는 누적 합이 아니라 단일 요청 최대 토큰 기준으로 판정한다."""
    from eval.scaling_benchmark import _expand_case_to_scale, run_linear_scaling_benchmark
    from eval.synthetic_generator import generate_synthetic_incidents

    _, base_cases = generate_synthetic_incidents(count=26, seed=42, noise_chunks_per_incident=2)
    misleading = next(c for c in base_cases if c.archetype == "adv_misleading_ref_eval")
    expanded = _expand_case_to_scale(misleading, target_chunks=20, seed=99)
    expanded_ids = {c.chunk_id for c in expanded.chunks}
    # 기준 케이스의 모든 청크(경고 로그 및 참조된 미끼 청크 포함)가 스케일 확장 후에도 보존되어야 함
    assert {c.chunk_id for c in misleading.chunks}.issubset(expanded_ids)

    # 단일 요청(selector 1스텝 및 judge)은 500 tok 미만이지만 누적 합은 500 tok 이상이 되도록 context_limit_tokens=500 테스트
    # -> 개별 요청 최대가 한도 미만이면 DR-DCI overflow=0%여야 함
    payload = await run_linear_scaling_benchmark(
        scales=(10,),
        incidents_per_scale=3,
        seed=42,
        top_k=3,
        context_limit_tokens=600,
    )
    pts = {p["paradigm"]: p for p in payload["points"]}
    assert pts["dci"]["overflow_32k_rate"] == 1.0
    assert pts["dr_dci"]["overflow_32k_rate"] == 0.0
