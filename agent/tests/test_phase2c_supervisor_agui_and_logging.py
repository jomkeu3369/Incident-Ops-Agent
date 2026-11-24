from __future__ import annotations

import json
from types import SimpleNamespace
import httpx
from pydantic import SecretStr

from common.a2a_utils import send_a2a_json
from common.backend_client import BackendClient
from common.config import Settings
from common.decisions import DecisionsClient
from common.tools import ExternalToolsClient
from log_agent.main import create_log_agent_app
from remediation_agent.main import create_remediation_agent_app
from supervisor.main import create_supervisor_app


def _settings(**overrides) -> Settings:
    """Phase 2C 통합 테스트용 설정을 생성한다."""
    base = {
        "internal_service_token": SecretStr("e2e-secret"),
        "default_operator_id": "oncall-lead",
        "redis_url": "redis://127.0.0.1:1/0",  # 로컬 테스트에서는 연결 불가 시 메모리 폴백 사용
        "dr_dci_max_pull_steps": 3,
        "max_replan_attempts": 2,
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


def _parse_sse(raw_text: str) -> list[dict]:
    """AG-UI SSE 응답 본문에서 data: JSON 프레임 목록을 파싱한다."""
    events: list[dict] = []
    for line in raw_text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _fake_external_transport() -> httpx.MockTransport:
    """결제 장애 시나리오용 GitHub·Grafana·Google Calendar 모의 HTTP 전송기."""
    cal_id = "freeze@group.calendar.google.com"

    def handler(req: httpx.Request) -> httpx.Response:
        if "/deployments/" in str(req.url) and str(req.url).endswith("/statuses?per_page=30"):
            dep_id = req.url.path.split("/")[-2]
            created = "2026-11-18T14:00:00+09:00" if dep_id == "42" else "2026-11-18T12:00:00+09:00"
            return httpx.Response(200, json=[{"state": "success", "created_at": created}])
        if req.url.path.endswith("/deployments"):
            return httpx.Response(200, json=[{"id": 42, "sha": "sha-v42", "ref": "v42"}, {"id": 41, "sha": "sha-v41", "ref": "v41"}])
        if req.url.path.endswith("/api/v1/query"):
            q = req.url.params["query"]
            val = ("180" if 'status="502"' in q else "1000") if 'git_sha="sha-v42"' in q else ("3" if 'status="502"' in q else "1000")
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, val]}]}})
        if req.url.path.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {cal_id: {"busy": [{"start": "2026-11-18T14:30:00+09:00", "end": "2026-11-18T14:40:00+09:00"}]}}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _fake_decisions(choice: str) -> DecisionsClient:
    """지정 판정값을 반환하는 모의 DecisionsClient를 생성한다."""
    ans = SimpleNamespace(type="choice", choice=choice, confidence=0.97, probabilities=[SimpleNamespace(value=choice, probability=0.97)])
    resp = SimpleNamespace(answers=[ans], usage=SimpleNamespace(input_tokens=100))
    return DecisionsClient(_settings(), openai_client=SimpleNamespace(decisions=SimpleNamespace(create=lambda **kw: resp)))


