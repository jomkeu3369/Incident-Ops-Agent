from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from ag_ui.core import RunAgentInput
from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr

from common.backend_client import BackendClient
from common.config import Settings
from log_agent.main import create_log_agent_app
from remediation_agent.main import create_remediation_agent_app
from supervisor.cruds import SupervisorRedisStore
from supervisor.schemas import ManagedPlanCreate
from supervisor.services import SupervisorService


class _FakePipeline:
    def __init__(self, redis: "_FullFakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> "_FakePipeline":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def llen(self, key: str) -> "_FakePipeline":
        self._ops.append(("llen", (key,)))
        return self

    def lindex(self, key: str, idx: int) -> "_FakePipeline":
        self._ops.append(("lindex", (key, idx)))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, args in self._ops:
            fn = getattr(self._redis, op)
            results.append(await fn(*args))
        return results


class _FullFakeRedis:
    def __init__(self) -> None:
        self.up = True
        self.ping_calls = 0
        self.lrange_calls = 0
        self.fail_rpush_on_call: int | None = None
        self.rpush_calls = 0
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}

    async def ping(self) -> bool:
        self.ping_calls += 1
        if not self.up:
            raise ConnectionError("redis down")
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
        self.rpush_calls += 1
        if self.fail_rpush_on_call is not None and self.rpush_calls == self.fail_rpush_on_call:
            raise ConnectionError("transient rpush error")
        self.lists.setdefault(k, []).append(v)

    async def lrange(self, k: str, s: int, e: int) -> list[str]:
        self.lrange_calls += 1
        return list(self.lists.get(k, []))

    async def llen(self, k: str) -> int:
        return len(self.lists.get(k, []))

    async def lindex(self, k: str, idx: int) -> str | None:
        rows = self.lists.get(k, [])
        if not rows:
            return None
        try:
            return rows[idx]
        except IndexError:
            return None

    def pipeline(self, transaction: bool = False) -> _FakePipeline:
        return _FakePipeline(self)


async def test_redis_store_caches_ping_and_avoids_memory_leak_when_healthy() -> None:
    """정상 Redis에서 PING을 매 호출마다 반복하지 않고 인메모리 폴백 맵을 누적하지 않는지 검증한다."""
    redis = _FullFakeRedis()
    store = SupervisorRedisStore(redis, health_check_interval=60.0)

    for idx in range(5):
        await store.save_thread_memory(f"t-{idx}", {"threadId": f"t-{idx}", "messages": [{"id": str(idx)}]})
        await store.get_thread_memory(f"t-{idx}")
        await store.append_session_log(f"s-{idx}", {"sessionId": f"s-{idx}", "timestamp": f"2026-11-23T00:0{idx}:00Z"})
        await store.create_plan(
            ManagedPlanCreate(
                planId=f"PLAN-{idx}",
                incidentCode="INC-1",
                title=f"Plan {idx}",
                planDate="2026-11-23",
                startTime=f"0{idx}:00",
                endTime=f"0{idx}:10",
            )
        )

    assert redis.ping_calls == 1
    assert store._mem_threads == {}
    assert store._mem_logs == {}
    assert store._mem_plans == {}


async def test_redis_store_list_sessions_uses_llen_and_lindex_without_lrange() -> None:
    """list_sessions가 전체 LRANGE 대신 LLEN/LINDEX 파이프라인으로 요약 정보를 읽는지 검증한다."""
    redis = _FullFakeRedis()
    store = SupervisorRedisStore(redis, health_check_interval=60.0)

    await store.append_session_log("sess-a", {"sessionId": "sess-a", "timestamp": "2026-11-23T01:00:00Z", "eventType": "step_started"})
    await store.append_session_log("sess-a", {"sessionId": "sess-a", "timestamp": "2026-11-23T01:02:00Z", "eventType": "step_finished"})
    await store.append_session_log("sess-b", {"sessionId": "sess-b", "timestamp": "2026-11-23T02:00:00Z", "eventType": "step_started"})

    summaries = await store.list_sessions()
    assert redis.lrange_calls == 0
    assert [(s.sessionId, s.eventCount, s.startedAt, s.lastEventAt) for s in summaries] == [
        ("sess-a", 2, "2026-11-23T01:00:00Z", "2026-11-23T01:02:00Z"),
        ("sess-b", 1, "2026-11-23T02:00:00Z", "2026-11-23T02:00:00Z"),
    ]


async def test_redis_store_reconcile_prevents_duplicate_events_on_partial_failure() -> None:
    """복구 동기화 중 부분 rpush 실패 후 재시도해도 이미 반영된 이벤트가 중복 적재되지 않는지 검증한다."""
    redis = _FullFakeRedis()
    redis.up = False
    store = SupervisorRedisStore(redis, health_check_interval=60.0)

    for idx in range(3):
        await store.append_session_log(
            "sess-outage",
            {"sessionId": "sess-outage", "timestamp": f"2026-11-23T03:0{idx}:00Z", "seq": idx},
        )

    redis.up = True
    redis.fail_rpush_on_call = 2
    store._redis_retry_after = 0.0
    # 2번째 rpush 실패로 폴백 조회가 반환되며, 첫 1건은 재큐잉에서 제거된 상태
    _ = await store.get_session_logs("sess-outage")

    redis.fail_rpush_on_call = None
    store._redis_retry_after = 0.0
    recovered = await store.get_session_logs("sess-outage")

    assert [ev["seq"] for ev in recovered] == [0, 1, 2]
    assert "sess-outage" in redis.sets["exec_log:index"]
    assert store._pending_log_events == {}
    assert store._mem_logs == {}


