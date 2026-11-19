from __future__ import annotations

from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from common.config import Settings
from common.di import get_settings
from log_agent.main import create_log_agent_app
from remediation_agent.main import create_remediation_agent_app
from supervisor.main import create_supervisor_app


def _test_settings(**overrides) -> Settings:
    """부트스트랩·보안 테스트용 기본 설정을 생성한다."""
    defaults = {"internal_service_token": SecretStr("test-secret-token"), "default_operator_id": "oncall-lead", "trust_operator_header": False, "openai_model": "gpt-test"}
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_all_three_services_health_and_security_headers():
    """3개 FastAPI 서비스의 헬스체크와 기본 보안 응답 헤더를 검증한다."""
    apps = [("supervisor-agent", create_supervisor_app(_test_settings())), ("log-analyzer-agent", create_log_agent_app(_test_settings())), ("remediation-agent", create_remediation_agent_app(_test_settings()))]
    for expected_name, app in apps:
        with TestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok", "service": expected_name}
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.headers["cache-control"] == "no-store"


def test_internal_token_fail_closed_and_verified():
    """내부 토큰 미설정·누락·불일치 시 401로 차단되고 일치 시 200을 반환하는지 검증한다."""
    for factory in (create_log_agent_app, create_remediation_agent_app):
        # 서버 토큰이 비어 있으면 fail-closed로 모든 요청을 거부해야 한다
        with TestClient(factory(_test_settings(internal_service_token=SecretStr("")))) as client:
            assert client.get("/internal/ping", headers={"X-Internal-Token": ""}).status_code == 401

        with TestClient(factory(_test_settings())) as client:
            assert client.get("/internal/ping").status_code == 401
            assert client.get("/internal/ping", headers={"X-Internal-Token": "wrong"}).status_code == 401
            ok = client.get("/internal/ping", headers={"X-Internal-Token": "test-secret-token"})
            assert ok.status_code == 200
            assert ok.json()["auth"] == "verified"


def test_di_override_and_operator_header_trust():
    """FastAPI Depends 오버라이드와 비신뢰 X-Operator-Id 헤더 무시를 검증한다."""
    app = create_supervisor_app(_test_settings(trust_operator_header=False))
    with TestClient(app) as client:
        # 신뢰 설정이 꺼져 있으면 클라이언트가 보낸 헤더를 무시해야 한다
        resp = client.get("/api/ag-ui/status", headers={"X-Operator-Id": "spoofed-user"})
        assert resp.status_code == 200
        assert resp.json()["operator"] == "oncall-lead"

        # DI 오버라이드로 런타임 설정을 교체할 수 있어야 한다
        app.dependency_overrides[get_settings] = lambda: _test_settings(default_operator_id="injected-op", trust_operator_header=True, openai_model="gpt-override")
        resp2 = client.get("/api/ag-ui/status", headers={"X-Operator-Id": "verified-proxy-op"})
        assert resp2.status_code == 200
        assert resp2.json()["operator"] == "verified-proxy-op"
        assert resp2.json()["model"] == "gpt-override"


def test_validation_error_returns_400_and_unhandled_error_hides_secrets():
    """요청 스키마 오류가 HTTP 400으로 변환되고 내부 예외 메시지가 노출되지 않는지 검증한다."""
    app = create_supervisor_app(_test_settings())

    class Payload(BaseModel):
        count: int

    probe = APIRouter()

    @probe.post("/probe/validate")
    async def probe_validate(body: Payload):
        return {"count": body.count}

    @probe.get("/probe/crash")
    async def probe_crash():
        raise RuntimeError("secret-token-leak-test")

    app.include_router(probe)

    with TestClient(app, raise_server_exceptions=False) as client:
        bad = client.post("/probe/validate", json={"count": "not-an-int"})
        assert bad.status_code == 400
        assert bad.json()["status"] == 400

        crash = client.get("/probe/crash")
        assert crash.status_code == 500
        assert "secret-token-leak-test" not in crash.text
