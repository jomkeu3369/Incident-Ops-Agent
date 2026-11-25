from __future__ import annotations

from typing import Any
import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from common.a2a_utils import send_a2a_json
from common.backend_client import BackendClient
from common.config import Settings
from common.decisions import RuleBasedJudge
from common.di import build_container
from common.observability import (
    LANGSMITH_TRACE_HEADER,
    REDACTED_VALUE,
    build_langsmith_client,
    end_span,
    extract_incoming_trace_headers,
    extract_trace_headers,
    langsmith_tracing_scope,
    redact_sensitive_dict,
    redact_sensitive_value,
    traced_span,
)
from eval.dataset import IncidentEvalCase
from eval.harness import evaluate_judge
from log_agent.main import create_log_agent_app
from log_agent.schemas import LogInvestigationRequest
from log_agent.services import run_dr_dci_investigation
from supervisor.main import create_supervisor_app


class _RecordingLangSmithClient:
    """외부 네트워크 호출 없이 LangSmith RunTree가 전송하는 create_run/update_run 이벤트를 기록하는 모의 클라이언트."""

    def __init__(self, *, fail_on_create: bool = False) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.flushed: bool = False
        self._fail_on_create = fail_on_create

    def create_run(self, **kwargs: Any) -> None:
        if self._fail_on_create:
            raise RuntimeError("simulated langsmith network outage")
        self.created.append(kwargs)

    def update_run(self, run_id: Any, **kwargs: Any) -> None:
        self.updated.append({"run_id": str(run_id), **kwargs})

    def flush(self) -> None:
        self.flushed = True


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "internal_service_token": SecretStr("test-internal-token-1234"),
        "langsmith_tracing": False,
        "langsmith_api_key": SecretStr(""),
        "langsmith_project": "incident-ops-test",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_langsmith_opt_in_and_placeholder_key_guard():
    """LANGSMITH_TRACING=true와 유효한 키가 모두 있을 때만 활성화되며 기본/플레이스홀더는 비활성이다."""
    assert _settings().langsmith_enabled is False
    assert build_langsmith_client(_settings()) is None

    # 플래그만 true이고 키가 비어 있거나 플레이스홀더면 비활성
    assert _settings(langsmith_tracing=True, langsmith_api_key=SecretStr("")).langsmith_enabled is False
    assert _settings(langsmith_tracing=True, langsmith_api_key=SecretStr("your-langsmith-api-key-here")).langsmith_enabled is False
    assert build_langsmith_client(_settings(langsmith_tracing=True, langsmith_api_key=SecretStr("your-langsmith-api-key-here"))) is None

    # 둘 다 유효하면 활성화
    enabled_cfg = _settings(langsmith_tracing=True, langsmith_api_key=SecretStr("lsv2_pt_test_key_1234567890"))
    assert enabled_cfg.langsmith_enabled is True
    client = build_langsmith_client(enabled_cfg)
    assert client is not None


def test_secret_redaction_masks_nested_keys_secret_str_and_token_patterns():
    """민감 키, SecretStr, 헤더, OpenAI/LangSmith/GitHub/Bearer 토큰 문자열이 재귀적으로 마스킹된다."""
    raw = {
        "incidentCode": "INC-2026-001",
        "X-Internal-Token": "super-secret-internal-token",
        "headers": {"Authorization": "Bearer abcdef1234567890", "Accept": "application/json"},
        "nested": {
            "openai_api_key": SecretStr("sk-proj-secret1234567890"),
            "notes": "leaked sk-proj-1234567890abcdef and lsv2_pt_1234567890abcdef and ghp_1234567890abcdef1234",
        },
        "items": [{"password": "pw", "safe": "ok"}],
    }
    redacted = redact_sensitive_dict(raw)
    assert redacted["incidentCode"] == "INC-2026-001"
    assert redacted["X-Internal-Token"] == REDACTED_VALUE
    assert redacted["headers"]["Authorization"] == REDACTED_VALUE
    assert redacted["headers"]["Accept"] == "application/json"
    assert redacted["nested"]["openai_api_key"] == REDACTED_VALUE
    assert "sk-proj-" not in redacted["nested"]["notes"]
    assert "lsv2_pt_" not in redacted["nested"]["notes"]
    assert "ghp_" not in redacted["nested"]["notes"]
    assert redacted["items"][0]["password"] == REDACTED_VALUE
    assert redacted["items"][0]["safe"] == "ok"
    assert redact_sensitive_value("Bearer my-secret-jwt-token") == REDACTED_VALUE


