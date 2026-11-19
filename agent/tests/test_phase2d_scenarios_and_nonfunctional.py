from __future__ import annotations

import json
from typing import Any
import httpx
import pytest
from fastapi.testclient import TestClient

from common.a2a_utils import send_a2a_json
from common.backend_client import BackendApiError, BackendClient
from common.config import Settings
from common.decisions import DecisionResult
from common.tools import ExternalToolsClient
from log_agent.main import create_log_agent_app
from remediation_agent.main import create_remediation_agent_app
from supervisor.main import create_supervisor_app

SECRET_TOKEN = "phase2d-secret-token-777"
SECRET_API_KEY = "sk-live-secret-never-leak-999"
SECRET_CAL_KEY = "cal-secret-bearer-888"


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """SSE 응답 본문에서 AG-UI JSON 이벤트 목록을 추출한다."""
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


class _ScenarioState:
    """Phase 2D 대표 시나리오 5종과 HITL 409 검증용 백엔드·외부 Tool 상태 저장소."""

    def __init__(self, *, freeze_windows: list[dict[str, str]] | None = None, calendar_status: int = 200, verdicts: list[str] | None = None, extra_chunks: int = 0) -> None:
        self.freeze_windows = freeze_windows if freeze_windows is not None else []
        self.calendar_status = calendar_status
        self.verdicts = list(verdicts or ["SUPPORTED"])
        self.tool_hits = {"github": 0, "grafana": 0, "calendar": 0}
        self.mcp_hits: list[str] = []
        self.approvals: dict[str, dict[str, Any]] = {}
        self.completed: list[dict[str, Any]] = []
        self.incident_status = "OPEN"
        self.postmortem: dict[str, Any] | None = None
        self.chunks: dict[str, dict[str, Any]] = {
            "LOG-104": {"chunkId": "LOG-104", "corpusType": "LOG", "title": "HikariPool timeout", "content": "14:04 HikariPool-1 connection timeout after v42 deploy."},
            "LOG-105": {"chunkId": "LOG-105", "corpusType": "LOG", "title": "502 spike", "content": "14:04 payment-service HTTP 502 spike detected."},
            "RUNBOOK-01": {"chunkId": "RUNBOOK-01", "corpusType": "RUNBOOK", "title": "Rollback runbook", "content": "Run `kubectl rollout undo deployment/payment-service` when 502 spikes after deploy."},
        }
        for i in range(extra_chunks):
            cid = f"LOG-20{i}"
            self.chunks[cid] = {"chunkId": cid, "corpusType": "LOG", "title": f"Extra HikariPool log {i}", "content": f"Additional HikariPool log chunk {i}."}

    def build_ext_http(self) -> httpx.AsyncClient:
        """GitHub·Grafana·Calendar 외부 API 스텁 HTTP 클라이언트를 반환한다."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            path = request.url.path
            if "api.github.com" in url and path.endswith("/deployments"):
                self.tool_hits["github"] += 1
                return httpx.Response(200, json=[
                    {"id": 42, "sha": "sha-v42", "ref": "v42", "environment": "production", "created_at": "2026-11-18T14:00:00+09:00"},
                    {"id": 41, "sha": "sha-v41", "ref": "v41", "environment": "production", "created_at": "2026-11-18T13:10:00+09:00"},
                ])
            if "api.github.com" in url and path.endswith("/statuses"):
                return httpx.Response(200, json=[{"state": "success", "created_at": "2026-11-18T14:00:05+09:00" if "/42/" in path else "2026-11-18T13:10:05+09:00"}])
            if "grafana.test" in url:
                self.tool_hits["grafana"] += 1
                q = request.url.params.get("query", "")
                is_502 = 'status="502"' in q
                val = ("12" if is_502 else "200") if "sha-v42" in q else ("3" if is_502 else "1000")
                return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1700000000, val]}]}})
            if "calendar.test" in url:
                self.tool_hits["calendar"] += 1
                if self.calendar_status != 200:
                    # 상위 오류 본문에 민감 정보와 스택트레이스가 섞여 있어도 외부로 노출되면 안 된다
                    return httpx.Response(self.calendar_status, json={"error": {"message": f"upstream failure bearer={SECRET_CAL_KEY} Traceback line 99"}})
                return httpx.Response(200, json={"calendars": {"freeze@group.calendar.google.com": {"busy": self.freeze_windows}}})
            return httpx.Response(404)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def build_backend_client(self, settings: Settings) -> BackendClient:
        """Spring REST + MCP 계약을 검증하는 백엔드 클라이언트를 반환한다."""

        async def mcp_caller(tool_name: str, args: dict[str, Any]) -> Any:
            self.mcp_hits.append(tool_name)
            if tool_name == "searchCorpusIndex":
                ctype = args["corpusType"]
                return [{"chunkId": c["chunkId"], "corpusType": c["corpusType"], "title": c["title"], "summary": c["content"][:80]} for c in self.chunks.values() if c["corpusType"] == ctype]
            if tool_name == "pullCorpusChunk":
                return self.chunks[args["chunkId"]]
            if tool_name == "completeIncidentWithPostMortem":
                apr = self.approvals.get(args["approvalId"])
                if not apr or apr.get("decision") != "APPROVED":
                    raise BackendApiError(409, "승인되지 않은 approvalId로 완료를 반영할 수 없습니다.")
                self.incident_status = "RESOLVED"
                self.postmortem = {"rootCause": args.get("rootCause"), "timelineMarkdown": args.get("timelineMarkdown"), "preventionItems": args.get("preventionItems")}
                self.completed.append(args)
                return {"incidentCode": args["incidentCode"], "status": "RESOLVED", "postMortem": self.postmortem}
            return {}

        def rest_handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path.endswith("/INC-2026-001"):
                return httpx.Response(200, json={"incident": {"incidentCode": "INC-2026-001", "serviceName": "payment-service", "status": self.incident_status}})
            if request.method == "POST" and path.endswith("/plans"):
                return httpx.Response(201, json=json.loads(request.content))
            if request.method == "POST" and path.endswith("/approvals"):
                body = json.loads(request.content)
                body["decision"] = "PENDING"
                self.approvals[body["approvalId"]] = body
                return httpx.Response(201, json=body)
            if request.method == "PATCH" and "/approvals/" in path:
                aid = path.rsplit("/", 1)[-1]
                apr = self.approvals.get(aid)
                if not apr:
                    return httpx.Response(404, json={"message": "승인 요청을 찾을 수 없습니다."})
                if apr["decision"] != "PENDING":
                    return httpx.Response(409, json={"message": "이미 결정이 완료된 승인 요청입니다."})
                body = json.loads(request.content)
                apr["decision"] = body["decision"]
                apr["approvedBy"] = body["approvedBy"]
                return httpx.Response(200, json=apr)
            return httpx.Response(404)

        return BackendClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(rest_handler)), mcp_caller=mcp_caller)


class _ScriptedDecisions:
    """시나리오별 Decisions 판정값을 순서대로 반환하는 어댑터."""

    def __init__(self, verdicts: list[str]) -> None:
        self._verdicts = list(verdicts)

    async def judge_plan_evidence(self, **kwargs: Any) -> DecisionResult:
        v = self._verdicts.pop(0) if len(self._verdicts) > 1 else self._verdicts[0]
        return DecisionResult(verdict=v, confidence=0.93, probabilities={v: 0.93}, provider="openai", model="gpt-6-luna")  # type: ignore[arg-type]


def _make_settings() -> Settings:
    """시나리오 테스트용 공통 Settings를 생성한다."""
    return Settings(
        _env_file=None,
        openai_api_key=SECRET_API_KEY,
        internal_service_token=SECRET_TOKEN,
        redis_url="redis://127.0.0.1:1/0",
        spring_mcp_sse_url="http://backend.test:8080/sse",
        github_token="gh-secret-token",
        github_repo="acme/payment-service",
        grafana_prometheus_url="https://grafana.test/api/prom",
        grafana_instance_id="12345",
        grafana_metrics_token="gf-secret-token",
        google_calendar_base_url="https://calendar.test/calendar/v3",
        google_calendar_id="freeze@group.calendar.google.com",
        google_calendar_token=SECRET_CAL_KEY,
        max_replan_attempts=3,
    )


def _wire_supervisor(scenario: _ScenarioState, *, max_replan: int = 3):
    """supervisor + log_agent + remediation_agent 3개 앱을 실 A2A로 연결한다."""
    settings = _make_settings()
    settings.max_replan_attempts = max_replan
    backend = scenario.build_backend_client(settings)
    ext_tools = ExternalToolsClient(settings, scenario.build_ext_http())
    decisions = _ScriptedDecisions(scenario.verdicts)

    log_app = create_log_agent_app(settings)
    rem_app = create_remediation_agent_app(settings)
    sup_app = create_supervisor_app(settings)
    a2a_count = {"calls": 0}

    async def in_process_a2a(base_url: str, payload: dict[str, Any], context_id: str) -> dict[str, Any]:
        a2a_count["calls"] += 1
        target = log_app if base_url == settings.log_agent_a2a_url else rem_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=target), base_url=base_url) as client:
            return await send_a2a_json(client, base_url=base_url, internal_token=SECRET_TOKEN, payload=payload, context_id=context_id)

    return settings, backend, ext_tools, decisions, log_app, rem_app, sup_app, a2a_count, in_process_a2a


def _run_agui(client: TestClient, payload: dict[str, Any]):
    """POST /api/ag-ui 요청을 전송한다."""
    return client.post("/api/ag-ui", headers={"X-Internal-Token": SECRET_TOKEN}, json=payload)


def test_scenario_1_first_try_satisfied_and_approved_to_resolved() -> None:
    """시나리오 1: 변경 금지 충돌 없이 한 번에 조건 충족 -> 승인 -> RESOLVED + 포스트모템 반영."""
    scenario = _ScenarioState(freeze_windows=[])
    settings, backend, ext_tools, decisions, log_app, rem_app, sup_app, _, a2a_fn = _wire_supervisor(scenario)

    with TestClient(log_app), TestClient(rem_app), TestClient(sup_app) as client:
        log_app.state.container.backend_client = backend
        rem_app.state.container.external_tools = ext_tools
        rem_app.state.container.decisions_client = decisions
        sup_app.state.container.backend_client = backend
        sup_app.state.container.a2a_caller = a2a_fn

        res1 = _run_agui(client, {
            "threadId": "s1-normal", "runId": "run-s1-1", "state": {},
            "messages": [{"id": "m1", "role": "user", "content": "502 비율 1% 미만 후보로 14:28~14:50 사이 5분 롤백 계획 세워줘"}],
            "tools": [], "context": [],
            "forwardedProps": {"incidentCode": "INC-2026-001", "incidentStartedAt": "2026-11-18T14:04:00+09:00"},
        })
        assert res1.status_code == 200
        events1 = _parse_sse(res1.text)
        snap1 = [e["snapshot"] for e in events1 if e["type"] == "STATE_SNAPSHOT"][-1]
        assert snap1["workflowStatus"] == "AWAITING_APPROVAL"
        assert snap1["replanHistory"] == []
        assert snap1["plan"]["workWindow"]["start"].startswith("2026-11-18T14:28:00")
        assert snap1["plan"]["workWindow"]["end"].startswith("2026-11-18T14:33:00")
        approval_id = snap1["approval"]["approvalId"]

        # 승인 전에는 RESOLVED가 아니며, 승인 후 A2A 실행 -> MCP 완료 -> RESOLVED + 포스트모템 반영
        res2 = _run_agui(client, {
            "threadId": "s1-normal", "runId": "run-s1-2", "state": {}, "messages": [], "tools": [], "context": [],
            "forwardedProps": {"incidentCode": "INC-2026-001", "approvalDecision": {"approvalId": approval_id, "decision": "APPROVED"}},
        })
        assert res2.status_code == 200
        snap2 = [e["snapshot"] for e in _parse_sse(res2.text) if e["type"] == "STATE_SNAPSHOT"][-1]
        assert snap2["workflowStatus"] == "RESOLVED"
        assert scenario.incident_status == "RESOLVED"
        assert scenario.postmortem is not None and scenario.postmortem["rootCause"]
        assert sup_app.state.container.logging_sub_agent.llm_calls == 0


def test_scenario_2_time_conflict_replan_reuses_external_tool_results() -> None:
    """시나리오 2: 14:30~14:40 변경 금지 충돌 시 기존 외부 Tool 결과를 재사용(중복 0건)해 14:40~14:45로 재계획한다."""
    scenario = _ScenarioState(freeze_windows=[{"start": "2026-11-18T14:30:00+09:00", "end": "2026-11-18T14:40:00+09:00"}])
    settings, backend, ext_tools, decisions, log_app, rem_app, sup_app, _, a2a_fn = _wire_supervisor(scenario)

    with TestClient(log_app), TestClient(rem_app), TestClient(sup_app) as client:
        log_app.state.container.backend_client = backend
        rem_app.state.container.external_tools = ext_tools
        rem_app.state.container.decisions_client = decisions
        sup_app.state.container.backend_client = backend
        sup_app.state.container.a2a_caller = a2a_fn

        res1 = _run_agui(client, {
            "threadId": "s2-replan", "runId": "run-s2-1", "state": {},
            "messages": [{"id": "m1", "role": "user", "content": "502 비율 1% 미만 이전 버전으로 14:28~14:50 사이 5분 롤백 계획 세워줘"}],
            "tools": [], "context": [],
            "forwardedProps": {"incidentCode": "INC-2026-001", "incidentStartedAt": "2026-11-18T14:04:00+09:00"},
        })
        assert res1.status_code == 200
        events = _parse_sse(res1.text)
        assert any(e["type"] == "CUSTOM" and e["name"] == "replan" for e in events)
        snap = [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"][-1]
        assert snap["workflowStatus"] == "AWAITING_APPROVAL"
        assert snap["plan"]["workWindow"]["start"].startswith("2026-11-18T14:40:00")
        assert snap["plan"]["workWindow"]["end"].startswith("2026-11-18T14:45:00")
        assert len(snap["replanHistory"]) == 1
        # 재계획 시에도 외부 Tool은 최소 1회만 호출(GitHub 1, Grafana 4[2개 버전 x 2 PromQL], Calendar 1)
        assert scenario.tool_hits == {"github": 1, "grafana": 4, "calendar": 1}

        # 후속 요청에서 작업 시간만 4분으로 변경해도 GitHub·Grafana·Calendar 캐시 재사용(중복 호출 0건)
        res2 = _run_agui(client, {
            "threadId": "s2-replan", "runId": "run-s2-2", "state": {},
            "messages": [{"id": "m2", "role": "user", "content": "작업 시간만 4분으로 바꿔서 다시 계획해줘"}],
            "tools": [], "context": [],
            "forwardedProps": {"incidentCode": "INC-2026-001"},
        })
        assert res2.status_code == 200
        assert scenario.tool_hits == {"github": 1, "grafana": 4, "calendar": 1}


def test_scenario_3_replan_limit_stops_cleanly_at_3() -> None:
    """시나리오 3: 재계획 상한(3회) 도달 시 무한 루프 없이 중단하고 사유를 안내한다."""
    scenario = _ScenarioState(verdicts=["NEED_MORE_EVIDENCE"], extra_chunks=12)
    settings, backend, ext_tools, decisions, log_app, rem_app, sup_app, _, a2a_fn = _wire_supervisor(scenario, max_replan=3)

    with TestClient(log_app), TestClient(rem_app), TestClient(sup_app) as client:
        log_app.state.container.backend_client = backend
        rem_app.state.container.external_tools = ext_tools
        rem_app.state.container.decisions_client = decisions
        sup_app.state.container.backend_client = backend
        sup_app.state.container.a2a_caller = a2a_fn

        res = _run_agui(client, {
            "threadId": "s3-cap", "runId": "run-s3-1", "state": {},
            "messages": [{"id": "m1", "role": "user", "content": "14:28~14:50 사이 5분 롤백 계획 세워줘"}],
            "tools": [], "context": [],
            "forwardedProps": {"incidentCode": "INC-2026-001", "incidentStartedAt": "2026-11-18T14:04:00+09:00"},
        })
        assert res.status_code == 200
        events = _parse_sse(res.text)
        snap = [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"][-1]
        text = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
        assert snap["workflowStatus"] == "BLOCKED"
        assert "상한(3회)" in text
        # 초기 1회 + 재계획 3회 = 총 4회 evaluate 실행
        eval_steps = [e for e in events if e["type"] == "STEP_STARTED" and e["stepName"] == "evaluate"]
        assert len(eval_steps) == 4


def test_scenario_4_external_tool_failure_keeps_service_alive_and_hides_secrets() -> None:
    """시나리오 4: 외부 Tool 1종(Calendar) 실패 시 서비스가 중단되지 않고 시크릿/스택이 노출되지 않는다(NF3001~NF3003)."""
    scenario = _ScenarioState(calendar_status=503)
    settings, backend, ext_tools, decisions, log_app, rem_app, sup_app, _, a2a_fn = _wire_supervisor(scenario)

    with TestClient(log_app), TestClient(rem_app), TestClient(sup_app) as client:
        log_app.state.container.backend_client = backend
        rem_app.state.container.external_tools = ext_tools
        rem_app.state.container.decisions_client = decisions
        sup_app.state.container.backend_client = backend
        sup_app.state.container.a2a_caller = a2a_fn

        res = _run_agui(client, {
            "threadId": "s4-tool-fail", "runId": "run-s4-1", "state": {},
            "messages": [{"id": "m1", "role": "user", "content": "14:28~14:50 사이 5분 롤백 계획 세워줘"}],
            "tools": [], "context": [],
            "forwardedProps": {"incidentCode": "INC-2026-001", "incidentStartedAt": "2026-11-18T14:04:00+09:00"},
        })
        assert res.status_code == 200
        events = _parse_sse(res.text)
        snap = [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"][-1]
        assert snap["workflowStatus"] == "BLOCKED"
        assert "UPSTREAM_ERROR" in res.text
        for forbidden in (SECRET_TOKEN, SECRET_API_KEY, SECRET_CAL_KEY, "Traceback", "gh-secret-token", "gf-secret-token"):
            assert forbidden not in res.text

        # 실행 로그에도 시크릿이 남지 않아야 한다
        log_res = client.get("/api/ag-ui/sessions/s4-tool-fail", headers={"X-Internal-Token": SECRET_TOKEN})
        assert log_res.status_code == 200
        assert "변경 금지 시간 조회 실패" in res.text
        for forbidden in (SECRET_TOKEN, SECRET_API_KEY, SECRET_CAL_KEY, "Traceback"):
            assert forbidden not in log_res.text


def test_scenario_5_tool_free_request_skips_workers() -> None:
    """시나리오 5: Tool이 불필요한 인사/사용법 질문은 plan/execute/evaluate를 건너뛰고 A2A·Tool 0회로 응답한다."""
    scenario = _ScenarioState()
    settings, backend, ext_tools, decisions, log_app, rem_app, sup_app, a2a_count, a2a_fn = _wire_supervisor(scenario)

    with TestClient(log_app), TestClient(rem_app), TestClient(sup_app) as client:
        log_app.state.container.backend_client = backend
        rem_app.state.container.external_tools = ext_tools
        rem_app.state.container.decisions_client = decisions
        sup_app.state.container.backend_client = backend
        sup_app.state.container.a2a_caller = a2a_fn

        res = _run_agui(client, {
            "threadId": "s5-chat", "runId": "run-s5-1", "state": {},
            "messages": [{"id": "m1", "role": "user", "content": "안녕하세요, 어떤 기능을 사용할 수 있나요?"}],
            "tools": [], "context": [],
            "forwardedProps": {"incidentCode": "INC-2026-001"},
        })
        assert res.status_code == 200
        events = _parse_sse(res.text)
        step_names = [e["stepName"] for e in events if e["type"] == "STEP_STARTED"]
        assert step_names == ["analyze", "respond"]
        assert a2a_count["calls"] == 0
        assert scenario.tool_hits == {"github": 0, "grafana": 0, "calendar": 0}
        assert scenario.mcp_hits == []


def test_nonfunctional_unapproved_completion_409_and_docker_url_derivation() -> None:
    """미승인 완료 호출 409 거절, 중복 승인 409 HTTP 응답, SPRING_MCP_SSE_URL 기반 REST URL 자동 유도를 검증한다."""
    import asyncio

    docker_settings = Settings(_env_file=None, internal_service_token=SECRET_TOKEN, spring_mcp_sse_url="http://backend:8080/sse")
    assert docker_settings.backend_base_url == "http://backend:8080"

    scenario = _ScenarioState()
    settings, backend, ext_tools, decisions, log_app, rem_app, sup_app, _, a2a_fn = _wire_supervisor(scenario)
    scenario.approvals["APR-PENDING"] = {"approvalId": "APR-PENDING", "planId": "PLAN-1", "stepNo": 2, "decision": "PENDING"}

    # 1) 미승인(PENDING) 상태에서 MCP 완료 호출 시 409 거절
    with pytest.raises(BackendApiError) as exc_info:
        asyncio.run(backend.complete_incident_with_postmortem(approval_id="APR-PENDING", incident_code="INC-2026-001", action_command="kubectl rollout undo", executed_by="remediation-agent", execution_status="SUCCESS"))
    assert exc_info.value.status_code == 409
    assert scenario.incident_status == "OPEN"

    # 2) 이미 결정된 승인 건에 재승인 요청 시 스트림 오픈 전 HTTP 409 반환
    scenario.approvals["APR-DONE"] = {"approvalId": "APR-DONE", "planId": "PLAN-1", "stepNo": 2, "decision": "APPROVED"}
    with TestClient(sup_app) as client:
        sup_app.state.container.backend_client = scenario.build_backend_client(settings)
        sup_app.state.container.a2a_caller = a2a_fn
        conflict_res = _run_agui(client, {
            "threadId": "s-409", "runId": "run-409", "state": {}, "messages": [], "tools": [], "context": [],
            "forwardedProps": {"incidentCode": "INC-2026-001", "approvalDecision": {"approvalId": "APR-DONE", "decision": "APPROVED"}},
        })
        assert conflict_res.status_code == 409
        assert "application/json" in conflict_res.headers.get("content-type", "")


def test_review_fixes_resume_approved_and_incident_binding() -> None:
    """P1 중단된 APPROVED 실행 재개, 이미 실행된 건 409 유지, 다른 incidentCode 스레드 격리를 검증한다."""
    scenario = _ScenarioState(freeze_windows=[])
    settings, _, ext_tools, decisions, log_app, rem_app, sup_app, _, a2a_fn = _wire_supervisor(scenario)
    executions: list[dict[str, Any]] = []

    def rest_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/INC-2026-001"):
            return httpx.Response(200, json={"incident": {"incidentCode": "INC-2026-001", "serviceName": "payment-service", "status": scenario.incident_status}, "approvals": list(scenario.approvals.values()), "executions": executions})
        if request.method == "POST" and path.endswith("/plans"):
            return httpx.Response(201, json=json.loads(request.content))
        if request.method == "POST" and path.endswith("/approvals"):
            body = json.loads(request.content)
            body["decision"] = "PENDING"
            scenario.approvals[body["approvalId"]] = body
            return httpx.Response(201, json=body)
        if request.method == "PATCH" and "/approvals/" in path:
            aid = path.rsplit("/", 1)[-1]
            apr = scenario.approvals[aid]
            if apr["decision"] != "PENDING":
                return httpx.Response(409, json={"message": "이미 결정이 완료된 승인 요청입니다."})
            apr["decision"] = json.loads(request.content)["decision"]
            return httpx.Response(200, json=apr)
        return httpx.Response(404)

    async def mcp_caller(tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name == "searchCorpusIndex":
            return [{"chunkId": c["chunkId"], "corpusType": c["corpusType"], "title": c["title"], "summary": c["content"][:80]} for c in scenario.chunks.values() if c["corpusType"] == args["corpusType"]]
        if tool_name == "pullCorpusChunk":
            return scenario.chunks[args["chunkId"]]
        if tool_name == "completeIncidentWithPostMortem":
            executions.append({"approvalId": args["approvalId"], "status": "SUCCESS"})
            scenario.incident_status = "RESOLVED"
            return {"incidentCode": args["incidentCode"], "status": "RESOLVED"}
        return {}

    backend = BackendClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(rest_handler)), mcp_caller=mcp_caller)
    fail_once = {"done": False}

    async def flaky_a2a(base_url: str, payload: dict[str, Any], context_id: str) -> dict[str, Any]:
        if payload.get("action") == "execute" and not fail_once["done"]:
            fail_once["done"] = True
            raise httpx.ReadTimeout("worker timeout")
        return await a2a_fn(base_url, payload, context_id)

    with TestClient(log_app), TestClient(rem_app), TestClient(sup_app) as client:
        log_app.state.container.backend_client = backend
        rem_app.state.container.external_tools = ext_tools
        rem_app.state.container.decisions_client = decisions
        sup_app.state.container.backend_client = backend
        sup_app.state.container.a2a_caller = flaky_a2a

        r1 = _run_agui(client, {"threadId": "t-resume", "runId": "r1", "state": {}, "messages": [{"id": "m1", "role": "user", "content": "502 비율 1% 미만 14:28~14:50 5분 롤백 계획"}], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-2026-001", "incidentStartedAt": "2026-11-18T14:04:00+09:00"}})
        aid = [e["snapshot"] for e in _parse_sse(r1.text) if e["type"] == "STATE_SNAPSHOT"][-1]["approval"]["approvalId"]

        # 1차 승인 시 A2A 타임아웃으로 중단되지만 AWAITING_EXECUTION_RESULT가 저장된다
        r2 = _run_agui(client, {"threadId": "t-resume", "runId": "r2", "state": {}, "messages": [], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-2026-001", "approvalDecision": {"approvalId": aid, "decision": "APPROVED"}}})
        assert r2.status_code == 200 and scenario.incident_status == "OPEN"

        # 2차 재시도 시 백엔드 409 대신 미완료 APPROVED 건을 복구·재개해 RESOLVED로 마무리한다
        r3 = _run_agui(client, {"threadId": "t-resume", "runId": "r3", "state": {}, "messages": [], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-2026-001", "approvalDecision": {"approvalId": aid, "decision": "APPROVED"}}})
        assert r3.status_code == 200
        assert [e["snapshot"] for e in _parse_sse(r3.text) if e["type"] == "STATE_SNAPSHOT"][-1]["workflowStatus"] == "RESOLVED"

        # 이미 완료된 건을 다시 승인 요청하면 409를 반환한다
        r4 = _run_agui(client, {"threadId": "t-resume", "runId": "r4", "state": {}, "messages": [], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-2026-001", "approvalDecision": {"approvalId": aid, "decision": "APPROVED"}}})
        assert r4.status_code == 409

        # 같은 threadId로 다른 장애(INC-2026-999)를 restore해도 이전 장애 스냅샷·메시지가 상속되지 않는다
        r_other = _run_agui(client, {"threadId": "t-resume", "runId": "r5", "state": {}, "messages": [], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-2026-999", "restore": True}})
        ev_other = _parse_sse(r_other.text)
        snap_other = [e["snapshot"] for e in ev_other if e["type"] == "STATE_SNAPSHOT"][-1]
        msgs_other = [e["messages"] for e in ev_other if e["type"] == "MESSAGES_SNAPSHOT"][-1]
        assert snap_other["incidentCode"] == "INC-2026-999" and snap_other["workflowStatus"] == "IDLE" and snap_other["plan"] is None
        assert msgs_other == []


async def test_review_fixes_log_broaden_and_redis_reconcile_and_live_stream() -> None:
    """키워드 소진 시 로그 전체 확장, Redis 복구 재반영, 그래프 실행 중 실시간 이벤트 방출을 검증한다."""
    import asyncio
    from log_agent.schemas import LogInvestigationRequest
    from log_agent.services import run_dr_dci_investigation
    from supervisor.cruds import SupervisorRedisStore
    from supervisor.schemas import ManagedPlanCreate
    from supervisor.services import SupervisorService
    from ag_ui.core import RunAgentInput

    # 1) log_agent: 첫 키워드 매칭 청크가 모두 excluded면 나머지 로그까지 확장 조회한다
    settings = _make_settings()
    all_logs = [
        {"chunkId": "LOG-104", "corpusType": "LOG", "title": "HikariPool timeout", "keywords": "HikariPool", "sequenceOrder": 1},
        {"chunkId": "LOG-109", "corpusType": "LOG", "title": "Deploy v42 marker", "keywords": "deploy", "sequenceOrder": 2},
    ]

    async def mcp_broaden(tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name == "searchCorpusIndex" and args["corpusType"] == "LOG":
            kw = args.get("keyword")
            return [r for r in all_logs if kw.lower() in r["keywords"].lower()] if kw else all_logs
        if tool_name == "searchCorpusIndex":
            return []
        return {"chunkId": args["chunkId"], "corpusType": "LOG", "title": args["chunkId"], "content": "deploy v42 details"}

    backend = BackendClient(settings, httpx.AsyncClient(), mcp_caller=mcp_broaden)
    log_out = await run_dr_dci_investigation(backend, settings, LogInvestigationRequest(incidentCode="INC-2026-001", keywords=["HikariPool"], excludeChunkIds=["LOG-104"], maxPulls=2))
    assert log_out.evidenceChunkIds == ["LOG-109"]

    # 2) Redis 폴백 동안 기록된 계획·로그가 Redis 복구 시 자동 재반영된다
    class _ToggleRedis:
        def __init__(self) -> None:
            self.up = False
            self.kv: dict[str, str] = {}
            self.sets: dict[str, set[str]] = {}
            self.lists: dict[str, list[str]] = {}

        async def ping(self) -> bool:
            if not self.up:
                raise ConnectionError("down")
            return True

        async def get(self, k: str) -> str | None:
            return self.kv.get(k)

        async def set(self, k: str, v: str) -> None:
            self.kv[k] = v

        async def delete(self, k: str) -> int:
            return int(self.kv.pop(k, None) is not None or self.lists.pop(k, None) is not None)

        async def sadd(self, k: str, v: str) -> None:
            self.sets.setdefault(k, set()).add(v)

        async def srem(self, k: str, v: str) -> None:
            self.sets.setdefault(k, set()).discard(v)

        async def smembers(self, k: str) -> set[str]:
            return set(self.sets.get(k, set()))

        async def rpush(self, k: str, v: str) -> None:
            self.lists.setdefault(k, []).append(v)

        async def lrange(self, k: str, s: int, e: int) -> list[str]:
            return list(self.lists.get(k, []))

    fake_redis = _ToggleRedis()
    store = SupervisorRedisStore(fake_redis)
    created = await store.create_plan(ManagedPlanCreate(incidentCode="INC-2026-001", title="outage-plan", planDate="2026-11-20", startTime="14:00", endTime="14:30"))
    await store.append_session_log("outage-s1", {"sessionId": "outage-s1", "eventType": "step_started", "timestamp": "2026-11-19T00:00:00Z", "payload": {}})

    # Redis 복구 + 쿨다운 만료 후 조회해도 폴백 데이터가 사라지지 않는다
    fake_redis.up = True
    store._redis_retry_after = 0.0
    recovered_plan = await store.get_plan(created.plan.planId)
    recovered_logs = await store.get_session_logs("outage-s1")
    assert recovered_plan is not None and recovered_plan.title == "outage-plan"
    assert len(recovered_logs) == 1

    # 3) 실시간 스트리밍: 워커가 대기 중일 때도 앞선 STEP_STARTED 이벤트가 먼저 스트리밍된다
    gate = asyncio.Event()

    async def gated_a2a(base_url: str, payload: dict[str, Any], context_id: str) -> dict[str, Any]:
        await gate.wait()
        return {"ok": False, "status": "BLOCKED", "message": "done", "toolCalls": [], "cachedToolResults": {}}

    svc = SupervisorService(settings, httpx.AsyncClient(), backend, SupervisorRedisStore(None), a2a_caller=gated_a2a)
    req_in = RunAgentInput(thread_id="t-stream", run_id="r-stream", state={}, messages=[{"id": "m1", "role": "user", "content": "502 롤백 계획 세워줘"}], tools=[], context=[], forwarded_props={"incidentCode": "INC-2026-001", "incidentStartedAt": "2026-11-18T14:04:00+09:00"})
    props = svc.parse_forwarded_props(req_in)
    gen = svc.stream_ag_ui(req_in, props, "op", None, None)

    seen_before_gate: list[str] = []
    for _ in range(5):
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        seen_before_gate.extend(e["type"] for e in _parse_sse(chunk))
    assert "RUN_STARTED" in seen_before_gate and "STEP_STARTED" in seen_before_gate
    gate.set()
    async for _ in gen:
        pass

