"""AG-UI 상태가 Spring 승인 원장과 어긋나지 않는지(취소·단절·재개) 검증하는 회귀 테스트."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from ag_ui.core import RunAgentInput, UserMessage
from fastapi import HTTPException

from common.backend_client import BackendApiError, BackendClient
from supervisor.checkpointer import RedisCheckpointSaver
from supervisor.cruds import SupervisorRedisStore
from supervisor.services import SupervisorService
from tests.test_phase7_protocol_resilience import _SharedInMemoryRedis, _parse_ag_ui_events, _settings

INCIDENT = "INC-2026-001"
COMMAND = "kubectl rollout undo deployment/payment-service"
PROMPT = "14:28~14:50 5분 롤백 계획 세워줘"


class _Gate:
    """원하는 지점에서 실행을 멈춰 두고 테스트가 풀어 주는 동기화 지점."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.armed = False

    async def hit(self) -> None:
        if self.armed:
            self.armed = False
            self.entered.set()
            await self.release.wait()


class _World:
    """Spring 승인 원장, A2A 워커, 공유 Redis를 흉내 내는 테스트 환경."""

    def __init__(self) -> None:
        self.cfg = _settings()
        self.redis = _SharedInMemoryRedis()
        self.status = "OPEN"
        self.plans: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []
        self.calls = {"log": 0, "plan": 0, "execute": 0, "complete": 0}
        self.ledger_down = False
        self.failing_reads = 0
        self.worker_cancelled = False
        self.completion_error: BackendApiError | None = None
        self.gates = {name: _Gate() for name in ("plan_worker", "plan_written", "approval_written", "execute_worker", "completion_written")}

    async def _rest(self, request: httpx.Request) -> httpx.Response:
        if self.ledger_down:
            return httpx.Response(503, json={"message": "down"})
        path = request.url.path
        if request.method == "GET":
            if self.failing_reads:
                self.failing_reads -= 1
                return httpx.Response(503, json={"message": "transient"})
            return httpx.Response(200, json={
                "incident": {"incidentCode": INCIDENT, "serviceName": "payment-service", "status": self.status},
                "plans": self.plans, "approvals": self.approvals, "executions": self.executions,
            })
        body = json.loads(request.content)
        if request.method == "POST" and path.endswith("/plans"):
            if any(p["planId"] == body["planId"] for p in self.plans):
                return httpx.Response(409, json={"message": "duplicate plan"})
            self.plans.append(body)
            await self.gates["plan_written"].hit()
            return httpx.Response(201, json=body)
        if request.method == "POST" and path.endswith("/approvals"):
            if any(a["decision"] in ("PENDING", "APPROVED") for a in self.approvals) and not self.executions:
                return httpx.Response(409, json={"message": "unfinished approval"})
            row = {**body, "decision": "PENDING", "actionCommand": COMMAND}
            self.approvals.append(row)
            await self.gates["approval_written"].hit()
            return httpx.Response(201, json=row)
        if request.method == "PATCH":
            row = next(a for a in self.approvals if path.endswith(a["approvalId"]))
            if row["decision"] != "PENDING":
                return httpx.Response(409, json={"message": "already decided"})
            row["decision"] = body["decision"]
            return httpx.Response(200, json=row)
        return httpx.Response(404)

    async def _mcp(self, tool: str, args: dict[str, Any]) -> Any:
        self.calls["complete"] += 1
        if self.completion_error is not None and not self.executions:
            error, self.completion_error = self.completion_error, None
            raise error
        if not any(e["approvalId"] == args["approvalId"] for e in self.executions):
            self.executions.append({"approvalId": args["approvalId"], "executionStatus": args["executionStatus"]})
            self.status = "RESOLVED"
        await self.gates["completion_written"].hit()
        return {"incidentStatus": self.status}

    async def _a2a(self, url: str, payload: dict[str, Any], context_id: str) -> dict[str, Any]:
        if url == self.cfg.log_agent_a2a_url:
            self.calls["log"] += 1
            return {"ok": True, "incidentCode": INCIDENT, "evidence": [{"chunkId": "LOG-104", "corpusType": "LOG", "title": "t", "summary": "timeout"}],
                    "evidenceChunkIds": ["LOG-104"], "runbookSummary": "rollback", "recommendedCommand": COMMAND, "remainingChunkIds": [], "pulledCount": 1}
        if payload.get("action") == "execute":
            self.calls["execute"] += 1
            await self.gates["execute_worker"].hit()
            return {"ok": True, "executionStatus": "SUCCESS"}
        self.calls["plan"] += 1
        try:
            await self.gates["plan_worker"].hit()
        except asyncio.CancelledError:
            self.worker_cancelled = True
            raise
        return {"ok": True, "status": "READY_FOR_APPROVAL", "message": "ready", "toolCalls": [], "cachedToolResults": {},
                "planDraft": {"planId": "PLAN-1", "approvalStepNo": 2, "verificationCriteria": "ok",
                              "steps": [{"stepNo": 1, "description": "check"}, {"stepNo": 2, "description": "rollback", "actionCommand": COMMAND}]}}

    def service(self, http: httpx.AsyncClient, store: SupervisorRedisStore | None = None) -> SupervisorService:
        return SupervisorService(self.cfg, http, BackendClient(self.cfg, http, mcp_caller=self._mcp), store or SupervisorRedisStore(self.redis),
                                 a2a_caller=self._a2a, checkpointer=RedisCheckpointSaver(self.redis))

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._rest))


