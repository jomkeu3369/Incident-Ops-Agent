from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence
import pytest
from pydantic import SecretStr, ValidationError

from common.config import Settings
from eval.dr_dci_baseline import CorpusChunkFixture, InMemoryCorpusBackend
from eval.dr_dci_benchmark import (
    evaluate_adoption_gate,
    format_four_way_markdown,
    main as benchmark_cli_main,
    run_four_way_benchmark,
)
from eval.dr_dci_corpus import build_dr_dci_benchmark_dataset
from eval.plot import generate_four_way_charts
from log_agent.agentic_dr_dci import (
    AdaptiveEvidenceSelector,
    OpenAiDrDciSelector,
    SelectorDecision,
    run_agentic_dr_dci_investigation,
)
from log_agent.schemas import EvidenceItem, LogInvestigationRequest
from log_agent.services import run_dr_dci_investigation


def test_dr_dci_settings_validation():
    """DR_DCI_MODE 및 DR_DCI_SELECTOR_MODE 설정이 허용 값만 통과시키고 정규화한다."""
    cfg = Settings(_env_file=None, dr_dci_mode="  AGENTIC ", dr_dci_selector_mode=" OPENAI ")
    assert cfg.dr_dci_mode == "agentic"
    assert cfg.dr_dci_selector_mode == "openai"

    with pytest.raises(ValidationError):
        Settings(_env_file=None, dr_dci_mode="vector_only")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, dr_dci_selector_mode="random")


async def test_agentic_dr_dci_pivots_on_multihop_clue_and_stops_early():
    """에이전틱 DR-DCI가 1차 로그 본문의 단서로 2차 원인 청크를 동적 추적하고 런북 확보 후 조기 종료한다."""
    chunks = (
        CorpusChunkFixture("LOG-01", "LOG", "INC-MH", "payment-service", "정상 헬스체크", "health,200", "INFO 200 OK", 1),
        CorpusChunkFixture("LOG-02", "LOG", "INC-MH", "payment-service", "게이트웨이 502 급증", "502,timeout", "v44 배포 직후 502 급증. 상세 원인은 LOG-05 참조.", 2),
        CorpusChunkFixture("LOG-05", "LOG", "INC-MH", "payment-service", "워커 스레드 감사", "worker,audit", "WARN ProxyLeakTask Apparent connection leak detected in v44.", 5),
        CorpusChunkFixture("RB-01", "RUNBOOK", None, "payment-service", "DB 커넥션 누수 롤백 런북", "HikariPool,leak,rollback,kubectl",
                           "1. 배포 직후 커넥션 누수 확인 시 롤백을 실행한다.\nkubectl rollout undo deployment/payment-service", 1),
    )
    backend = InMemoryCorpusBackend(chunks)
    cfg = Settings(_env_file=None, dr_dci_mode="agentic", dr_dci_max_pull_steps=5)
    req = LogInvestigationRequest(incidentCode="INC-MH", keywords=["502", "timeout"], maxPulls=5)

    res = await run_dr_dci_investigation(backend, cfg, req)
    assert res.ok is True
    assert set(res.evidenceChunkIds) == {"LOG-02", "LOG-05", "RB-01"}
    assert res.pulledCount == 3
    assert res.stopReason == "STOP_SUFFICIENT"
    assert res.recommendedCommand == "kubectl rollout undo deployment/payment-service"
    assert len(res.decisionTrace) == 4  # 3회 PULL + 1회 STOP_SUFFICIENT


