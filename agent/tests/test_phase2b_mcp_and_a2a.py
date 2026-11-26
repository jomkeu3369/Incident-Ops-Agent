from __future__ import annotations

from types import SimpleNamespace
import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from common.a2a_utils import send_a2a_json
from common.backend_client import BackendApiError, BackendClient
from common.config import Settings
from common.decisions import DecisionsClient
from common.tools import ExternalToolsClient
from log_agent.main import create_log_agent_app
from log_agent.schemas import LogInvestigationRequest
from log_agent.services import run_dr_dci_investigation
from remediation_agent.main import create_remediation_agent_app


def _settings(**overrides) -> Settings:
    """테스트용 공통 설정을 생성한다."""
    base = {
        "internal_service_token": SecretStr("a2a-secret"),
        "dr_dci_max_pull_steps": 3,
        "dr_dci_max_chunk_chars": 400,
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


def _fake_external_transport() -> httpx.MockTransport:
    """결제 장애 시나리오(v42 18% vs v41 0.3%, 변경 금지 14:30~14:40)용 외부 API 모의 전송기."""
    cal_id = "freeze@group.calendar.google.com"

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "/deployments/" in url and url.endswith("/statuses?per_page=30"):
            dep_id = req.url.path.split("/")[-2]
            created = "2026-11-18T14:00:00+09:00" if dep_id == "42" else "2026-11-18T12:00:00+09:00"
            return httpx.Response(200, json=[{"state": "success", "created_at": created}])
        if req.url.path.endswith("/deployments"):
            return httpx.Response(200, json=[{"id": 42, "sha": "sha-v42", "ref": "v42"}, {"id": 41, "sha": "sha-v41", "ref": "v41"}])
        if req.url.path.endswith("/api/v1/query"):
            q = req.url.params["query"]
            if 'git_sha="sha-v42"' in q:
                val = "180" if 'status="502"' in q else "1000"
            else:
                val = "3" if 'status="502"' in q else "1000"
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, val]}]}})
        if req.url.path.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {cal_id: {"busy": [{"start": "2026-11-18T14:30:00+09:00", "end": "2026-11-18T14:40:00+09:00"}]}}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _fake_decisions(choice: str) -> DecisionsClient:
    """지정한 판정값(SUPPORTED|NEED_MORE_EVIDENCE|CONTRADICTED)을 반환하는 모의 판정 클라이언트."""
    ans = SimpleNamespace(type="choice", choice=choice, confidence=0.99, probabilities=[SimpleNamespace(value=choice, probability=0.99)])
    resp = SimpleNamespace(answers=[ans], usage=SimpleNamespace(input_tokens=90))
    fake_openai = SimpleNamespace(decisions=SimpleNamespace(create=lambda **kw: resp))
    return DecisionsClient(_settings(), openai_client=fake_openai)


def test_a2a_agent_cards_and_rpc_auth():
    """두 워커 에이전트가 표준 AgentCard를 노출하고 A2A RPC에 내부 토큰을 강제하는지 검증한다."""
    for expected_name, app in [("log-analyzer-agent", create_log_agent_app(_settings())), ("remediation-agent", create_remediation_agent_app(_settings()))]:
        with TestClient(app) as client:
            card = client.get("/.well-known/agent-card.json")
            assert card.status_code == 200
            assert card.json()["name"] == expected_name

            # 토큰 없는 A2A JSON-RPC 호출은 401로 차단된다
            denied = client.post("/", json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {}})
            assert denied.status_code == 401