def _input(run_id: str, props: dict[str, Any], text: str | None = None) -> RunAgentInput:
    return RunAgentInput(
        thread_id="thread-1", run_id=run_id, state={}, tools=[], context=[],
        messages=[UserMessage(id="u1", content=text)] if text else [],
        forwarded_props={"incidentCode": INCIDENT, "incidentStartedAt": "2026-11-18T14:04:00+09:00", **props},
    )


async def _run(service: SupervisorService, run_id: str, props: dict[str, Any], text: str | None = None) -> list[dict[str, Any]]:
    run_input = _input(run_id, props, text)
    parsed = service.parse_forwarded_props(run_input)
    decided = await service.prepare_approval_decision(parsed, "op", run_input.thread_id)
    return _parse_ag_ui_events([c async for c in service.stream_ag_ui(run_input, parsed, "op", decided, "text/event-stream")])


def _last_snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    return [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"][-1]


async def _stored_snapshot(world: _World) -> dict[str, Any]:
    return (await SupervisorRedisStore(world.redis).get_thread_memory("thread-1"))["snapshot"]


def _approve(approval_id: str) -> dict[str, Any]:
    return {"approvalDecision": {"approvalId": approval_id, "decision": "APPROVED"}}


async def _drop(task: asyncio.Task) -> None:
    """SSE 클라이언트 단절을 흉내 낸다."""
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), 5)


async def test_cancel_during_planning_stops_worker_without_ledger_writes() -> None:
    world = _World()
    gate = world.gates["plan_worker"]
    gate.armed = True
    async with world.http() as http:
        service = world.service(http)
        running = asyncio.create_task(_run(service, "r1", {}, PROMPT))
        await _wait(gate.entered)
        # 워커를 풀어 주지 않아도 취소가 끝나야 한다
        cancelled = await asyncio.wait_for(_run(service, "r2", {"explicitCancel": True}), 5)
        old = await asyncio.wait_for(running, 5)

    assert _last_snapshot(cancelled)["workflowStatus"] == "CANCELLED"
    assert not any(e["type"] == "STATE_SNAPSHOT" for e in old)
    assert world.plans == [] and world.approvals == []
    assert (await _stored_snapshot(world))["workflowStatus"] == "CANCELLED"


async def test_cancel_during_approval_write_keeps_committed_approval_visible() -> None:
    world = _World()
    gate = world.gates["approval_written"]
    gate.armed = True
    async with world.http() as http:
        service = world.service(http)
        running = asyncio.create_task(_run(service, "r1", {}, PROMPT))
        await _wait(gate.entered)
        cancelling = asyncio.create_task(_run(service, "r2", {"explicitCancel": True}))
        await asyncio.sleep(0.05)
        assert not cancelling.done(), "취소는 시작된 원장 쓰기가 끝난 뒤에 상태를 확정해야 한다"
        gate.release.set()
        events = await asyncio.wait_for(cancelling, 5)
        await asyncio.wait_for(running, 5)

    snapshot = _last_snapshot(events)
    assert snapshot["workflowStatus"] == "AWAITING_APPROVAL"
    assert snapshot["approval"]["approvalId"] == world.approvals[0]["approvalId"]
    assert snapshot["plan"]["planId"] == "PLAN-1"


