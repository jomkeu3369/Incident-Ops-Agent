"""Supervisor의 실서비스 경로가 실제 HTTP 위에서 A2A 스트리밍·원격 취소를 사용하는지 검증한다."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import socket
from typing import Any, AsyncIterator

import httpx
import pytest
import uvicorn
from ag_ui.core import RunAgentInput, UserMessage
from fastapi import FastAPI, Request
from pydantic import SecretStr

from common.a2a_utils import cancel_a2a_task, send_a2a_stream
from common.backend_client import BackendClient
from log_agent import routers as log_routers
from log_agent.main import create_log_agent_app
from log_agent.schemas import LogInvestigationResponse
from supervisor.checkpointer import RedisCheckpointSaver
from supervisor.cruds import SupervisorRedisStore
from supervisor.services import SupervisorService
from tests.test_phase7_protocol_resilience import _SharedInMemoryRedis, _settings

INCIDENT = "INC-2026-001"


class _Corpus:
    """청크 인출을 원하는 시점까지 붙잡아 두는 MCP 대역."""

    def __init__(self) -> None:
        self.calls = {"search": 0, "pull": 0}
        self.pull_started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "searchCorpusIndex":
            self.calls["search"] += 1
            chunk = "LOG-104" if args["corpusType"] == "LOG" else "RUNBOOK-01"
            return [{"chunkId": chunk, "corpusType": args["corpusType"], "title": "HikariPool timeout", "keywords": "HikariPool,502", "sequenceOrder": 1}]
        self.calls["pull"] += 1
        self.pull_started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return {"chunkId": args["chunkId"], "corpusType": "LOG", "title": args["chunkId"], "content": "HikariPool timeout"}


def _blocked_remediation_app() -> FastAPI:
    app = FastAPI()

    @app.post("/")
    async def rpc(request: Request) -> dict[str, Any]:
        body = await request.json()
        result = {"ok": True, "status": "BLOCKED", "message": "blocked", "toolCalls": [], "cachedToolResults": {}}
        return {"jsonrpc": "2.0", "id": body["id"], "result": {"message": {"messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": json.dumps(result)}]}}}

    return app


@asynccontextmanager
async def _serve(app: FastAPI) -> AsyncIterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        server.force_exit = True
        await asyncio.wait_for(task, 10)


@asynccontextmanager
async def _stack(
    corpus: _Corpus, *, http_timeout: float = 10.0, worker_delay: float = 0.0, received: asyncio.Event | None = None,
    investigator_token: str = "",
) -> AsyncIterator[tuple[Any, httpx.AsyncClient, str]]:
    tokens = {"internal_investigator_token": SecretStr(investigator_token)}
    log_app = create_log_agent_app(_settings(**tokens))
    if worker_delay:
        @log_app.middleware("http")
        async def slow_first_response(request: Request, call_next):
            if b"SendStreamingMessage" in await request.body():
                if received is not None:
                    received.set()
                await asyncio.sleep(worker_delay)
            return await call_next(request)

    async with _serve(log_app) as log_url, _serve(_blocked_remediation_app()) as rem_url:
        cfg = _settings(log_agent_a2a_url=log_url, remediation_agent_a2a_url=rem_url, http_timeout_seconds=http_timeout, **tokens)
        log_app.state.container.backend_client = BackendClient(cfg, log_app.state.container.http_client, mcp_caller=corpus, agent_role="investigator")

        def ledger(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"incident": {"incidentCode": INCIDENT, "serviceName": "payment-service", "status": "OPEN"}, "plans": [], "approvals": [], "executions": []})

        async with httpx.AsyncClient(timeout=http_timeout) as a2a_http, httpx.AsyncClient(transport=httpx.MockTransport(ledger)) as ledger_http:
            redis = _SharedInMemoryRedis()
            service = SupervisorService(cfg, a2a_http, BackendClient(cfg, ledger_http), SupervisorRedisStore(redis), checkpointer=RedisCheckpointSaver(redis))
            yield service, a2a_http, log_url


def _input(run_id: str, props: dict[str, Any], text: str | None = None) -> RunAgentInput:
    return RunAgentInput(
        thread_id="thread-a2a", run_id=run_id, state={}, tools=[], context=[],
        messages=[UserMessage(id="u", content=text)] if text else [],
        forwarded_props={"incidentCode": INCIDENT, "incidentStartedAt": "2026-11-18T14:04:00+09:00", **props},
    )


async def _consume(service: SupervisorService, run_input: RunAgentInput, sink: list[dict[str, Any]]) -> None:
    props = service.parse_forwarded_props(run_input)
    async for chunk in service.stream_ag_ui(run_input, props, "op", None, "text/event-stream"):
        sink.extend(json.loads(line[5:]) for line in chunk.splitlines() if line.startswith("data:"))


async def _until(predicate) -> None:
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


def _progress(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["value"] for e in events if e["type"] == "CUSTOM" and e["name"] == "a2a_progress"]


async def test_progress_reaches_ag_ui_before_investigation_finishes() -> None:
    corpus = _Corpus()
    events: list[dict[str, Any]] = []
    async with _stack(corpus) as (service, _, _):
        running = asyncio.create_task(_consume(service, _input("r1", {}, "14:28~14:50 5분 롤백 계획"), events))
        await _until(lambda: any(p["stage"] == "pulling_chunks" for p in _progress(events)))
        assert not running.done(), "진행 이벤트는 조사가 끝나기 전에 도착해야 한다"
        live = _progress(events)
        corpus.release.set()
        await asyncio.wait_for(running, 10)

    assert [p["stage"] for p in live] == ["searching_index", "pulling_chunks"]
    task_id = live[0]["taskId"]
    assert all(p["taskId"] == task_id and p["incidentCode"] == INCIDENT for p in live)
    snapshot = [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"][-1]
    assert snapshot["trace"]["a2aTaskIds"][0] == task_id, "트레이스에는 워커가 발급한 실제 작업 ID를 남긴다"


async def test_explicit_cancel_stops_the_remote_investigation() -> None:
    corpus = _Corpus()
    events: list[dict[str, Any]] = []
    async with _stack(corpus) as (service, _, _):
        running = asyncio.create_task(_consume(service, _input("r1", {}, "14:28~14:50 5분 롤백 계획"), events))
        await asyncio.wait_for(corpus.pull_started.wait(), 10)
        cancel_events: list[dict[str, Any]] = []
        await asyncio.wait_for(_consume(service, _input("r2", {"explicitCancel": True}), cancel_events), 10)
        await asyncio.wait_for(running, 10)
        await asyncio.wait_for(corpus.cancelled.wait(), 5)

    assert [e["snapshot"]["workflowStatus"] for e in cancel_events if e["type"] == "STATE_SNAPSHOT"] == ["CANCELLED"]


async def test_remote_cancel_is_sent_even_if_the_old_stream_disconnects_at_the_same_time() -> None:
    corpus = _Corpus()
    async with _stack(corpus) as (service, _, _):
        running = asyncio.create_task(_consume(service, _input("r1", {}, "14:28~14:50 5분 롤백 계획"), []))
        await asyncio.wait_for(corpus.pull_started.wait(), 10)
        cancelling = asyncio.create_task(_consume(service, _input("r2", {"explicitCancel": True}), []))
        await asyncio.sleep(0)  # 취소 신호가 전달된 직후
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.wait_for(cancelling, 10)
        await asyncio.wait_for(corpus.cancelled.wait(), 5)


async def test_disconnect_keeps_remote_work_and_resume_joins_it() -> None:
    corpus = _Corpus()
    events: list[dict[str, Any]] = []
    async with _stack(corpus) as (service, _, _):
        running = asyncio.create_task(_consume(service, _input("r1", {}, "14:28~14:50 5분 롤백 계획"), events))
        await asyncio.wait_for(corpus.pull_started.wait(), 10)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(0.2)
        assert not corpus.cancelled.is_set(), "연결 단절은 원격 작업을 취소하지 않는다"

        resumed: list[dict[str, Any]] = []
        joining = asyncio.create_task(_consume(service, _input("r2", {"resume": True}, "14:28~14:50 5분 롤백 계획"), resumed))
        await _until(lambda: _progress(resumed))
        corpus.release.set()
        await asyncio.wait_for(joining, 10)

    assert corpus.calls["search"] == 2, "LOG·RUNBOOK 색인 검색은 한 번씩만 실행된다"
    assert [e["snapshot"] for e in resumed if e["type"] == "STATE_SNAPSHOT"][-1]["trace"]["evidenceChunkIds"]


@pytest.mark.parametrize("resume_first", [False, True])
async def test_explicit_cancel_after_a_disconnect_stops_every_waiter_of_the_run(resume_first: bool) -> None:
    corpus = _Corpus()
    prompt = "14:28~14:50 5분 롤백 계획"
    async with _stack(corpus) as (service, _, _):
        running = asyncio.create_task(_consume(service, _input("r1", {}, prompt), []))
        await asyncio.wait_for(corpus.pull_started.wait(), 10)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        joining = None
        if resume_first:
            resumed: list[dict[str, Any]] = []
            joining = asyncio.create_task(_consume(service, _input("r2", {"resume": True, "resumeRunId": "r1"}, prompt), resumed))
            await _until(lambda: _progress(resumed))
        await asyncio.wait_for(_consume(service, _input("r3", {"explicitCancel": True}), []), 10)
        if joining is not None:
            await asyncio.wait_for(joining, 10)
        await asyncio.wait_for(corpus.cancelled.wait(), 5)


async def test_quiet_investigation_outlives_the_client_read_timeout() -> None:
    """워커의 SSE ping 간격(15초)보다 짧은 기본 읽기 타임아웃이 조사를 끊지 않아야 한다."""
    corpus = _Corpus()
    events: list[dict[str, Any]] = []
    async with _stack(corpus, http_timeout=0.4) as (service, _, _):
        running = asyncio.create_task(_consume(service, _input("r1", {}, "14:28~14:50 5분 롤백 계획"), events))
        await asyncio.wait_for(corpus.pull_started.wait(), 10)
        await asyncio.sleep(1.0)
        corpus.release.set()
        await asyncio.wait_for(running, 10)

    snapshot = [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"][-1]
    assert snapshot["trace"]["evidenceChunkIds"], "조사 결과가 계획 단계에 전달돼야 한다"


async def test_cancel_and_disconnect_before_the_first_worker_event_still_cancel_the_task() -> None:
    corpus = _Corpus()
    received = asyncio.Event()
    async with _stack(corpus, worker_delay=0.6, received=received) as (service, _, _):
        running = asyncio.create_task(_consume(service, _input("r1", {}, "14:28~14:50 5분 롤백 계획"), []))
        await asyncio.wait_for(received.wait(), 10)
        cancelling = asyncio.create_task(_consume(service, _input("r2", {"explicitCancel": True}), []))
        await asyncio.sleep(0.05)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.wait_for(cancelling, 10)
        await asyncio.sleep(1.0)

    assert not corpus.pull_started.is_set() or corpus.cancelled.is_set(), "취소 후에는 원격 인출이 계속되면 안 된다"


async def test_selector_stop_without_evidence_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def insufficient(backend, settings, req, **_: Any) -> LogInvestigationResponse:
        nonlocal calls
        calls += 1
        return LogInvestigationResponse(ok=True, incidentCode=req.incidentCode, stopReason="STOP_INSUFFICIENT")

    monkeypatch.setattr(log_routers, "run_dr_dci_investigation", insufficient)
    cfg = _settings()
    async with _stack(_Corpus()) as (_, http, log_url):
        for _ in range(2):
            await send_a2a_stream(
                http, base_url=log_url, internal_token=cfg.internal_service_token.get_secret_value(),
                payload={"incidentCode": INCIDENT, "keywords": ["HikariPool"]}, idempotency_key="retry",
            )

    assert calls == 2


async def test_unrequested_cancellation_marks_the_task_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def leaked_cancel(backend, settings, req, **_: Any) -> LogInvestigationResponse:
        raise asyncio.CancelledError

    monkeypatch.setattr(log_routers, "run_dr_dci_investigation", leaked_cancel)
    cfg = _settings()
    token = cfg.internal_service_token.get_secret_value()
    ids: list[str] = []
    async with _stack(_Corpus()) as (_, http, log_url):
        with pytest.raises(Exception):
            await send_a2a_stream(
                http, base_url=log_url, internal_token=token,
                payload={"incidentCode": INCIDENT, "keywords": ["HikariPool"]}, on_task_id=ids.append,
            )
        async with asyncio.timeout(5):
            while True:
                state = (await http.post(f"{log_url}/", headers={"A2A-Version": "1.0", "X-Internal-Token": token},
                                         json={"jsonrpc": "2.0", "id": "g", "method": "GetTask", "params": {"id": ids[0]}})).json()
                if state["result"]["status"]["state"] != "TASK_STATE_WORKING":
                    break
                await asyncio.sleep(0.05)

    assert state["result"]["status"]["state"] == "TASK_STATE_FAILED"


async def test_dedicated_investigator_token_is_the_only_log_agent_credential() -> None:
    corpus = _Corpus()
    events: list[dict[str, Any]] = []
    async with _stack(corpus, investigator_token="read-only-token") as (service, http, log_url):
        with pytest.raises(RuntimeError, match="401"):
            await send_a2a_stream(
                http, base_url=log_url, internal_token=_settings().internal_service_token.get_secret_value(),
                payload={"incidentCode": INCIDENT, "keywords": ["HikariPool"]},
            )
        running = asyncio.create_task(_consume(service, _input("r1", {}, "14:28~14:50 5분 롤백 계획"), events))
        await asyncio.wait_for(corpus.pull_started.wait(), 10)
        await asyncio.wait_for(_consume(service, _input("r2", {"explicitCancel": True}), []), 10)
        await asyncio.wait_for(running, 10)
        await asyncio.wait_for(corpus.cancelled.wait(), 5)

    assert _progress(events), "Supervisor는 전용 토큰으로 조사 워커를 호출한다"


async def test_cancel_one_of_two_shared_waiters_keeps_the_other_running() -> None:
    corpus = _Corpus()
    cfg = _settings()
    async with _stack(corpus) as (_, http, log_url):
        ids: list[str] = []

        def start() -> asyncio.Task:
            return asyncio.create_task(send_a2a_stream(
                http, base_url=log_url, internal_token=cfg.internal_service_token.get_secret_value(),
                payload={"incidentCode": INCIDENT, "keywords": ["HikariPool"]}, idempotency_key="shared", on_task_id=ids.append,
            ))

        first = start()
        await asyncio.wait_for(corpus.pull_started.wait(), 10)
        second = start()
        await _until(lambda: len(ids) == 2)
        await cancel_a2a_task(http, base_url=log_url, internal_token=cfg.internal_service_token.get_secret_value(), task_id=ids[0])
        cancelled = await asyncio.wait_for(first, 10)
        assert not corpus.cancelled.is_set()
        corpus.release.set()
        completed = await asyncio.wait_for(second, 10)

    assert cancelled.canceled is True
    assert completed.payload["ok"] is True
    assert corpus.calls["search"] == 2
