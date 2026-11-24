from __future__ import annotations

from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient

from common.anomaly import SessionAnomalyAnalyzer, evaluate_session_rules
from common.calendar_adapter import LocalFreezeCalendarAdapter
from common.config import Settings
from common.tools import ExternalToolsClient
from common.worker_executor import PlannedToolStep, SplitWorkerExecutor
from supervisor.main import create_supervisor_app
from supervisor.routers import get_anomaly_analyzer


# --- 로깅 이상 징후 판단 테스트 ---


async def test_anomaly_normal_session_uses_zero_llm_calls() -> None:
    """정상 실행 로그는 NORMAL로 판정하고 LLM을 0회 호출해야 한다."""
    called = 0

    async def fake_llm(sid: str, findings: list[dict], events: list[dict]) -> str:
        nonlocal called
        called += 1
        return "진단 결과"

    events = [
        {"eventType": "step_finished", "payload": {"step": "plan", "steps": ["a2a:log-analyzer-agent", "a2a:remediation-agent"]}},
        {"eventType": "tool_call", "payload": {"tool": "a2a:log-analyzer-agent", "args": {"incidentCode": "INC-1"}, "ok": True}},
        {"eventType": "tool_call", "payload": {"tool": "list_rollback_candidates", "args": {"serviceName": "payment-service"}, "ok": True}},
        {"eventType": "tool_call", "payload": {"tool": "a2a:remediation-agent", "args": {"incidentCode": "INC-1"}, "ok": True}},
    ]
    analyzer = SessionAnomalyAnalyzer(diagnosis_fn=fake_llm)
    report = await analyzer.analyze("sess-normal", events)
    assert report.status == "NORMAL"
    assert report.findings == []
    assert report.diagnosis is None
    assert report.llm_calls == 0
    assert called == 0


async def test_anomaly_missing_evidence_returns_undetermined_with_zero_llm() -> None:
    """빈 로그나 판정 근거 없는 이벤트만 있으면 UNDETERMINED(판정 불가)와 LLM 0회를 반환한다."""
    analyzer = SessionAnomalyAnalyzer()
    empty_report = await analyzer.analyze("sess-empty", [])
    assert empty_report.status == "UNDETERMINED"
    assert empty_report.llm_calls == 0

    irrelevant_report = await analyzer.analyze("sess-meta", [{"eventType": "approval_decided", "payload": {"decision": "APPROVED"}}])
    assert irrelevant_report.status == "UNDETERMINED"
    assert irrelevant_report.llm_calls == 0


async def test_anomaly_detects_repeated_call_excessive_replan_and_unplanned_tool_with_3_sentence_diagnosis() -> None:
    """동일 입력 반복, 과다 재계획, 미계획 Tool 호출을 탐지하고 이상 건에만 3문장 진단을 생성한다."""
    called = 0

    async def fake_llm(sid: str, findings: list[dict], events: list[dict]) -> str:
        nonlocal called
        called += 1
        return "첫 번째 원인 문장입니다. 두 번째 영향 문장입니다. 세 번째 조치 문장입니다. 네 번째 초과 문장입니다."

    events = [
        {"eventType": "step_finished", "payload": {"step": "plan", "steps": ["a2a:log-analyzer-agent"]}},
        {"eventType": "tool_call", "payload": {"tool": "a2a:log-analyzer-agent", "args": {"incidentCode": "INC-1"}, "ok": True}},
        {"eventType": "tool_call", "payload": {"tool": "a2a:log-analyzer-agent", "args": {"incidentCode": "INC-1"}, "ok": True}},
        {"eventType": "tool_call", "payload": {"tool": "unknown_shell_exec", "args": {"cmd": "ls"}, "ok": False}},
        {"eventType": "replan", "payload": {"reason": "충돌 1"}},
        {"eventType": "replan", "payload": {"reason": "충돌 2"}},
        {"eventType": "replan", "payload": {"reason": "충돌 3"}},
    ]
    status, findings = evaluate_session_rules("sess-bad", events)
    assert status == "ANOMALY"
    assert {f.rule for f in findings} == {"REPEATED_IDENTICAL_CALL", "EXCESSIVE_REPLAN", "UNPLANNED_TOOL_CALL"}

    analyzer = SessionAnomalyAnalyzer(diagnosis_fn=fake_llm)
    report = await analyzer.analyze("sess-bad", events)
    assert report.status == "ANOMALY"
    assert report.llm_calls == 1
    assert called == 1
    assert report.diagnosis is not None
    assert "네 번째" not in report.diagnosis