async def test_agentic_dr_dci_server_guards_reject_hallucinated_or_duplicate_chunks_and_enforce_budgets():
    """서버 가드가 허용 목록 밖 청크 ID, 중복 인출, 컨텍스트 문자 예산을 엄격히 차단한다."""
    chunks = (
        CorpusChunkFixture("LOG-01", "LOG", "INC-G", "svc", "502 오류", "502", "A" * 350, 1),
        CorpusChunkFixture("LOG-02", "LOG", "INC-G", "svc", "추가 로그", "502", "B" * 350, 2),
        CorpusChunkFixture("RB-01", "RUNBOOK", None, "svc", "런북", "rollback", "C" * 350, 1),
    )

    class _HallucinatingSelector:
        name = "hallucinator"

        async def select_next_action(self, **kwargs: Any) -> SelectorDecision:
            return SelectorDecision(action="PULL_CHUNK", chunk_id="OTHER-INCIDENT-999", reason="invented")

    res_bad = await run_agentic_dr_dci_investigation(
        InMemoryCorpusBackend(chunks),
        Settings(_env_file=None, dr_dci_max_pull_steps=3),
        LogInvestigationRequest(incidentCode="INC-G", keywords=["502"], maxPulls=3),
        selector=_HallucinatingSelector(),
    )
    assert res_bad.ok is True
    assert res_bad.pulledCount == 0
    assert res_bad.stopReason == "GUARD_REJECTED_INVALID_CHUNK"
    assert res_bad.decisionTrace[0]["guardRejected"] is True

    class _DuplicateSelector:
        name = "duplicator"

        async def select_next_action(self, **kwargs: Any) -> SelectorDecision:
            return SelectorDecision(action="PULL_CHUNK", chunk_id="LOG-01", reason="repeat")

    res_dup = await run_agentic_dr_dci_investigation(
        InMemoryCorpusBackend(chunks),
        Settings(_env_file=None, dr_dci_max_pull_steps=3),
        LogInvestigationRequest(incidentCode="INC-G", keywords=["502"], maxPulls=3),
        selector=_DuplicateSelector(),
    )
    assert res_dup.pulledCount == 1
    assert res_dup.stopReason == "GUARD_REJECTED_INVALID_CHUNK"
    assert res_dup.decisionTrace[1]["guardRejected"] is True

    class _SequentialSelector:
        name = "seq"

        async def select_next_action(self, *, remaining_index: Sequence[dict[str, Any]], **kwargs: Any) -> SelectorDecision:
            return SelectorDecision(action="PULL_CHUNK", chunk_id=str(remaining_index[0]["chunkId"]), reason="next")

    # 컨텍스트 문자 예산 400자: 첫 350자 인출 후 두 번째 청크는 남은 50자 이내로 캡핑되고 누적 400자 도달로 안전 종료
    res_budget = await run_agentic_dr_dci_investigation(
        InMemoryCorpusBackend(chunks),
        Settings(_env_file=None, dr_dci_max_pull_steps=5, dr_dci_max_context_chars=400),
        LogInvestigationRequest(incidentCode="INC-G", keywords=["502"], maxPulls=5),
        selector=_SequentialSelector(),
    )
    assert res_budget.pulledCount == 2
    assert res_budget.stopReason == "CONTEXT_CHAR_BUDGET_REACHED"
    assert sum(len(e.summary) for e in res_budget.evidence) <= 400

    # 1-pull에서도 400자 예산보다 긴 1000자 런북이 예산을 초과하지 않도록 캡핑되어야 함
    long_rb_chunks = (
        CorpusChunkFixture("RB-LONG", "RUNBOOK", None, "svc", "긴 런북", "rollback", "X" * 1000, 1),
        CorpusChunkFixture("LOG-01", "LOG", "INC-G", "svc", "로그", "502", "502 error", 1),
    )

    class _PickRunbookSelector:
        name = "pick_rb"

        async def select_next_action(self, **kwargs: Any) -> SelectorDecision:
            return SelectorDecision(action="PULL_CHUNK", chunk_id="RB-LONG", reason="pick long runbook")

    res_rb_cap = await run_agentic_dr_dci_investigation(
        InMemoryCorpusBackend(long_rb_chunks),
        Settings(_env_file=None, dr_dci_max_pull_steps=1, dr_dci_max_context_chars=400),
        LogInvestigationRequest(incidentCode="INC-G", keywords=["rollback"], maxPulls=1),
        selector=_PickRunbookSelector(),
    )
    assert len(res_rb_cap.runbookSummary) == 400
    assert res_rb_cap.stopReason == "CONTEXT_CHAR_BUDGET_REACHED"