async def test_cancel_during_execution_leaves_approval_resumable() -> None:
    world = _World()
    gate = world.gates["execute_worker"]
    async with world.http() as http:
        service = world.service(http)
        approval_id = _last_snapshot(await _run(service, "r1", {}, PROMPT))["approval"]["approvalId"]
        gate.armed = True
        running = asyncio.create_task(_run(service, "r2", _approve(approval_id)))
        await _wait(gate.entered)
        events = await asyncio.wait_for(_run(service, "r3", {"explicitCancel": True}), 5)
        await asyncio.wait_for(running, 5)

        assert _last_snapshot(events)["workflowStatus"] == "AWAITING_EXECUTION_RESULT"
        assert world.calls["complete"] == 0 and world.status == "OPEN"
        resumed = await _run(service, "r4", _approve(approval_id))

    assert _last_snapshot(resumed)["workflowStatus"] == "RESOLVED"
    assert len(world.executions) == 1


async def test_cancel_after_completion_write_cannot_overwrite_resolved() -> None:
    world = _World()
    gate = world.gates["completion_written"]
    async with world.http() as http:
        service = world.service(http)
        approval_id = _last_snapshot(await _run(service, "r1", {}, PROMPT))["approval"]["approvalId"]
        gate.armed = True
        running = asyncio.create_task(_run(service, "r2", _approve(approval_id)))
        await _wait(gate.entered)
        cancelling = asyncio.create_task(_run(service, "r3", {"explicitCancel": True}))
        await asyncio.sleep(0.05)
        gate.release.set()
        await asyncio.wait_for(running, 5)
        events = await asyncio.wait_for(cancelling, 5)

    assert _last_snapshot(events)["workflowStatus"] == "RESOLVED"
    assert (await _stored_snapshot(world))["workflowStatus"] == "RESOLVED"


async def test_cancel_for_another_incident_does_not_touch_the_active_run() -> None:
    world = _World()
    gate = world.gates["plan_worker"]
    gate.armed = True
    async with world.http() as http:
        service = world.service(http)
        running = asyncio.create_task(_run(service, "r1", {}, PROMPT))
        await _wait(gate.entered)
        events = await _run(service, "r2", {"explicitCancel": True, "incidentCode": "INC-OTHER"})
        gate.release.set()
        finished = await asyncio.wait_for(running, 5)

    assert [e["code"] for e in events if e["type"] == "RUN_ERROR"] == ["CANCEL_SCOPE_MISMATCH"]
    assert _last_snapshot(finished)["workflowStatus"] == "AWAITING_APPROVAL"


async def test_cancel_with_unreadable_ledger_does_not_claim_cancelled() -> None:
    world = _World()
    async with world.http() as http:
        service = world.service(http)
        await _run(service, "r1", {}, PROMPT)
        world.ledger_down = True
        events = await _run(service, "r2", {"explicitCancel": True})

    assert [e["code"] for e in events if e["type"] == "RUN_ERROR"] == ["LEDGER_UNAVAILABLE"]
    assert (await _stored_snapshot(world))["workflowStatus"] == "AWAITING_APPROVAL"