def test_anomaly_supervisor_session_endpoint() -> None:
    """Supervisor GET /api/ag-ui/sessions/{session_id}/anomalies 엔드포인트가 정상 동작한다."""
    settings = Settings(internal_service_token="test-token")
    app = create_supervisor_app(settings)

    async def fake_diag(sid: str, findings: list[dict], events: list[dict]) -> str:
        return "반복 호출이 감지되었습니다. 재계획 조건을 확인하세요. 캐시 정책을 점검하세요."

    app.dependency_overrides[get_anomaly_analyzer] = lambda: SessionAnomalyAnalyzer(diagnosis_fn=fake_diag)

    with TestClient(app) as client:
        # 존재하지 않는 세션 -> 404
        assert client.get("/api/ag-ui/sessions/missing/anomalies").status_code == 404

        # 실행 로그 저장 후 이상 분석 조회
        store = app.state.container.supervisor_store
        assert store is not None
        import asyncio

        asyncio.run(store.append_session_log("sess-api", {"eventType": "tool_call", "payload": {"tool": "list_rollback_candidates", "args": {"serviceName": "payment-service"}}}))
        asyncio.run(store.append_session_log("sess-api", {"eventType": "tool_call", "payload": {"tool": "list_rollback_candidates", "args": {"serviceName": "payment-service"}}}))

        res = client.get("/api/ag-ui/sessions/sess-api/anomalies")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ANOMALY"
        assert body["llmCalls"] == 1
        assert body["findings"][0]["rule"] == "REPEATED_IDENTICAL_CALL"


# --- Planner·Worker LLM 분리 비교 테스트 ---


async def test_split_worker_uses_zero_llm_on_happy_path_and_reduces_calls() -> None:
    """정상 경로에서 Worker LLM 호출은 0회이며 통합형 대비 LLM 호출이 크게 감소한다."""
    async def runner(tool: str, args: dict) -> dict:
        return {"ok": True, "data": {"tool": tool, "args": args}}

    async def repair(tool: str, fixed: dict, cur: dict, err: dict) -> dict:
        raise AssertionError("정상 경로에서는 Worker LLM 보정이 호출되면 안 됩니다.")

    executor = SplitWorkerExecutor(runner, repair_fn=repair, max_retries=1)
    steps = [
        PlannedToolStep("list_rollback_candidates", fixed_args={"serviceName": "payment-service"}, mutable_args={"environment": "production"}),
        PlannedToolStep("get_release_metrics", fixed_args={"serviceName": "payment-service", "gitSha": "sha-v41"}, mutable_args={"from": "14:05", "to": "14:10"}),
        PlannedToolStep("get_change_freeze_windows", fixed_args={"serviceName": "payment-service"}, mutable_args={"windowStart": "14:28", "windowEnd": "14:50"}),
    ]
    outcomes, metrics = await executor.execute_plan_and_compare(steps, planner_llm_calls=1)
    assert all(o.ok for o in outcomes)
    assert metrics.split_worker_llm_calls == 0
    assert metrics.split_total_llm_calls == 1
    assert metrics.monolithic_llm_calls == 4
    assert metrics.llm_call_reduction_percent == 75.0
    assert metrics.fixed_args_preserved_all is True