async def test_agentic_dr_dci_enforces_timeout_around_awaited_ops_and_preserves_failed_chunks():
    """느린 선택기/MCP 호출은 dr_dci_timeout_seconds 데드라인으로 중단되고, 일시 실패 청크는 remainingChunkIds에 보존된다."""
    import asyncio
    from common.backend_client import BackendApiError

    chunks = (
        CorpusChunkFixture("LOG-01", "LOG", "INC-T", "svc", "502 오류", "502", "502 error", 1),
        CorpusChunkFixture("RB-01", "RUNBOOK", None, "svc", "런북", "rollback", "rollback", 1),
    )

    class _SlowSelector:
        name = "slow"

        async def select_next_action(self, **kwargs: Any) -> SelectorDecision:
            await asyncio.sleep(2.0)
            return SelectorDecision(action="PULL_CHUNK", chunk_id="LOG-01", reason="slow")

    res_timeout = await run_agentic_dr_dci_investigation(
        InMemoryCorpusBackend(chunks),
        Settings(_env_file=None, dr_dci_max_pull_steps=3, dr_dci_timeout_seconds=0.5),
        LogInvestigationRequest(incidentCode="INC-T", keywords=["502"], maxPulls=3),
        selector=_SlowSelector(),
    )
    assert res_timeout.ok is True
    assert res_timeout.stopReason == "TIMEOUT_BUDGET_REACHED"
    assert res_timeout.pulledCount == 0

    class _FailingPullBackend(InMemoryCorpusBackend):
        async def pull_corpus_chunk(self, incident_code: str, chunk_id: str) -> dict[str, Any]:
            if chunk_id == "LOG-01":
                raise BackendApiError(502, "transient backend error")
            return await super().pull_corpus_chunk(incident_code, chunk_id)

    class _PickFirstSelector:
        name = "pick_first"

        async def select_next_action(self, *, remaining_index: Sequence[dict[str, Any]], **kwargs: Any) -> SelectorDecision:
            return SelectorDecision(action="PULL_CHUNK", chunk_id=str(remaining_index[0]["chunkId"]), reason="pick")

    res_fail = await run_agentic_dr_dci_investigation(
        _FailingPullBackend(chunks),
        Settings(_env_file=None, dr_dci_max_pull_steps=3),
        LogInvestigationRequest(incidentCode="INC-T", keywords=["502"], maxPulls=3),
        selector=_PickFirstSelector(),
    )
    assert res_fail.ok is True
    assert "RB-01" in res_fail.evidenceChunkIds
    assert "LOG-01" in res_fail.remainingChunkIds


async def test_openai_dr_dci_selector_tracks_tokens_and_fails_closed():
    """OpenAiDrDciSelector가 토큰·지연을 기록하고 키 누락/예외 시 fail-closed STOP_INSUFFICIENT를 반환한다."""
    # 키 누락 -> fail-closed
    no_key_sel = OpenAiDrDciSelector(Settings(_env_file=None, openai_api_key=SecretStr("")))
    dec0 = await no_key_sel.select_next_action(
        incident_code="INC-1",
        keywords=["502"],
        remaining_index=[{"chunkId": "LOG-01", "corpusType": "LOG", "title": "t", "keywords": "502"}],
        collected_evidence=[],
        step_no=1,
        max_steps=3,
    )
    assert dec0.action == "STOP_INSUFFICIENT"

    # 정상 Structured Outputs 모킹
    def fake_parse(**kwargs: Any) -> Any:
        msg = SimpleNamespace(
            parsed=SimpleNamespace(action="PULL_CHUNK", chunk_id="LOG-01", reason="502 증상 확인"),
            content=None,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=210, completion_tokens=28),
        )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=fake_parse)))
    ok_sel = OpenAiDrDciSelector(Settings(_env_file=None, openai_api_key=SecretStr("sk-test")), openai_client=fake_client)
    dec1 = await ok_sel.select_next_action(
        incident_code="INC-1",
        keywords=["502"],
        remaining_index=[{"chunkId": "LOG-01", "corpusType": "LOG", "title": "t", "keywords": "502"}],
        collected_evidence=[EvidenceItem(chunkId="LOG-00", corpusType="LOG", title="init", summary="init")],
        step_no=1,
        max_steps=3,
    )
    assert dec1.action == "PULL_CHUNK"
    assert dec1.chunk_id == "LOG-01"
    assert dec1.input_tokens == 210
    assert dec1.output_tokens == 28


def test_phase6_corpus_has_60_cases_6_categories_and_clean_dev_eval_split():
    """60건 벤치마크 코퍼스가 6개 카테고리 × 10건, 고유 ID, dev/eval 생성 템플릿 완전 분리 조건을 만족한다."""
    version, cases = build_dr_dci_benchmark_dataset()
    assert version == "dr-dci-corpus-v1.0"
    assert len(cases) == 60
    assert len({c.case_id for c in cases}) == 60

    by_cat: dict[str, int] = {}
    dev_archetypes = set()
    eval_archetypes = set()
    dev_templates = set()
    eval_templates = set()
    for c in cases:
        by_cat[c.category] = by_cat.get(c.category, 0) + 1
        if c.split == "dev":
            dev_archetypes.add(c.archetype)
            dev_templates.add(c.template_id)
        else:
            eval_archetypes.add(c.archetype)
            eval_templates.add(c.template_id)
    assert len(by_cat) == 6
    assert all(cnt == 10 for cnt in by_cat.values())
    assert dev_archetypes.isdisjoint(eval_archetypes)
    assert dev_templates.isdisjoint(eval_templates)
    assert sum(1 for c in cases if c.prompt_injection) >= 6
    assert sum(1 for c in cases if c.multi_hop) >= 12
    assert sum(1 for c in cases if c.agentic_disadvantage) >= 2