async def test_supervisor_thread_lock_serializes_concurrent_runs_without_lost_updates() -> None:
    """동일 threadId 동시 요청 시 thread_lock으로 직렬화되어 두 메시지가 모두 보존되고 락이 정리되는지 검증한다."""
    settings = Settings(_env_file=None, internal_service_token=SecretStr("sec"))
    store = SupervisorRedisStore(None)
    backend = BackendClient(settings, httpx.AsyncClient())
    entered_first = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def gated_a2a(url: str, payload: dict[str, Any], context_id: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered_first.set()
            await release_first.wait()
        return {"ok": False, "status": "BLOCKED", "message": f"reply-{calls}", "toolCalls": [], "cachedToolResults": {}}

    svc = SupervisorService(settings, httpx.AsyncClient(), backend, store, a2a_caller=gated_a2a)

    async def drain(run_id: str, content: str) -> None:
        req = RunAgentInput(
            thread_id="t-concurrent",
            run_id=run_id,
            state={},
            messages=[{"id": f"m-{run_id}", "role": "user", "content": content}],
            tools=[],
            context=[],
            forwarded_props={"incidentCode": "INC-1", "incidentStartedAt": "2026-11-18T14:04:00+09:00"},
        )
        props = svc.parse_forwarded_props(req)
        async for _ in svc.stream_ag_ui(req, props, "op", None, None):
            pass

    t1 = asyncio.create_task(drain("r1", "첫 번째 요청 502 계획"))
    await entered_first.wait()
    t2 = asyncio.create_task(drain("r2", "두 번째 요청 502 계획"))
    await asyncio.sleep(0.03)
    assert calls == 1
    release_first.set()
    await asyncio.gather(t1, t2)

    mem = await store.get_thread_memory("t-concurrent")
    user_contents = [m["content"] for m in mem["messages"] if m["role"] == "user"]
    assert user_contents == ["첫 번째 요청 502 계획", "두 번째 요청 502 계획"]
    assert store._thread_locks == {}


async def test_supervisor_tool_cache_expires_after_ttl() -> None:
    """cachedToolResults가 TTL 이내에는 재사용되고 TTL 만료 후에는 빈 캐시로 재조회되는지 검증한다."""
    settings = Settings(_env_file=None, internal_service_token=SecretStr("sec"), tool_cache_ttl_seconds=60.0)
    store = SupervisorRedisStore(None)

    async def fake_mcp(tool_name: str, args: dict[str, Any]) -> Any:
        return []

    def fake_rest(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"incident": {"incidentCode": "INC-1", "serviceName": "payment-service", "status": "OPEN"}})

    backend = BackendClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(fake_rest)), mcp_caller=fake_mcp)
    seen_cached_inputs: list[dict[str, Any]] = []

    async def spy_a2a(url: str, payload: dict[str, Any], context_id: str) -> dict[str, Any]:
        if url == settings.log_agent_a2a_url:
            return {"ok": True, "incidentCode": "INC-1", "evidence": [], "evidenceChunkIds": [], "remainingChunkIds": [], "pulledCount": 0}
        seen_cached_inputs.append(dict(payload.get("cachedToolResults") or {}))
        return {"ok": False, "status": "BLOCKED", "message": "done", "toolCalls": [], "cachedToolResults": {"fresh": True}}

    svc = SupervisorService(settings, httpx.AsyncClient(), backend, store, a2a_caller=spy_a2a)

    async def run_once(run_id: str) -> None:
        req = RunAgentInput(
            thread_id="t-ttl",
            run_id=run_id,
            state={},
            messages=[{"id": f"m-{run_id}", "role": "user", "content": "502 롤백 계획"}],
            tools=[],
            context=[],
            forwarded_props={"incidentCode": "INC-1", "incidentStartedAt": "2026-11-18T14:04:00+09:00"},
        )
        props = svc.parse_forwarded_props(req)
        async for _ in svc.stream_ag_ui(req, props, "op", None, None):
            pass

    await run_once("r1")
    await run_once("r2")
    mem = await store.get_thread_memory("t-ttl")
    mem["cachedToolResultsAt"] = time.time() - 120.0
    await store.save_thread_memory("t-ttl", mem)
    await run_once("r3")

    assert seen_cached_inputs == [{}, {"fresh": True}, {}]


def test_a2a_401_responses_include_security_headers() -> None:
    """A2A JSON-RPC 인증 미들웨어가 반환하는 401 응답에도 보안 헤더가 포함되는지 검증한다."""
    cfg = Settings(_env_file=None, internal_service_token=SecretStr("secret-token"))
    for app in (create_log_agent_app(cfg), create_remediation_agent_app(cfg)):
        with TestClient(app) as client:
            resp = client.post("/", json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {}})
            assert resp.status_code == 401
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.headers["cache-control"] == "no-store"
