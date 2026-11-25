from __future__ import annotations

import json
from pathlib import Path
import re

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