async def test_three_protocol_e2e_plan_replan_restore_approve_and_complete():
    """AG-UI -> LangGraph -> A2A(2워커) -> MCP/REST -> 새로고침 복원 -> 승인·완료 전체 흐름을 검증한다."""
    cfg = _settings()
    sup_app = create_supervisor_app(cfg, attach_logger=True)
    log_app = create_log_agent_app(cfg)
    rem_app = create_remediation_agent_app(cfg)

    approvals_state: dict[str, dict] = {}
    completed_calls: list[dict] = []

    async def fake_mcp(tool_name: str, args: dict):
        if tool_name == "searchCorpusIndex" and args["corpusType"] == "LOG":
            return [{"chunkId": "LOG-104", "corpusType": "LOG", "title": "HikariPool timeout", "keywords": "HikariPool,502", "sequenceOrder": 4}]
        if tool_name == "searchCorpusIndex" and args["corpusType"] == "RUNBOOK":
            return [{"chunkId": "RUNBOOK-01", "corpusType": "RUNBOOK", "title": "롤백 런북", "keywords": "rollback", "sequenceOrder": 1}]
        if tool_name == "pullCorpusChunk":
            cid = args["chunkId"]
            content = "배포 직후 커넥션 누수 시 롤백: `kubectl rollout undo deployment/payment-service`" if cid == "RUNBOOK-01" else "14:04 HikariPool timeout & 502 spike"
            return {"chunkId": cid, "corpusType": "RUNBOOK" if cid == "RUNBOOK-01" else "LOG", "title": cid, "content": content}
        if tool_name == "completeIncidentWithPostMortem":
            completed_calls.append(args)
            return {"incidentCode": args["incidentCode"], "status": "RESOLVED"}
        raise AssertionError(f"예상치 못한 MCP 호출: {tool_name}")

    def fake_spring_rest(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/api/incidents/INC-2026-009"):
            return httpx.Response(200, json={"incident": {"incidentCode": "INC-2026-009", "serviceName": "payment-service", "status": "OPEN"}})
        if req.method == "POST" and req.url.path.endswith("/plans"):
            return httpx.Response(201, json=json.loads(req.content))
        if req.method == "POST" and req.url.path.endswith("/approvals"):
            body = json.loads(req.content)
            approvals_state[body["approvalId"]] = {**body, "decision": "PENDING", "actionCommand": "kubectl rollout undo deployment/payment-service"}
            return httpx.Response(201, json=approvals_state[body["approvalId"]])
        if req.method == "PATCH" and "/approvals/" in req.url.path:
            aid = req.url.path.split("/")[-1]
            cur = approvals_state.get(aid)
            if not cur:
                return httpx.Response(404, json={"message": "승인 요청을 찾을 수 없습니다."})
            if cur["decision"] != "PENDING":
                return httpx.Response(409, json={"message": "이미 처리된 승인 요청입니다."})
            body = json.loads(req.content)
            assert body["approvedBy"] == "oncall-lead"
            cur["decision"] = body["decision"]
            cur["approvedBy"] = body["approvedBy"]
            return httpx.Response(200, json=cur)
        return httpx.Response(404)

    async with sup_app.router.lifespan_context(sup_app), log_app.router.lifespan_context(log_app), rem_app.router.lifespan_context(rem_app):
        async with httpx.AsyncClient(transport=_fake_external_transport()) as ext_http, httpx.AsyncClient(transport=httpx.MockTransport(fake_spring_rest)) as spring_http:
            backend = BackendClient(cfg, spring_http, mcp_caller=fake_mcp)
            sup_app.state.container.backend_client = backend
            log_app.state.container.backend_client = backend
            rem_app.state.container.external_tools = ExternalToolsClient(cfg, ext_http)
            rem_app.state.container.decisions_client = _fake_decisions("SUPPORTED")

            # Supervisor의 A2A 호출을 실제 log_agent / remediation_agent FastAPI ASGI 앱으로 연결한다
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=log_app), base_url="http://log-agent") as log_http, httpx.AsyncClient(transport=httpx.ASGITransport(app=rem_app), base_url="http://rem-agent") as rem_http:
                async def route_a2a(url: str, payload: dict, context_id: str) -> dict:
                    if url == cfg.log_agent_a2a_url:
                        return await send_a2a_json(log_http, base_url="http://log-agent", internal_token="e2e-secret", payload=payload, context_id=context_id)
                    return await send_a2a_json(rem_http, base_url="http://rem-agent", internal_token="e2e-secret", payload=payload, context_id=context_id)

                sup_app.state.container.a2a_caller = route_a2a

                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sup_app), base_url="http://supervisor") as client:
                    # 1) 계획 요청 (AG-UI SSE)
                    plan_resp = await client.post(
                        "/api/ag-ui",
                        headers={"Accept": "text/event-stream"},
                        json={
                            "threadId": "thread-009",
                            "runId": "run-009-1",
                            "state": {},
                            "messages": [{"id": "m1", "role": "user", "content": "이전 버전의 502 비율이 1% 미만이면 롤백을 제안하고 14:28~14:50 사이에서 5분 작업 창을 찾아줘."}],
                            "tools": [],
                            "context": [],
                            "forwardedProps": {"incidentCode": "INC-2026-009", "incidentStartedAt": "2026-11-18T14:04:00+09:00"},
                        },
                    )
                    assert plan_resp.status_code == 200
                    events = _parse_sse(plan_resp.text)
                    types = [e["type"] for e in events]
                    assert types[0] == "RUN_STARTED" and types[-1] == "RUN_FINISHED"
                    assert any(e["type"] == "CUSTOM" and e["name"] == "replan" for e in events)
                    snap = next(e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT")
                    assert snap["workflowStatus"] == "AWAITING_APPROVAL"
                    assert snap["plan"]["workWindow"]["start"] == "2026-11-18T14:40:00+09:00"
                    assert len(snap["replanHistory"]) == 1
                    approval_id = snap["approval"]["approvalId"]

                    # 2) 새로고침 복원 (restore=True)
                    restore_resp = await client.post(
                        "/api/ag-ui",
                        json={"threadId": "thread-009", "runId": "run-009-r", "state": {}, "messages": [], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-2026-009", "restore": True}},
                    )
                    assert restore_resp.status_code == 200
                    r_events = _parse_sse(restore_resp.text)
                    assert any(e["type"] == "STATE_SNAPSHOT" and e["snapshot"]["workflowStatus"] == "AWAITING_APPROVAL" for e in r_events)
                    assert any(e["type"] == "MESSAGES_SNAPSHOT" and len(e["messages"]) >= 2 for e in r_events)

                    # 3) 운영자 승인 -> A2A 실행 -> MCP completeIncidentWithPostMortem -> RESOLVED
                    approve_resp = await client.post(
                        "/api/ag-ui",
                        json={"threadId": "thread-009", "runId": "run-009-2", "state": {}, "messages": [], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-2026-009", "approvalDecision": {"approvalId": approval_id, "decision": "APPROVED"}}},
                    )
                    assert approve_resp.status_code == 200
                    a_events = _parse_sse(approve_resp.text)
                    snaps = [e["snapshot"]["workflowStatus"] for e in a_events if e["type"] == "STATE_SNAPSHOT"]
                    assert snaps == ["AWAITING_EXECUTION_RESULT", "RESOLVED"]
                    assert len(completed_calls) == 1 and completed_calls[0]["approvalId"] == approval_id

                    # 4) 이미 처리된 승인 재전송 시 스트림 오픈 전 HTTP 409 반환
                    dup = await client.post(
                        "/api/ag-ui",
                        json={"threadId": "thread-009", "runId": "run-009-3", "state": {}, "messages": [], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-2026-009", "approvalDecision": {"approvalId": approval_id, "decision": "APPROVED"}}},
                    )
                    assert dup.status_code == 409

                    # 5) 로깅 서브 에이전트 실행 로그 조회·삭제·수정불가 및 LLM 0회 검증
                    assert sup_app.state.container.logging_sub_agent.llm_calls == 0
                    sessions = await client.get("/api/ag-ui/sessions")
                    assert sessions.status_code == 200 and any(s["sessionId"] == "thread-009" for s in sessions.json())
                    detail = await client.get("/api/ag-ui/sessions/thread-009")
                    assert detail.status_code == 200 and len(detail.json()["events"]) >= 5
                    assert (await client.patch("/api/ag-ui/sessions/thread-009", json={})).status_code == 405
                    assert (await client.delete("/api/ag-ui/sessions/thread-009")).status_code == 204


async def test_decoupled_logger_general_chat_memory_update_replan_cap_and_plans_crud():
    """로깅 비연결 동작, Tool 불필요 요청, 대화 조건 갱신, 재계획 상한, 작업 계획 CRUD·겹침 경고를 검증한다."""
    cfg = _settings(max_replan_attempts=2)
    # 로깅 서브 에이전트를 연결하지 않아도 메인 에이전트가 정상 응답해야 한다
    app = create_supervisor_app(cfg, attach_logger=False)
    a2a_calls: list[dict] = []

    async def fake_a2a(url: str, payload: dict, context_id: str) -> dict:
        a2a_calls.append({"url": url, "payload": payload})
        if url == cfg.log_agent_a2a_url:
            idx = len(payload.get("excludeChunkIds") or [])
            return {"ok": True, "incidentCode": "INC-1", "evidence": [{"chunkId": f"LOG-{idx + 1}", "summary": "timeout", "corpusType": "LOG"}], "evidenceChunkIds": [f"LOG-{idx + 1}"], "remainingChunkIds": [f"LOG-{idx + 2}"], "pulledCount": 1}
        return {"ok": True, "status": "NEED_MORE_EVIDENCE", "incidentCode": "INC-1", "verdict": "NEED_MORE_EVIDENCE", "toolCalls": [], "cachedToolResults": {"cached": True}, "message": "근거 부족"}

    async with app.router.lifespan_context(app):
        app.state.container.a2a_caller = fake_a2a
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://supervisor") as client:
            # 1) Tool 불필요 요청 (인사) -> A2A 호출 0건
            hi = await client.post(
                "/api/ag-ui",
                json={"threadId": "t-chat", "runId": "r1", "state": {}, "messages": [{"id": "m1", "role": "user", "content": "안녕하세요, 사용법 알려주세요"}], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-1"}},
            )
            assert hi.status_code == 200
            assert a2a_calls == []
            assert any(e["type"] == "RUN_FINISHED" for e in _parse_sse(hi.text))

            # 2) restore + approvalDecision 동시 전송 -> HTTP 400
            bad = await client.post(
                "/api/ag-ui",
                json={"threadId": "t-chat", "runId": "r2", "state": {}, "messages": [], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-1", "restore": True, "approvalDecision": {"approvalId": "APR-1", "decision": "APPROVED"}}},
            )
            assert bad.status_code == 400

            # 3) 근거 부족 반복 시 재계획 상한(2회)에서 정지하고 BLOCKED 반환
            cap = await client.post(
                "/api/ag-ui",
                json={"threadId": "t-cap", "runId": "r-cap", "state": {}, "messages": [{"id": "m2", "role": "user", "content": "14:28~14:50 사이 5분 작업 창으로 롤백 계획 세워줘"}], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-1", "incidentStartedAt": "2026-11-18T14:04:00+09:00"}},
            )
            assert cap.status_code == 200
            cap_events = _parse_sse(cap.text)
            cap_snap = next(e["snapshot"] for e in cap_events if e["type"] == "STATE_SNAPSHOT")
            assert cap_snap["workflowStatus"] == "BLOCKED"
            # 초기 1회 + 재계획 2회 = 각 워커 최대 3회 호출 후 상한 정지
            assert len([c for c in a2a_calls if c["url"] == cfg.remediation_agent_a2a_url]) == 3

            # 4) 후속 대화에서 조건 하나(10분)만 변경 시 기존 incidentStartedAt·허용 시간 유지
            follow = await client.post(
                "/api/ag-ui",
                json={"threadId": "t-cap", "runId": "r-cap2", "state": {}, "messages": [{"id": "m3", "role": "user", "content": "작업 시간만 10분으로 바꿔줘"}], "tools": [], "context": [], "forwardedProps": {"incidentCode": "INC-1"}},
            )
            assert follow.status_code == 200
            last_rem_payload = [c["payload"] for c in a2a_calls if c["url"] == cfg.remediation_agent_a2a_url][-1]
            assert last_rem_payload["durationMinutes"] == 10
            assert last_rem_payload["incidentStartedAt"] == "2026-11-18T14:04:00+09:00"

            # 5) 작업 계획 CRUD 및 동일 날짜 시간 겹침 경고
            p1 = await client.post("/api/ag-ui/plans", json={"incidentCode": "INC-1", "title": "결제 롤백 1차", "planDate": "2026-11-18", "startTime": "14:40", "endTime": "14:45"})
            assert p1.status_code == 201 and p1.json()["warning"] is None
            p2 = await client.post("/api/ag-ui/plans", json={"incidentCode": "INC-2", "title": "주문 점검", "planDate": "2026-11-18", "startTime": "14:42", "endTime": "14:50"})
            assert p2.status_code == 201 and len(p2.json()["overlappingPlans"]) == 1 and p2.json()["warning"] is not None
            pid2 = p2.json()["plan"]["planId"]
            upd = await client.patch(f"/api/ag-ui/plans/{pid2}", json={"startTime": "15:00", "endTime": "15:10"})
            assert upd.status_code == 200 and upd.json()["warning"] is None
            assert (await client.delete(f"/api/ag-ui/plans/{pid2}")).status_code == 204