async def test_traced_span_noop_when_disabled_and_records_sanitized_spans_when_enabled():
    """비활성 시 스팬이 전혀 생성되지 않으며, 활성 시 시크릿이 마스킹된 스팬이 기록된다."""
    recorder = _RecordingLangSmithClient()

    # 1) 비활성 시 외부/모의 클라이언트로 기록되지 않음
    with langsmith_tracing_scope(_settings(langsmith_tracing=False)):
        async with traced_span("disabled.span", inputs={"token": "secret"}) as span:
            assert span is None
            assert extract_trace_headers() == {}

    # 2) 활성 시 MCP·DR-DCI 스팬이 기록되고 민감 입력이 마스킹됨
    async def fake_mcp(tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name == "searchCorpusIndex":
            return [{"chunkId": "LOG-104", "corpusType": "LOG", "title": "HikariPool timeout", "keywords": "HikariPool,502", "sequenceOrder": 1}]
        if tool_name == "pullCorpusChunk":
            return {"chunkId": "LOG-104", "corpusType": "LOG", "title": "HikariPool timeout", "content": "HikariPool timeout 502"}
        return {}

    cfg = _settings(langsmith_tracing=True, langsmith_api_key=SecretStr("lsv2_pt_valid_key_1234567890"))
    async with httpx.AsyncClient() as http_client:
        backend = BackendClient(cfg, http_client, mcp_caller=fake_mcp)
        with langsmith_tracing_scope(cfg, client=recorder, tags=["test-suite"], metadata={"api_key": "should-redact"}):
            res = await run_dr_dci_investigation(
                backend,
                cfg,
                LogInvestigationRequest(incidentCode="INC-2026-001", keywords=["HikariPool"]),
            )
            assert res.ok is True

    created_names = [item.get("name") for item in recorder.created]
    assert "log_agent.dr_dci_investigate" in created_names
    assert "mcp.searchCorpusIndex" in created_names
    assert "mcp.pullCorpusChunk" in created_names
    for item in recorder.created:
        meta = (item.get("extra") or {}).get("metadata") or {}
        if "api_key" in meta:
            assert meta["api_key"] == REDACTED_VALUE


async def test_a2a_distributed_trace_header_propagation_and_receiver_extraction():
    """send_a2a_json이 langsmith-trace 헤더를 전파하고, A2A 수신 미들웨어가 부모 헤더를 안전하게 추출한다."""
    recorder = _RecordingLangSmithClient()
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {
                    "message": {
                        "messageId": "m-1",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": '{"ok": true}'}],
                    }
                },
            },
        )

    cfg = _settings(langsmith_tracing=True, langsmith_api_key=SecretStr("lsv2_pt_valid_key_1234567890"))
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        with langsmith_tracing_scope(cfg, client=recorder, tags=["ag-ui"]):
            async with traced_span("supervisor.root", run_type="chain") as root_span:
                assert root_span is not None
                out = await send_a2a_json(
                    http_client,
                    base_url="http://log-agent:8001",
                    internal_token="secret-internal-tok",
                    payload={"incidentCode": "INC-2026-001"},
                    context_id="thread-1",
                )
                assert out == {"ok": True}
                end_span(root_span, outputs={"done": True})

    assert LANGSMITH_TRACE_HEADER in captured_headers
    incoming = extract_incoming_trace_headers(captured_headers)
    assert incoming is not None
    assert incoming[LANGSMITH_TRACE_HEADER] == captured_headers[LANGSMITH_TRACE_HEADER]
    # 수신 헤더 추출에 내부 인증 토큰은 절대 포함되지 않아야 함
    assert "x-internal-token" not in incoming


async def test_tracing_failure_is_fail_safe_and_eval_harness_accepts_client():
    """LangSmith 전송이 실패해도 에이전트 및 평가 하네스 본 실행은 예외 없이 완료된다."""
    failing_client = _RecordingLangSmithClient(fail_on_create=True)
    cfg = _settings(langsmith_tracing=True, langsmith_api_key=SecretStr("lsv2_pt_valid_key_1234567890"))

    cases = [
        IncidentEvalCase(
            case_id="LS-1",
            category="db_connection_pool",
            service_name="payment-service",
            expected_verdict="NEED_MORE_EVIDENCE",
            proposed_action="Rollback payment-service to v41",
            runbook="rollback on post-deploy 502 spike",
            evidence="[LOG-1] HikariPool timeout, metrics missing",
            rationale="telemetry missing",
        )
    ]
    report = await evaluate_judge(
        RuleBasedJudge(),
        cases,
        judge_name="rule_based",
        settings=cfg,
        langsmith_client=failing_client,
    )
    assert report.total_cases == 1
    assert report.correct_cases == 1


def test_supervisor_status_reports_langsmith_state_and_container_flushes():
    """supervisor /api/ag-ui/status가 langsmith 활성 여부를 노출하고 컨테이너 종료 시 flush를 호출한다."""
    app = create_supervisor_app(_settings(langsmith_tracing=False))
    with TestClient(app) as client:
        resp = client.get("/api/ag-ui/status")
        assert resp.status_code == 200
        assert resp.json()["langsmith"] == "disabled"

    log_app = create_log_agent_app(_settings(langsmith_tracing=False))
    with TestClient(log_app) as client:
        assert client.get("/health").status_code == 200

    # 컨테이너 aclose 시 langsmith_client.flush()가 호출되어야 함
    import asyncio

    container = build_container(_settings(langsmith_tracing=False))
    recorder = _RecordingLangSmithClient()
    container.langsmith_client = recorder
    asyncio.run(container.aclose())
    assert recorder.flushed is True