async def test_split_worker_retryable_failure_preserves_fixed_args_and_non_retryable_skips_llm() -> None:
    """NO_RESULT 실패 시에만 Worker LLM을 호출하고, LLM이 고정 인자 변조를 시도해도 강제 보존한다."""
    seen_args: list[dict] = []

    async def runner(tool: str, args: dict) -> dict:
        seen_args.append(dict(args))
        if tool == "list_rollback_candidates" and len(seen_args) == 1:
            return {"ok": False, "error": {"code": "NO_RESULT", "message": "배포 이력 없음"}}
        if tool == "get_release_metrics":
            return {"ok": False, "error": {"code": "AUTH_FAILED", "message": "토큰 만료"}}
        return {"ok": True, "data": {"recovered": True}}

    repair_calls = 0

    async def malicious_repair(tool: str, fixed: dict, cur: dict, err: dict) -> dict:
        nonlocal repair_calls
        repair_calls += 1
        # LLM이 고정 인자(serviceName)를 변조하려고 시도하는 상황을 시뮬레이션한다
        return {"serviceName": "hacked-service", "environment": "prod-kr"}

    executor = SplitWorkerExecutor(runner, repair_fn=malicious_repair, max_retries=1)

    # 1) 수정 가능 실패(NO_RESULT): 재시도 성공 + 고정 인자 보존
    step1 = PlannedToolStep("list_rollback_candidates", fixed_args={"serviceName": "payment-service"}, mutable_args={"environment": "production"})
    out1 = await executor.execute_step(step1)
    assert out1.ok is True
    assert out1.attempts == 2
    assert out1.worker_llm_calls == 1
    assert out1.fixed_args_preserved is True
    assert out1.used_args["serviceName"] == "payment-service"
    assert out1.used_args["environment"] == "prod-kr"

    # 2) 비수정 실패(AUTH_FAILED): Worker LLM 호출 0회
    step2 = PlannedToolStep("get_release_metrics", fixed_args={"serviceName": "payment-service"}, mutable_args={"gitSha": "v41"})
    out2 = await executor.execute_step(step2)
    assert out2.ok is False
    assert out2.attempts == 1
    assert out2.worker_llm_calls == 0
    assert repair_calls == 1


# --- 로컬 저장소 ↔ Google Calendar 교체형 어댑터 테스트 ---


async def test_calendar_local_adapter_swaps_with_google_calendar(tmp_path: Path) -> None:
    """ExternalToolsClient가 코드 변경 없이 LocalFreezeCalendarAdapter로 교체되어 동일 계약을 반환한다."""
    cal_file = tmp_path / "freeze-windows.json"
    local_adapter = LocalFreezeCalendarAdapter(storage_path=cal_file)
    local_adapter.add_freeze_window("payment-service", "2026-11-18T14:30:00+09:00", "2026-11-18T14:40:00+09:00", reason="정산 배치")
    local_adapter.add_freeze_window("payment-service", "2026-11-18T16:00:00+09:00", "2026-11-18T16:30:00+09:00", reason="외부 범위")

    # 파일 영속화 후 settings(calendar_adapter_mode="local")만으로 자동 어댑터 교체 확인
    settings = Settings(calendar_adapter_mode="local", local_freeze_calendar_path=str(cal_file))
    async with httpx.AsyncClient() as http_client:
        tools = ExternalToolsClient(settings, http_client)
        res = await tools.get_change_freeze_windows(
            "payment-service",
            "2026-11-18T14:28:00+09:00",
            "2026-11-18T14:50:00+09:00",
        )

    assert res["ok"] is True
    assert res["data"]["adapter"] == "local"
    assert len(res["data"]["freezeWindows"]) == 1
    assert res["data"]["freezeWindows"][0]["start"] == "2026-11-18T14:30:00+09:00"
    assert res["data"]["freezeWindows"][0]["end"] == "2026-11-18T14:40:00+09:00"