@pytest.mark.parametrize("reentry", ["restore", "same_approval"])
async def test_disconnect_after_completion_commit_recovers_from_ledger(reentry: str) -> None:
    world = _World()
    gate = world.gates["completion_written"]
    async with world.http() as http:
        service = world.service(http)
        approval_id = _last_snapshot(await _run(service, "r1", {}, PROMPT))["approval"]["approvalId"]
        gate.armed = True
        running = asyncio.create_task(_run(service, "r2", _approve(approval_id)))
        await _wait(gate.entered)
        await _drop(running)
        assert (await _stored_snapshot(world))["workflowStatus"] == "AWAITING_EXECUTION_RESULT"

        restarted = world.service(http)
        props = {"restore": True} if reentry == "restore" else _approve(approval_id)
        events = await _run(restarted, "r3", props)

    assert _last_snapshot(events)["workflowStatus"] == "RESOLVED"
    assert (await _stored_snapshot(world))["workflowStatus"] == "RESOLVED"
    assert world.calls["execute"] == 1 and world.calls["complete"] == 1
    assert len(world.executions) == 1


async def test_repeating_a_delivered_approval_still_returns_409() -> None:
    world = _World()
    async with world.http() as http:
        service = world.service(http)
        approval_id = _last_snapshot(await _run(service, "r1", {}, PROMPT))["approval"]["approvalId"]
        await _run(service, "r2", _approve(approval_id))
        with pytest.raises(HTTPException) as error:
            await _run(service, "r3", _approve(approval_id))

    assert error.value.status_code == 409
    assert world.calls["execute"] == 1


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(502, "AWAITING_EXECUTION_RESULT"), (409, "EXECUTION_FAILED")],
)
async def test_failed_completion_is_classified_by_ledger_and_status(status_code: int, expected: str) -> None:
    world = _World()
    async with world.http() as http:
        service = world.service(http)
        approval_id = _last_snapshot(await _run(service, "r1", {}, PROMPT))["approval"]["approvalId"]
        world.completion_error = BackendApiError(status_code, "boom")
        events = await _run(service, "r2", _approve(approval_id))

    assert _last_snapshot(events)["workflowStatus"] == expected


async def test_resume_of_finished_run_replays_without_rerunning_workers() -> None:
    world = _World()
    async with world.http() as http:
        service = world.service(http)
        first = _last_snapshot(await _run(service, "r1", {}, PROMPT))
        replayed = await _run(service, "r2", {"resume": True}, PROMPT)
        memory = await SupervisorRedisStore(world.redis).get_thread_memory("thread-1")

    assert _last_snapshot(replayed)["approval"] == first["approval"]
    assert _last_snapshot(replayed)["workflowStatus"] == "AWAITING_APPROVAL"
    assert world.calls["log"] == 1 and world.calls["plan"] == 1
    assert [m["role"] for m in memory["messages"]] == ["user", "assistant"]


@pytest.mark.parametrize("gate_name", ["plan_written", "approval_written"])
async def test_resume_after_disconnect_during_ledger_registration_reuses_own_rows(gate_name: str) -> None:
    world = _World()
    gate = world.gates[gate_name]
    gate.armed = True
    async with world.http() as http:
        service = world.service(http)
        running = asyncio.create_task(_run(service, "r1", {}, PROMPT))
        await _wait(gate.entered)
        running.cancel()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await running

        events = await _run(world.service(http), "r2", {"resume": True}, PROMPT)

    snapshot = _last_snapshot(events)
    assert snapshot["workflowStatus"] == "AWAITING_APPROVAL"
    assert len(world.plans) == 1 and len(world.approvals) == 1
    assert snapshot["approval"]["approvalId"] == world.approvals[0]["approvalId"]
    assert world.calls["log"] == 1 and world.calls["plan"] == 1


@pytest.mark.parametrize(
    "props",
    [
        {"restore": True, "explicitCancel": True},
        {"restore": True, "resume": True},
        {"resume": True, "explicitCancel": True},
        {"resume": True, **_approve("APR-1")},
    ],
)
async def test_contradictory_request_flags_are_rejected(props: dict[str, Any]) -> None:
    world = _World()
    async with world.http() as http:
        with pytest.raises(HTTPException) as error:
            world.service(http).parse_forwarded_props(_input("r1", props))

    assert error.value.status_code == 400