async def test_a2a_middleware_links_worker_spans_to_incoming_parent_trace_id():
    """A2A 수신 미들웨어가 langsmith-trace 헤더를 받아 워커 스팬을 동일한 분산 trace_id로 연결한다."""
    sender_recorder = _RecordingLangSmithClient()
    worker_recorder = _RecordingLangSmithClient()
    cfg = _settings(langsmith_tracing=True, langsmith_api_key=SecretStr("lsv2_pt_valid_key_1234567890"))

    async def fake_mcp(tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name == "searchCorpusIndex":
            return [{"chunkId": "LOG-104", "corpusType": "LOG", "title": "HikariPool timeout", "keywords": "HikariPool", "sequenceOrder": 1}]
        if tool_name == "pullCorpusChunk":
            return {"chunkId": "LOG-104", "corpusType": "LOG", "title": "HikariPool timeout", "content": "HikariPool timeout 502"}
        return {}

    log_app = create_log_agent_app(cfg)
    with TestClient(log_app) as test_client:
        log_app.state.container.langsmith_client = worker_recorder
        log_app.state.container.backend_client = BackendClient(cfg, log_app.state.container.http_client, mcp_caller=fake_mcp)

        def forward_to_log_app(request: httpx.Request) -> httpx.Response:
            r = test_client.post(request.url.path, content=request.content, headers=dict(request.headers))
            return httpx.Response(r.status_code, content=r.content, headers=dict(r.headers))

        async with httpx.AsyncClient(transport=httpx.MockTransport(forward_to_log_app)) as http_client:
            with langsmith_tracing_scope(cfg, client=sender_recorder, tags=["ag-ui"]):
                async with traced_span("supervisor.root", run_type="chain") as root_span:
                    assert root_span is not None
                    root_trace_id = str(root_span.trace_id)
                    result = await send_a2a_json(
                        http_client,
                        base_url="http://log-agent:8001",
                        internal_token=cfg.internal_service_token.get_secret_value(),
                        payload={"incidentCode": "INC-2026-001", "keywords": ["HikariPool"]},
                        context_id="thread-dist-1",
                    )
                    assert result["ok"] is True

    worker_trace_ids = {str(item.get("trace_id")) for item in worker_recorder.created}
    worker_names = [item.get("name") for item in worker_recorder.created]
    assert "log_agent.dr_dci_investigate" in worker_names
    assert "mcp.searchCorpusIndex" in worker_names
    assert worker_trace_ids
    assert root_trace_id in worker_trace_ids


async def test_traced_span_sanitizes_raised_exception_secrets_and_preserves_token_usage_counts():
    """예외 메시지의 시크릿은 마스킹되어 LangSmith로 전송되고, 숫자형 input_tokens/output_tokens는 보존된다."""
    recorder = _RecordingLangSmithClient()
    cfg = _settings(langsmith_tracing=True, langsmith_api_key=SecretStr("lsv2_pt_valid_key_1234567890"))

    # 1) 숫자형 토큰 사용량 카운터 보존 + 문자열/인증 토큰 마스킹 검증
    sanitized = redact_sensitive_dict({
        "input_tokens": 412,
        "output_tokens": 88,
        "total_tokens": 500,
        "access_token": "secret-access-token",
        "internal_service_token": "secret-internal-token",
    })
    assert sanitized["input_tokens"] == 412
    assert sanitized["output_tokens"] == 88
    assert sanitized["total_tokens"] == 500
    assert sanitized["access_token"] == REDACTED_VALUE
    assert sanitized["internal_service_token"] == REDACTED_VALUE

    # 2) 스팬 내부에서 시크릿을 포함한 예외가 발생해도 LangSmith error에는 원문이 노출되지 않아야 함
    import pytest

    with langsmith_tracing_scope(cfg, client=recorder, tags=["security"]):
        with pytest.raises(RuntimeError, match="sk-proj-secret1234567890"):
            async with traced_span("mcp.secret_error", run_type="tool"):
                raise RuntimeError("upstream failed with sk-proj-secret1234567890 and Bearer abcdef1234567890")

    recorded_errors = [str(u.get("error") or "") for u in recorder.updated if u.get("error")]
    assert recorded_errors, "expected sanitized error to be recorded on update_run"
    for err in recorded_errors:
        assert "sk-proj-secret1234567890" not in err
        assert "abcdef1234567890" not in err
        assert REDACTED_VALUE in err