async def test_adaptive_selector_ignores_negated_contradictions_and_adoption_gate_rejects_undecidable_judge():
    """부정 충돌 문구('external PG outage ruled out')에 조기 종료하지 않고, 판정기 전면 UNDECIDABLE 시 채택 게이트가 HOLD를 반환한다."""
    chunks = (
        CorpusChunkFixture("LOG-01", "LOG", "INC-NEG", "svc", "502 오류 초기 확인", "502,error", "502 detected; external PG outage ruled out. See LOG-02.", 1),
        CorpusChunkFixture("LOG-02", "LOG", "INC-NEG", "svc", "커넥션 누수 스택", "leak,HikariPool", "v42 배포 직후 ProxyLeakTask connection leak detected.", 2),
        CorpusChunkFixture("RB-01", "RUNBOOK", None, "svc", "롤백 런북", "502,rollback,kubectl", "1. 배포 직후 누수 확인 시 롤백 실행\nkubectl rollout undo deployment/svc", 1),
    )
    res = await run_agentic_dr_dci_investigation(
        InMemoryCorpusBackend(chunks),
        Settings(_env_file=None, dr_dci_max_pull_steps=4),
        LogInvestigationRequest(incidentCode="INC-NEG", keywords=["502"], maxPulls=4),
        selector=AdaptiveEvidenceSelector(),
    )
    # 부정 충돌 문구만으로 LOG-02를 놓치고 조기 종료해서는 안 됨
    assert "LOG-02" in res.evidenceChunkIds
    assert "RB-01" in res.evidenceChunkIds

    # 모든 케이스가 UNDECIDABLE인 판정기(예: API 키 누락 StructuredOutputJudge)로 벤치마크 실행 시 채택 게이트는 반드시 HOLD
    from common.decisions import DecisionResult

    class _AlwaysUndecidableJudge:
        async def judge_plan_evidence(self, **kwargs: Any) -> DecisionResult:
            return DecisionResult(verdict="UNDECIDABLE", confidence=None, probabilities={}, provider="test", model="none", reason="no key")

    _, all_cases = build_dr_dci_benchmark_dataset()
    payload = await run_four_way_benchmark(all_cases[:4], max_pull_steps=2, judge=_AlwaysUndecidableJudge())
    assert payload["adoption_gate"]["passed"] is False
    assert payload["adoption_gate"]["recommended_default"] == "keyword"
    assert payload["adoption_gate"]["checks"]["judge_decided_rate_ge_80pct"]["passed"] is False


def test_four_way_benchmark_and_phase6_charts_cli(tmp_path: Path):
    """4종 전략 벤치마크가 채택 게이트·반례·CSV/JSON/MD 및 4종 PNG 차트를 정상 생성한다."""
    out_json = tmp_path / "phase6.json"
    out_csv = tmp_path / "phase6.csv"
    out_md = tmp_path / "phase6.md"

    code = benchmark_cli_main([
        "--max-pull-steps", "3",
        "--output-json", str(out_json),
        "--output-csv", str(out_csv),
        "--output-md", str(out_md),
    ])
    assert code == 0
    assert out_csv.is_file()
    assert "4종 검색 전략 종합 비교" in out_md.read_text(encoding="utf-8")

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    by_strat = {s["strategy"]: s for s in payload["summaries"]}
    assert payload["total_cases"] == 60
    assert by_strat["agentic_dr_dci"]["multi_hop_evidence_recall"] > by_strat["keyword_dr_dci"]["multi_hop_evidence_recall"]
    assert by_strat["agentic_dr_dci"]["multi_hop_evidence_recall"] > by_strat["vector_rag"]["multi_hop_evidence_recall"]
    assert by_strat["agentic_dr_dci"]["false_supported_rate"] == 0.0
    assert by_strat["agentic_dr_dci"]["token_reduction_vs_full"] >= 0.30
    assert payload["adoption_gate"]["passed"] is True
    assert len(payload["agentic_disadvantage_cases"]) >= 2

    charts = generate_four_way_charts(out_json, tmp_path / "charts", prefix="p6")
    assert len(charts) == 4
    for chart in charts:
        assert chart.is_file()
        assert chart.stat().st_size > 5_000