async def test_log_agent_a2a_dr_dci_pulls_prioritized_chunks():
    """log_agent가 A2A SendMessage로 요청을 받아 MCP DR-DCI로 핵심 로그와 런북만 인출하는지 검증한다."""
    cfg = _settings(dr_dci_max_pull_steps=3)
    app = create_log_agent_app(cfg)

    async def fake_mcp(tool_name: str, args: dict):
        if tool_name == "searchCorpusIndex" and args["corpusType"] == "LOG":
            return [
                {"chunkId": "LOG-101", "corpusType": "LOG", "title": "정상 트래픽", "keywords": "ok", "sequenceOrder": 1},
                {"chunkId": "LOG-104", "corpusType": "LOG", "title": "HikariPool timeout", "keywords": "HikariPool,502", "sequenceOrder": 4},
                {"chunkId": "LOG-105", "corpusType": "LOG", "title": "connection leak", "keywords": "leak,connection", "sequenceOrder": 5},
                {"chunkId": "LOG-106", "corpusType": "LOG", "title": "기타 로그", "keywords": "info", "sequenceOrder": 6},
            ]
        if tool_name == "searchCorpusIndex" and args["corpusType"] == "RUNBOOK":
            return [{"chunkId": "RUNBOOK-01", "corpusType": "RUNBOOK", "title": "커넥션 풀 고갈 롤백 런북", "keywords": "HikariPool,rollback", "sequenceOrder": 1}]
        if tool_name == "pullCorpusChunk":
            contents = {
                "LOG-104": "14:04 HikariPool-1 connection timeout & 502 spike after v42 deploy",
                "LOG-105": "14:05 Detected unclosed connection leak in PaymentClient",
                "RUNBOOK-01": "배포 직후 커넥션 누수 확인 시 롤백:\n`kubectl rollout undo deployment/payment-service`",
            }
            return {"chunkId": args["chunkId"], "corpusType": "RUNBOOK" if args["chunkId"].startswith("RUNBOOK") else "LOG", "title": args["chunkId"], "content": contents[args["chunkId"]]}
        raise AssertionError(f"Unexpected MCP call: {tool_name}")

    async with app.router.lifespan_context(app):
        app.state.container.backend_client = BackendClient(cfg, app.state.container.http_client, mcp_caller=fake_mcp)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://log-agent") as http:
            res = await send_a2a_json(http, base_url="http://log-agent", internal_token="a2a-secret", payload={"incidentCode": "INC-2026-001", "keywords": ["HikariPool"]})

    assert res["ok"] is True
    assert res["pulledCount"] == 3
    assert set(res["evidenceChunkIds"]) == {"LOG-104", "LOG-105", "RUNBOOK-01"}
    assert res["recommendedCommand"] == "kubectl rollout undo deployment/payment-service"
    assert "LOG-101" in res["remainingChunkIds"]


async def test_dr_dci_fills_budget_from_full_index_when_keywords_hit_too_few_logs():
    """장애 제목에서 뽑은 키워드가 증상 로그 하나만 찾더라도 남은 예산으로 원인 로그를 인출한다."""
    rows = {
        "LOG-101": ("LOG", "정상 트래픽 및 헬스체크 로그", "healthcheck,200,normal"),
        "LOG-102": ("LOG", "배포 롤아웃 시작 이벤트", "deploy,rollout,v2.4.1"),
        "LOG-103": ("LOG", "외부 PG사 웹훅 수신 로그", "webhook,pg,callback"),
        "LOG-104": ("LOG", "HikariPool 커넥션 타임아웃 급증", "HikariPool,Connection,timeout"),
        "LOG-105": ("LOG", "커넥션 누수 감지 스택트레이스", "leak,Connection"),
        "LOG-106": ("LOG", "게이트웨이 502 Upstream 응답 기록", "502,Bad Gateway,upstream"),
        "RUNBOOK-01": ("RUNBOOK", "DB 커넥션 풀 고갈 및 누수 긴급 복구 런북", "HikariPool,leak,rollback"),
    }

    async def fake_mcp(tool_name: str, args: dict):
        if tool_name == "searchCorpusIndex":
            keyword = args.get("keyword") or ""
            return [
                {"chunkId": cid, "corpusType": kind, "title": title, "keywords": keywords, "sequenceOrder": order}
                for order, (cid, (kind, title, keywords)) in enumerate(rows.items())
                if kind == args["corpusType"] and (keyword in title or keyword in keywords)
            ]
        kind, title, _ = rows[args["chunkId"]]
        return {"chunkId": args["chunkId"], "corpusType": kind, "title": title, "content": title}

    cfg = _settings(dr_dci_max_pull_steps=5)
    async with httpx.AsyncClient() as http:
        result = await run_dr_dci_investigation(
            BackendClient(cfg, http, mcp_caller=fake_mcp),
            cfg,
            LogInvestigationRequest(incidentCode="INC-2026-001", keywords=["502", "Bad", "Gateway"], maxPulls=5),
        )

    assert {"LOG-104", "LOG-105", "LOG-106", "RUNBOOK-01"} <= set(result.evidenceChunkIds)
    assert result.pulledCount == 5
    assert {"LOG-101", "LOG-103"} <= set(result.remainingChunkIds)