async def test_calendar_local_adapter_reloads_when_file_updates(tmp_path: Path) -> None:
    """애플리케이션 기동 이후 로컬 캘린더 파일이 갱신되면 새 변경 금지 구간을 즉시 다시 읽어야 한다(P1 회귀)."""
    cal_file = tmp_path / "freeze-live.json"
    writer = LocalFreezeCalendarAdapter(storage_path=cal_file)
    writer.add_freeze_window("payment-service", "2026-11-18T14:30:00+09:00", "2026-11-18T14:35:00+09:00")

    reader = LocalFreezeCalendarAdapter(storage_path=cal_file)
    first = await reader.get_freeze_windows("payment-service", "2026-11-18T14:28:00+09:00", "2026-11-18T14:50:00+09:00")
    assert len(first["data"]["freezeWindows"]) == 1

    # 다른 인스턴스가 파일에 새 변경 금지 구간을 추가하고 mtime을 갱신한다
    import os

    writer.add_freeze_window("payment-service", "2026-11-18T14:40:00+09:00", "2026-11-18T14:45:00+09:00")
    st = cal_file.stat()
    os.utime(cal_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    second = await reader.get_freeze_windows("payment-service", "2026-11-18T14:28:00+09:00", "2026-11-18T14:50:00+09:00")
    assert len(second["data"]["freezeWindows"]) == 2


async def test_anomaly_fallback_diagnosis_reports_zero_llm_calls_and_legit_replan_not_flagged() -> None:
    """폴백 진단은 llmCalls=0을 보고하고, 근거 청크가 늘어난 정상 재계획은 동일 입력 반복으로 오탐하지 않는다(P2 회귀)."""
    replan_events = [
        {"eventType": "step_finished", "payload": {"step": "plan", "steps": ["a2a:log-analyzer-agent", "a2a:remediation-agent"]}},
        {"eventType": "tool_call", "payload": {"tool": "a2a:remediation-agent", "args": {"incidentCode": "INC-1", "evidenceChunkIds": ["LOG-104"]}}},
        {"eventType": "llm_decision", "payload": {"step": "evaluate", "decision": "replan"}},
        {"eventType": "tool_call", "payload": {"tool": "a2a:remediation-agent", "args": {"incidentCode": "INC-1", "evidenceChunkIds": ["LOG-104", "RUNBOOK-01"]}}},
    ]
    normal_report = await SessionAnomalyAnalyzer().analyze("sess-replan-ok", replan_events)
    assert normal_report.status == "NORMAL"
    assert normal_report.llm_calls == 0

    dup_events = [
        {"eventType": "tool_call", "payload": {"tool": "list_rollback_candidates", "args": {"serviceName": "payment-service"}}},
        {"eventType": "tool_call", "payload": {"tool": "list_rollback_candidates", "args": {"serviceName": "payment-service"}}},
    ]
    # OpenAI 키도 없고 diagnosis_fn도 없는 폴백 경로 -> llm_calls == 0
    fallback_report = await SessionAnomalyAnalyzer(settings=Settings(openai_api_key="")).analyze("sess-fallback", dup_events)
    assert fallback_report.status == "ANOMALY"
    assert fallback_report.diagnosis is not None
    assert fallback_report.llm_calls == 0


async def test_split_worker_multi_retry_accumulates_mutable_repairs_while_preserving_fixed_args() -> None:
    """max_retries > 1에서 단계별 부분 보정(environment -> region)이 유실되지 않고 누적되어야 한다(P2 회귀)."""
    async def runner(tool: str, args: dict) -> dict:
        if args.get("environment") != "prod-kr":
            return {"ok": False, "error": {"code": "NO_RESULT", "message": "wrong env"}}
        if args.get("region") != "ap-northeast-2":
            return {"ok": False, "error": {"code": "INVALID_INPUT", "message": "wrong region"}}
        return {"ok": True, "data": args}

    async def incremental_repair(tool: str, fixed: dict, cur: dict, err: dict) -> dict:
        if err["code"] == "NO_RESULT":
            return {"environment": "prod-kr"}
        return {"region": "ap-northeast-2", "serviceName": "should-be-ignored"}

    executor = SplitWorkerExecutor(runner, repair_fn=incremental_repair, max_retries=2)
    step = PlannedToolStep(
        "list_rollback_candidates",
        fixed_args={"serviceName": "payment-service"},
        mutable_args={"environment": "production", "region": "us-east-1"},
    )
    out = await executor.execute_step(step)
    assert out.ok is True
    assert out.attempts == 3
    assert out.worker_llm_calls == 2
    assert out.fixed_args_preserved is True
    assert out.used_args == {"serviceName": "payment-service", "environment": "prod-kr", "region": "ap-northeast-2"}
