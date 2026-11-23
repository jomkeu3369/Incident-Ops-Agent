from __future__ import annotations

from types import SimpleNamespace
import httpx
from pydantic import SecretStr

from common.config import Settings
from common.decisions import DecisionsClient
from common.planner_math import find_work_window, select_rollback_candidate
from common.tools import ExternalToolsClient, get_external_tool_specs


def _settings(**overrides) -> Settings:
    """Phase 2A 외부 API·판정 테스트용 설정을 생성한다."""
    base = {
        "github_token": SecretStr("ghp_test"),
        "github_repo": "acme/payment-service",
        "github_environment": "production",
        "grafana_prometheus_url": "https://prometheus.example.com",
        "grafana_instance_id": "12345",
        "grafana_metrics_token": SecretStr("glc_test"),
        "google_calendar_id": "freeze@group.calendar.google.com",
        "google_calendar_token": SecretStr("ya29_test"),
        "openai_api_key": SecretStr("sk-test"),
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_tool_specs_registry():
    """외부 API Tool 3종 명세가 레지스트리에 등록되어 있는지 검증한다."""
    specs = get_external_tool_specs()
    assert [s["name"] for s in specs] == ["list_rollback_candidates", "get_release_metrics", "get_change_freeze_windows"]
    assert all(s["kind"] == "external_api" and s["description"] for s in specs)


async def test_github_list_rollback_candidates_dedup_and_status_time():
    """GitHub Deployments가 success 상태 시각 기준으로 정렬하고 SHA 중복을 제거하는지 검증한다."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/deployments"):
            return httpx.Response(200, json=[
                {"id": 43, "sha": "sha-v43", "ref": "v43"},  # 장애 이후 배포
                {"id": 42, "sha": "sha-v42", "ref": "v42"},  # 기준 배포
                {"id": 412, "sha": "sha-v41", "ref": "v41"},  # v41 최신 성공
                {"id": 411, "sha": "sha-v41", "ref": "v41"},  # v41 이전 중복 성공
                {"id": 40, "sha": "sha-v40", "ref": "v40"},  # 실패 배포(제외 대상)
            ])
        dep_id = req.url.path.split("/")[-2]
        statuses = {
            "43": [{"state": "success", "created_at": "2026-11-18T14:10:00+09:00"}],
            "42": [{"state": "in_progress", "created_at": "2026-11-18T13:59:00+09:00"}, {"state": "success", "created_at": "2026-11-18T14:00:00+09:00"}],
            "412": [{"state": "success", "created_at": "2026-11-18T12:00:00+09:00"}],
            "411": [{"state": "success", "created_at": "2026-11-18T11:00:00+09:00"}],
            "40": [{"state": "failure", "created_at": "2026-11-18T10:00:00+09:00"}],
        }
        return httpx.Response(200, json=statuses[dep_id])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        res = await ExternalToolsClient(_settings(), http).list_rollback_candidates("payment-service", "2026-11-18T14:04:00+09:00")

    assert res["ok"] is True
    assert res["data"]["referenceDeployment"]["gitSha"] == "sha-v42"
    assert res["data"]["referenceDeployment"]["succeededAt"] == "2026-11-18T14:00:00+09:00"
    assert [c["gitSha"] for c in res["data"]["candidates"]] == ["sha-v41"]
    assert res["data"]["candidates"][0]["deploymentId"] == 412


async def test_github_error_codes_no_result_auth_rate_and_invalid():
    """GitHub 조회의 NO_RESULT·INVALID_INPUT·RATE_LIMITED 실패 코드 반환을 검증한다."""

    def no_candidate(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/deployments"):
            return httpx.Response(200, json=[{"id": 42, "sha": "sha-v42", "ref": "v42"}])
        return httpx.Response(200, json=[{"state": "success", "created_at": "2026-11-18T14:00:00+09:00"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_candidate)) as http:
        tools = ExternalToolsClient(_settings(), http)
        assert (await tools.list_rollback_candidates("payment-service", "2026-11-18T14:04:00+09:00"))["error"]["code"] == "NO_RESULT"
        assert (await tools.list_rollback_candidates("unknown-svc", "2026-11-18T14:04:00+09:00"))["error"]["code"] == "INVALID_INPUT"
        assert (await tools.list_rollback_candidates("payment-service", "2026-11-18T14:04:00"))["error"]["code"] == "INVALID_INPUT"

    rate_transport = httpx.MockTransport(lambda req: httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}))
    async with httpx.AsyncClient(transport=rate_transport) as http:
        assert (await ExternalToolsClient(_settings(), http).list_rollback_candidates("payment-service", "2026-11-18T14:04:00+09:00"))["error"]["code"] == "RATE_LIMITED"


async def test_grafana_metrics_calculation_zero_vs_insufficient_and_injection():
    """Grafana 지표의 502 비율 계산, 0%와 INSUFFICIENT_DATA 구분, PromQL 라벨 검증을 확인한다."""

    def handler(req: httpx.Request) -> httpx.Response:
        query = req.url.params["query"]
        if 'git_sha="sha-v41"' in query:
            val = "3" if 'status="502"' in query else "1000"
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, val]}]}})
        if 'git_sha="sha-zero"' in query:
            val = "0" if 'status="502"' in query else "500"
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, val]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        tools = ExternalToolsClient(_settings(), http)
        v41 = await tools.get_release_metrics("payment-service", "sha-v41", "2026-11-18T14:05:00+09:00", "2026-11-18T14:10:00+09:00")
        zero = await tools.get_release_metrics("payment-service", "sha-zero", "2026-11-18T14:05:00+09:00", "2026-11-18T14:10:00+09:00")
        missing = await tools.get_release_metrics("payment-service", "sha-missing", "2026-11-18T14:05:00+09:00", "2026-11-18T14:10:00+09:00")
        bad_sha = await tools.get_release_metrics("payment-service", 'sha"}+or+1==1', "2026-11-18T14:05:00+09:00", "2026-11-18T14:10:00+09:00")

    assert v41["ok"] is True and v41["data"]["http502RatePercent"] == 0.3
    assert zero["ok"] is True and zero["data"]["http502RatePercent"] == 0.0
    assert missing["ok"] is False and missing["error"]["code"] == "INSUFFICIENT_DATA"
    assert bad_sha["ok"] is False and bad_sha["error"]["code"] == "INVALID_INPUT"


async def test_google_calendar_freebusy_and_calendar_errors():
    """Google Calendar FreeBusy 정상 조회와 캘린더 errors[] 실패 처리를 검증한다."""
    cal_id = "freeze@group.calendar.google.com"

    def ok_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"calendars": {cal_id: {"busy": [{"start": "2026-11-18T14:30:00+09:00", "end": "2026-11-18T14:40:00+09:00"}]}}})

    def err_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"calendars": {cal_id: {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as http:
        res = await ExternalToolsClient(_settings(), http).get_change_freeze_windows("payment-service", "2026-11-18T14:28:00+09:00", "2026-11-18T14:50:00+09:00")
    assert res["ok"] is True and len(res["data"]["freezeWindows"]) == 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(err_handler)) as http:
        err = await ExternalToolsClient(_settings(), http).get_change_freeze_windows("payment-service", "2026-11-18T14:28:00+09:00", "2026-11-18T14:50:00+09:00")
    assert err["ok"] is False and err["error"]["code"] == "AUTH_FAILED"


def test_payment_scenario_replan_and_half_open_boundaries():
    """결제 장애 시나리오(v41 선택, 14:28~14:33 -> 14:40~14:45 재계획)와 반개구간 경계를 검증한다."""
    eval_res = select_rollback_candidate([{"gitSha": "sha-v42", "http502RatePercent": 18.0}, {"gitSha": "sha-v41", "http502RatePercent": 0.3}], max_502_rate_percent=1.0)
    assert eval_res.selected["gitSha"] == "sha-v41"
    assert len(eval_res.rejected) == 1 and eval_res.rejected[0]["gitSha"] == "sha-v42"

    # 결제 시나리오: 허용 14:28~14:50, 5분 작업, 금지 14:30~14:40 -> 14:40~14:45로 재계획
    decision = find_work_window("2026-11-18T14:28:00+09:00", "2026-11-18T14:50:00+09:00", 5, [{"start": "2026-11-18T14:30:00+09:00", "end": "2026-11-18T14:40:00+09:00"}])
    assert decision.replanned is True
    assert decision.previous_window.to_dict() == {"start": "2026-11-18T14:28:00+09:00", "end": "2026-11-18T14:33:00+09:00"}
    assert decision.selected_window.to_dict() == {"start": "2026-11-18T14:40:00+09:00", "end": "2026-11-18T14:45:00+09:00"}
    assert len(decision.remaining_gaps) == 2

    # 반개구간 경계: 14:33에 시작하는 금지 구간은 14:28~14:33 초안과 겹치지 않는다
    no_overlap = find_work_window("2026-11-18T14:28:00+09:00", "2026-11-18T14:50:00+09:00", 5, [{"start": "2026-11-18T14:33:00+09:00", "end": "2026-11-18T14:40:00+09:00"}])
    assert no_overlap.replanned is False
    assert no_overlap.selected_window.to_dict()["end"] == "2026-11-18T14:33:00+09:00"

    # 가용 시간 부족 시 None과 남은 시간대를 함께 반환한다
    blocked = find_work_window("2026-11-18T14:28:00+09:00", "2026-11-18T14:42:00+09:00", 5, [{"start": "2026-11-18T14:30:00+09:00", "end": "2026-11-18T14:40:00+09:00"}])
    assert blocked.selected_window is None
    assert len(blocked.remaining_gaps) == 2


async def test_decisions_client_all_verdicts_and_fail_safe():
    """Decisions API 3개 판정값 매핑과 키 누락 시 UNDECIDABLE 안전 폴백을 검증한다."""

    def make_fake_openai(choice: str):
        ans = SimpleNamespace(type="choice", choice=choice, confidence=0.98, probabilities=[SimpleNamespace(value=choice, probability=0.98)])
        resp = SimpleNamespace(answers=[ans], usage=SimpleNamespace(input_tokens=120))
        return SimpleNamespace(decisions=SimpleNamespace(create=lambda **kw: resp))

    for expected in ("SUPPORTED", "NEED_MORE_EVIDENCE", "CONTRADICTED"):
        client = DecisionsClient(_settings(), openai_client=make_fake_openai(expected))
        verdict = await client.judge_plan_evidence(proposed_action="rollback payment-service to v41", runbook="rollback when 502 spikes after deploy and previous version is healthy", evidence="synthetic case")
        assert verdict.verdict == expected and verdict.confidence == 0.98 and verdict.model == "gpt-6-luna"

    # API 키 누락 시 예외를 던지지 않고 UNDECIDABLE을 반환해야 한다
    res = await DecisionsClient(_settings(openai_api_key=SecretStr(""))).judge_plan_evidence(proposed_action="x", runbook="y", evidence="z")
    assert res.verdict == "UNDECIDABLE"


async def test_github_list_rollback_candidates_parallel_status_and_error_propagation():
    """다수 배포 상태 조회가 병렬 처리되고 중간 실패 시 실패 코드가 결정론적으로 반환되는지 검증한다."""
    import asyncio

    active = 0
    peak_active = 0

    async def async_handler(req: httpx.Request) -> httpx.Response:
        nonlocal active, peak_active
        if req.url.path.endswith("/deployments"):
            return httpx.Response(200, json=[
                {"id": 100 + i, "sha": f"sha-v{10 - i}", "ref": f"v{10 - i}"}
                for i in range(8)
            ])
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        dep_idx = int(req.url.path.split("/")[-2]) - 100
        return httpx.Response(200, json=[{"state": "success", "created_at": f"2026-11-18T13:0{7 - dep_idx}:00+09:00"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(async_handler)) as http:
        res = await ExternalToolsClient(_settings(), http).list_rollback_candidates("payment-service", "2026-11-18T14:00:00+09:00")

    assert res["ok"] is True
    assert peak_active >= 2
    assert res["data"]["referenceDeployment"]["gitSha"] == "sha-v10"
    assert len(res["data"]["candidates"]) == 7

    def fail_mid_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/deployments"):
            return httpx.Response(200, json=[
                {"id": 101, "sha": "sha-v2", "ref": "v2"},
                {"id": 102, "sha": "sha-v1", "ref": "v1"},
            ])
        if "/101/" in req.url.path:
            return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_mid_handler)) as http:
        err_res = await ExternalToolsClient(_settings(), http).list_rollback_candidates("payment-service", "2026-11-18T14:00:00+09:00")
    assert err_res["ok"] is False and err_res["error"]["code"] == "RATE_LIMITED"


async def test_grafana_metrics_rejects_nan_inf_and_negative_values():
    """Prometheus 응답이 NaN, Inf 또는 음수일 때 INSUFFICIENT_DATA를 반환하는지 검증한다."""

    def make_handler(err_val: str, total_val: str = "1000"):
        def handler(req: httpx.Request) -> httpx.Response:
            q = req.url.params["query"]
            val = err_val if 'status="502"' in q else total_val
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, val]}]}})

        return handler

    for bad_val in ("NaN", "Inf", "-Inf", "-1"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(make_handler(bad_val))) as http:
            res = await ExternalToolsClient(_settings(), http).get_release_metrics(
                "payment-service", "sha-v41", "2026-11-18T14:05:00+09:00", "2026-11-18T14:10:00+09:00"
            )
        assert res["ok"] is False and res["error"]["code"] == "INSUFFICIENT_DATA"

    async with httpx.AsyncClient(transport=httpx.MockTransport(make_handler("0", "NaN"))) as http:
        res_total_nan = await ExternalToolsClient(_settings(), http).get_release_metrics(
            "payment-service", "sha-v41", "2026-11-18T14:05:00+09:00", "2026-11-18T14:10:00+09:00"
        )
    assert res_total_nan["ok"] is False and res_total_nan["error"]["code"] == "INSUFFICIENT_DATA"