async def test_remediation_agent_a2a_three_verdicts_cache_reuse_and_execute():
    """remediation_agent가 A2A로 3개 판정 분기, 캐시 재사용(중복 호출 0건), 승인 실행을 처리하는지 검증한다."""
    cfg = _settings()
    base_payload = {
        "action": "plan",
        "incidentCode": "INC-2026-001",
        "serviceName": "payment-service",
        "incidentStartedAt": "2026-11-18T14:04:00+09:00",
        "metricsFrom": "2026-11-18T14:05:00+09:00",
        "metricsTo": "2026-11-18T14:10:00+09:00",
        "allowedWindowStart": "2026-11-18T14:28:00+09:00",
        "allowedWindowEnd": "2026-11-18T14:50:00+09:00",
        "durationMinutes": 5,
        "max502RatePercent": 1.0,
        "evidence": [{"chunkId": "LOG-104", "summary": "배포 후 HikariPool timeout 및 502 급증", "corpusType": "LOG"}, {"chunkId": "RUNBOOK-01", "summary": "커넥션 누수 시 롤백", "corpusType": "RUNBOOK"}],
        "recommendedCommand": "kubectl rollout undo deployment/payment-service",
    }

    # 1) SUPPORTED + 재계획(14:40~14:45) + 캐시 재사용 검증
    app = create_remediation_agent_app(cfg)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=_fake_external_transport()) as ext_http:
            app.state.container.external_tools = ExternalToolsClient(cfg, ext_http)
            app.state.container.decisions_client = _fake_decisions("SUPPORTED")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rem-agent") as http:
                res = await send_a2a_json(http, base_url="http://rem-agent", internal_token="a2a-secret", payload=base_payload)
                assert res["status"] == "READY_FOR_APPROVAL"
                assert res["verdict"] == "SUPPORTED"
                assert res["replanEvent"]["nextWindow"]["start"] == "2026-11-18T14:40:00+09:00"
                assert len(res["toolCalls"]) == 4  # candidates + ref metrics + v41 metrics + freeze

                # 캐시를 넘겨 재호출하면 외부 Tool 중복 호출이 0건이어야 한다
                cached_payload = {**base_payload, "cachedToolResults": res["cachedToolResults"]}
                reused = await send_a2a_json(http, base_url="http://rem-agent", internal_token="a2a-secret", payload=cached_payload)
                assert reused["status"] == "READY_FOR_APPROVAL"
                assert reused["toolCalls"] == []

                # 2) NEED_MORE_EVIDENCE 분기
                app.state.container.decisions_client = _fake_decisions("NEED_MORE_EVIDENCE")
                more = await send_a2a_json(http, base_url="http://rem-agent", internal_token="a2a-secret", payload=cached_payload)
                assert more["status"] == "NEED_MORE_EVIDENCE"

                # 3) CONTRADICTED 분기 (롤백 명령 제거된 수정 계획)
                app.state.container.decisions_client = _fake_decisions("CONTRADICTED")
                revised = await send_a2a_json(http, base_url="http://rem-agent", internal_token="a2a-secret", payload=cached_payload)
                assert revised["status"] == "REVISED_NO_ROLLBACK"
                assert all(s["actionCommand"] is None for s in revised["planDraft"]["steps"])

                # 4) 승인 조치 실행(execute) 모드
                exec_res = await send_a2a_json(
                    http,
                    base_url="http://rem-agent",
                    internal_token="a2a-secret",
                    payload={"action": "execute", "incidentCode": "INC-2026-001", "approvalId": "APR-2026-001", "planId": "PLAN-2026-001", "actionCommand": "kubectl rollout undo deployment/payment-service", "verificationCriteria": "502 < 1%"},
                )
                assert exec_res["ok"] is True
                assert exec_res["executionStatus"] == "SUCCESS"
                assert exec_res["verificationSummary"]


async def test_backend_rest_client_sends_token_and_maps_conflict():
    """BackendClient가 X-Internal-Token 헤더를 전송하고 409 충돌을 BackendApiError로 변환하는지 검증한다."""
    seen_headers: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_headers.append(req.headers.get("X-Internal-Token", ""))
        if req.url.path.endswith("/plans"):
            return httpx.Response(201, json={"planId": "PLAN-1"})
        return httpx.Response(409, json={"message": "이미 처리된 승인 요청입니다."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BackendClient(_settings(), http)
        plan = await client.create_plan("INC-2026-001", {"planId": "PLAN-1"})
        assert plan["planId"] == "PLAN-1"
        try:
            await client.decide_approval("INC-2026-001", "APR-1", approved_by="oncall-lead", decision="APPROVED")
            raise AssertionError("409 예외가 발생해야 합니다.")
        except BackendApiError as exc:
            assert exc.status_code == 409
            assert "이미 처리된" in exc.message

    assert seen_headers == ["a2a-secret", "a2a-secret"]