async def test_every_transition_advances_event_sequence_above_client_cursor() -> None:
    world = _World()
    async with world.http() as http:
        service = world.service(http)
        planned = _last_snapshot(await _run(service, "r1", {"lastEventSeq": 40}, PROMPT))
        approved = await _run(service, "r2", _approve(planned["approval"]["approvalId"]))

    sequences = [e["snapshot"]["eventSeq"] for e in approved if e["type"] == "STATE_SNAPSHOT"]
    assert planned["eventSeq"] == 41
    assert sequences == [42, 43]
    sync = [e["value"] for e in approved if e["type"] == "CUSTOM" and e["name"] == "checkpoint_sync"]
    assert sync[-1]["eventSeq"] == 43 and sync[-1]["incidentCode"] == INCIDENT


async def test_restore_during_active_run_waits_and_commits_a_new_sequence() -> None:
    world = _World()
    gate = world.gates["completion_written"]
    async with world.http() as http:
        service = world.service(http)
        approval_id = _last_snapshot(await _run(service, "r1", {}, PROMPT))["approval"]["approvalId"]
        gate.armed = True
        running = asyncio.create_task(_run(service, "r2", _approve(approval_id)))
        await _wait(gate.entered)
        waiting = await _stored_snapshot(world)
        restoring = asyncio.create_task(_run(service, "r3", {"restore": True}))
        await asyncio.sleep(0.05)
        assert not restoring.done(), "저장되지 않은 상태를 먼저 보내면 안 된다"
        await _drop(running)
        restored = _last_snapshot(await asyncio.wait_for(restoring, 5))

    assert restored["workflowStatus"] == "RESOLVED"
    assert restored["eventSeq"] > waiting["eventSeq"]
    assert await _stored_snapshot(world) == restored


async def test_reused_approval_reports_the_ledger_status_after_a_transient_read_failure() -> None:
    world = _World()
    gate = world.gates["approval_written"]
    gate.armed = True
    async with world.http() as http:
        service = world.service(http)
        running = asyncio.create_task(_run(service, "r1", {}, PROMPT))
        await _wait(gate.entered)
        running.cancel()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        world.approvals[0]["decision"] = "APPROVED"
        world.failing_reads = 1
        snapshot = _last_snapshot(await _run(world.service(http), "r2", {"resume": True}, PROMPT))

    assert snapshot["approval"]["decision"] == "APPROVED"
    assert snapshot["workflowStatus"] == "AWAITING_EXECUTION_RESULT"


async def test_closing_the_stream_cleans_up_before_releasing_the_thread_lock() -> None:
    world = _World()
    gate = world.gates["plan_worker"]
    gate.armed = True
    async with world.http() as http:
        store = SupervisorRedisStore(world.redis)
        service = world.service(http, store)
        run_input = _input("r1", {}, PROMPT)
        stream = service.stream_ag_ui(run_input, service.parse_forwarded_props(run_input), "op", None, "text/event-stream")
        await anext(stream)  # RUN_STARTED
        await anext(stream)  # 첫 단계 이벤트: 위임 스트림이 yield에서 멈춘 상태
        await _wait(gate.entered)
        await stream.aclose()

        assert world.worker_cancelled, "락을 놓기 전에 그래프 태스크가 정리돼야 한다"
        assert store.active_runs("thread-1") == []
        assert store._thread_locks == {}


def _checkpoint(index: int) -> dict[str, Any]:
    return {"id": f"cp-{index:03d}", "v": 1, "ts": "2026-11-26T00:00:00Z", "channel_values": {}, "channel_versions": {}, "versions_seen": {}}


def _thread(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def test_checkpoint_expired_in_redis_is_not_revived_from_local_mirror() -> None:
    redis = _SharedInMemoryRedis()
    saver = RedisCheckpointSaver(redis)
    await saver.aput(_thread("t"), _checkpoint(1), {}, {})
    redis.kv.clear()
    redis.lists.clear()

    assert await saver.aget_tuple(_thread("t")) is None


async def test_local_checkpoint_mirror_is_bounded() -> None:
    saver = RedisCheckpointSaver(_SharedInMemoryRedis(), max_mem_threads=3, max_mem_checkpoints_per_thread=2)
    for thread in range(10):
        for index in range(5):
            await saver.aput(_thread(f"t{thread}"), _checkpoint(index), {}, {})

    assert len(saver._mem_threads) == 3
    assert len(saver._mem_checkpoints) == 6
