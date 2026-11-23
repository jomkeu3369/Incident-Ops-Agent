from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from ag_ui.core import RunAgentInput
from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr

from common.anomaly import SessionAnomalyAnalyzer
from common.backend_client import BackendClient
from common.config import Settings
from log_agent.main import create_log_agent_app
from remediation_agent.main import create_remediation_agent_app
from supervisor.cruds import StoreUnavailableError, SupervisorRedisStore
from supervisor.main import create_supervisor_app
from supervisor.routers import get_anomaly_analyzer
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
    """정상 Redis에서 PING 반복과 무제한 인메모리 누적을 막는지 검증한다."""
    redis = _FullFakeRedis()
    store = SupervisorRedisStore(redis, health_check_interval=60.0, max_cached_threads=3)

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
    assert len(store._mem_threads) == 3
    assert list(store._mem_threads.keys()) == ["t-2", "t-3", "t-4"]
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


async def test_supervisor_tool_cache_expires_per_entry_without_sliding_extension() -> None:
    """새 Tool 캐시 키 추가가 기존 키의 TTL을 연장하지 않고 항목별로 만료되는지 검증한다."""
    settings = Settings(_env_file=None, internal_service_token=SecretStr("sec"), tool_cache_ttl_seconds=60.0)
    store = SupervisorRedisStore(None)

    async def fake_mcp(tool_name: str, args: dict[str, Any]) -> Any:
        return []

    def fake_rest(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"incident": {"incidentCode": "INC-1", "serviceName": "payment-service", "status": "OPEN"}})

    backend = BackendClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(fake_rest)), mcp_caller=fake_mcp)
    seen_cached_inputs: list[dict[str, Any]] = []
    call_idx = 0

    async def spy_a2a(url: str, payload: dict[str, Any], context_id: str) -> dict[str, Any]:
        nonlocal call_idx
        if url == settings.log_agent_a2a_url:
            return {"ok": True, "incidentCode": "INC-1", "evidence": [], "evidenceChunkIds": [], "remainingChunkIds": [], "pulledCount": 0}
        call_idx += 1
        seen_cached_inputs.append(dict(payload.get("cachedToolResults") or {}))
        if call_idx == 1:
            return {"ok": False, "status": "BLOCKED", "message": "r1", "toolCalls": [], "cachedToolResults": {"freeze_a": True}}
        if call_idx == 2:
            merged = dict(payload.get("cachedToolResults") or {})
            merged["freeze_b"] = True
            return {"ok": False, "status": "BLOCKED", "message": "r2", "toolCalls": [], "cachedToolResults": merged}
        return {"ok": False, "status": "BLOCKED", "message": "r3", "toolCalls": [], "cachedToolResults": dict(payload.get("cachedToolResults") or {})}

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
    mem = await store.get_thread_memory("t-ttl")
    mem["cachedToolResultsTimestamps"]["freeze_a"] = time.time() - 40.0
    await store.save_thread_memory("t-ttl", mem)

    await run_once("r2")
    mem2 = await store.get_thread_memory("t-ttl")
    assert mem2["cachedToolResultsTimestamps"]["freeze_a"] < mem2["cachedToolResultsTimestamps"]["freeze_b"]

    mem2["cachedToolResultsTimestamps"]["freeze_a"] -= 30.0
    await store.save_thread_memory("t-ttl", mem2)
    await run_once("r3")

    assert seen_cached_inputs == [{}, {"freeze_a": True}, {"freeze_b": True}]


async def test_redis_store_preserves_thread_history_across_outage_and_eviction() -> None:
    """단절 전 캐시된 스레드는 보존되고 LRU에서 제거된 스레드는 복구 시 기존 Redis 이력을 덮어쓰지 않는지 검증한다."""
    redis = _FullFakeRedis()
    store = SupervisorRedisStore(redis, health_check_interval=0.0, max_cached_threads=1)

    await store.save_thread_memory("t-old", {"threadId": "t-old", "messages": [{"id": "orig-old"}]})
    await store.save_thread_memory("t-hot", {"threadId": "t-hot", "messages": [{"id": "orig-hot"}]})

    redis.up = False
    hot_mem = await store.get_thread_memory("t-hot")
    assert hot_mem["messages"] == [{"id": "orig-hot"}]
    await store.save_thread_memory("t-hot", {"threadId": "t-hot", "messages": [{"id": "orig-hot"}, {"id": "offline-hot"}]})

    evicted_mem = await store.get_thread_memory("t-old")
    assert evicted_mem["messages"] == []
    await store.save_thread_memory("t-old", {"threadId": "t-old", "messages": [{"id": "partial-old"}]})
    await store.save_thread_memory("t-new", {"threadId": "t-new", "messages": [{"id": "offline-new"}]})

    redis.up = True
    store._redis_retry_after = 0.0
    assert (await store.get_thread_memory("t-hot"))["messages"] == [{"id": "orig-hot"}, {"id": "offline-hot"}]
    assert (await store.get_thread_memory("t-old"))["messages"] == [{"id": "orig-old"}]
    assert (await store.get_thread_memory("t-new"))["messages"] == [{"id": "offline-new"}]


async def test_redis_store_sadd_failure_retries_index_without_duplicating_events() -> None:
    """append_session_log와 재반영 중 SADD만 실패해도 이벤트 중복 적재 없이 인덱스 등록만 재시도되는지 검증한다."""

    class _FailSaddRedis(_FullFakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.fail_sadd = False

        async def sadd(self, k: str, v: str) -> None:
            if self.fail_sadd:
                raise ConnectionError("sadd error")
            await super().sadd(k, v)

    redis = _FailSaddRedis()
    store = SupervisorRedisStore(redis, health_check_interval=0.0)

    redis.fail_sadd = True
    await store.append_session_log("sess-live", {"sessionId": "sess-live", "timestamp": "2026-11-23T00:00:01Z"})

    redis.fail_sadd = False
    redis.up = False
    await store.append_session_log("sess-offline", {"sessionId": "sess-offline", "timestamp": "2026-11-23T00:00:02Z"})

    redis.up = True
    redis.fail_sadd = True
    store._redis_retry_after = 0.0
    # 첫 복구 시도: sess-offline 이벤트 RPUSH 후 SADD 단계에서 실패한다.
    _ = await store.get_session_logs("sess-live")
    assert len(redis.lists["exec_log:sess-live"]) == 1
    assert len(redis.lists["exec_log:sess-offline"]) == 1

    redis.fail_sadd = False
    store._redis_retry_after = 0.0
    sessions = {s.sessionId for s in await store.list_sessions()}
    assert {"sess-live", "sess-offline"} <= sessions
    assert len(await store.get_session_logs("sess-live")) == 1
    assert len(await store.get_session_logs("sess-offline")) == 1


def test_a2a_401_responses_include_security_headers() -> None:
    """A2A JSON-RPC 인증 미들웨어가 반환하는 401 응답에도 보안 헤더가 포함되는지 검증한다."""
    cfg = Settings(_env_file=None, internal_service_token=SecretStr("secret-token"))
    for app in (create_log_agent_app(cfg), create_remediation_agent_app(cfg)):
        with TestClient(app) as client:
            resp = client.post("/", json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {}})
            assert resp.status_code == 401
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.headers["cache-control"] == "no-store"


async def test_redis_store_pins_in_flight_locked_thread_against_lru_eviction() -> None:
    """실행 중(thread_lock 보유) 스레드는 다른 스레드 LRU 축출에서도 캐시에 고정되어 오프라인 갱신이 유실되지 않는지 검증한다."""
    redis = _FullFakeRedis()
    store = SupervisorRedisStore(redis, health_check_interval=0.0, max_cached_threads=1)
    await store.save_thread_memory("t-inflight", {"threadId": "t-inflight", "messages": [{"id": "m0"}]})

    async with store.thread_lock("t-inflight"):
        loaded = await store.get_thread_memory("t-inflight")
        assert loaded["messages"] == [{"id": "m0"}]

        await store.save_thread_memory("t-other-1", {"threadId": "t-other-1", "messages": [{"id": "o1"}]})
        await store.save_thread_memory("t-other-2", {"threadId": "t-other-2", "messages": [{"id": "o2"}]})
        assert "t-inflight" in store._mem_threads

        redis.up = False
        loaded["messages"].append({"id": "m1-offline"})
        await store.save_thread_memory("t-inflight", loaded)

    assert "t-inflight" not in store._unverified_fallback_threads
    redis.up = True
    store._redis_retry_after = 0.0
    recovered = await store.get_thread_memory("t-inflight")
    assert recovered["messages"] == [{"id": "m0"}, {"id": "m1-offline"}]
    assert len(store._mem_threads) <= 1


async def test_redis_store_and_router_reject_uncertain_delete_with_503_without_queuing() -> None:
    """Redis 단절 중 로컬에 없는 세션·계획 삭제는 삭제 큐잉 없이 503을 반환하고 기존 데이터를 보존하는지 검증한다."""
    cfg = Settings(_env_file=None, internal_service_token=SecretStr("secret-token"))
    redis = _FullFakeRedis()
    store = SupervisorRedisStore(redis, health_check_interval=0.0)
    headers = {"X-Internal-Token": "secret-token"}

    app = create_supervisor_app(cfg)
    async with app.router.lifespan_context(app):
        app.state.container.supervisor_store = store
        await store.append_session_log("sess-persisted", {"sessionId": "sess-persisted", "timestamp": "2026-11-23T00:00:00Z"})
        await store.create_plan(
            ManagedPlanCreate(
                planId="PLAN-PERSISTED",
                incidentCode="INC-1",
                title="Persisted Plan",
                planDate="2026-11-23",
                startTime="01:00",
                endTime="01:10",
            )
        )

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            redis.up = False
            try:
                await store.delete_session_logs("sess-persisted")
                raise AssertionError("StoreUnavailableError expected")
            except StoreUnavailableError:
                pass

            res_sess_503 = await client.delete("/api/ag-ui/sessions/sess-persisted", headers=headers)
            assert res_sess_503.status_code == 503
            res_plan_503 = await client.delete("/api/ag-ui/plans/PLAN-PERSISTED", headers=headers)
            assert res_plan_503.status_code == 503
            assert store._deleted_logs == set()
            assert store._deleted_plans == set()

            await store.append_session_log("sess-offline", {"sessionId": "sess-offline", "timestamp": "2026-11-23T00:00:01Z"})
            res_plan_create = await client.post(
                "/api/ag-ui/plans",
                headers=headers,
                json={
                    "planId": "PLAN-OFFLINE",
                    "incidentCode": "INC-1",
                    "title": "Offline Plan",
                    "planDate": "2026-11-23",
                    "startTime": "02:00",
                    "endTime": "02:10",
                },
            )
            assert res_plan_create.status_code == 201
            assert (await client.delete("/api/ag-ui/sessions/sess-offline", headers=headers)).status_code == 204
            assert (await client.delete("/api/ag-ui/plans/PLAN-OFFLINE", headers=headers)).status_code == 204

            redis.up = True
            store._redis_retry_after = 0.0
            assert (await client.get("/api/ag-ui/sessions/sess-persisted", headers=headers)).status_code == 200
            assert (await client.get("/api/ag-ui/plans/PLAN-PERSISTED", headers=headers)).status_code == 200

            # 로컬 전용(redis=None) 저장소에서는 미존재 삭제 시 기존대로 404를 반환한다.
            app.state.container.supervisor_store = SupervisorRedisStore(None)
            assert (await client.delete("/api/ag-ui/sessions/missing-sess", headers=headers)).status_code == 404
            assert (await client.delete("/api/ag-ui/plans/MISSING-PLAN", headers=headers)).status_code == 404


async def test_redis_store_atomic_eval_deduplicates_after_ambiguous_rpush_timeout() -> None:
    """Lua eval 기반 실행 로그 적재 시 응답 전 타임아웃(모호한 실패) 뒤 재시도해도 중복 없이 멱등 반영되는지 검증한다."""

    class _AtomicEvalFakeRedis(_FullFakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.eval_calls = 0
            self.fail_after_commit_calls: set[int] = set()

        async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
            self.eval_calls += 1
            keys = keys_and_args[:numkeys]
            args = keys_and_args[numkeys:]
            log_key, ids_key, index_key = keys[0], keys[1], keys[2]
            event_id, raw_json, session_id = str(args[0]), str(args[1]), str(args[2])
            seen = self.sets.setdefault(ids_key, set())
            if event_id not in seen:
                seen.add(event_id)
                self.lists.setdefault(log_key, []).append(raw_json)
            self.sets.setdefault(index_key, set()).add(session_id)
            if self.eval_calls in self.fail_after_commit_calls:
                raise TimeoutError("ambiguous timeout after RPUSH")
            return 1

        async def delete(self, k: str) -> int:
            removed_set = self.sets.pop(k, None) is not None
            return int(bool(await super().delete(k)) or removed_set)

    redis = _AtomicEvalFakeRedis()
    store = SupervisorRedisStore(redis, health_check_interval=0.0)

    # 1) 첫 실시간 적재가 서버 커밋 직후 TimeoutError로 실패해도 동일 eventId가 대기 큐에 보존된다.
    redis.fail_after_commit_calls = {1}
    await store.append_session_log("sess-ambig", {"sessionId": "sess-ambig", "timestamp": "2026-11-23T00:00:01Z", "seq": 1})
    assert len(redis.lists["exec_log:sess-ambig"]) == 1

    # 2) 오프라인 구간에서 두 번째 이벤트 누적
    await store.append_session_log("sess-ambig", {"sessionId": "sess-ambig", "timestamp": "2026-11-23T00:00:02Z", "seq": 2})

    # 3) 첫 복구 시도 중 2번째(seq=1 재시도)는 멱등 중복 제거되고, 3번째(seq=2 커밋 후)는 다시 TimeoutError 발생
    redis.fail_after_commit_calls = {3}
    store._redis_retry_after = 0.0
    _ = await store.get_session_logs("sess-ambig")
    assert len(redis.lists["exec_log:sess-ambig"]) == 2

    # 4) 두 번째 복구 완료 후에도 중복 없이 정확히 2건만 존재한다.
    redis.fail_after_commit_calls = set()
    store._redis_retry_after = 0.0
    recovered = await store.get_session_logs("sess-ambig")
    assert [ev["seq"] for ev in recovered] == [1, 2]
    assert "sess-ambig" in {s.sessionId for s in await store.list_sessions()}

    # 5) 세션 삭제 시 실행 로그와 ID 중복 제거 셋이 모두 정리된다.
    assert await store.delete_session_logs("sess-ambig") is True
    assert "exec_log:sess-ambig" not in redis.lists
    assert "exec_log_ids:sess-ambig" not in redis.sets


async def test_redis_store_dedup_keys_are_disjoint_from_session_log_keys() -> None:
    """sessionId에 'ids:' 접두어가 포함되어도 실행 로그 키와 중복 제거 SET 키가 충돌하지 않는지 검증한다."""

    class _DisjointCheckRedis(_FullFakeRedis):
        async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
            keys = keys_and_args[:numkeys]
            args = keys_and_args[numkeys:]
            log_key, ids_key, index_key = str(keys[0]), str(keys[1]), str(keys[2])
            event_id, raw_json, session_id = str(args[0]), str(args[1]), str(args[2])
            assert log_key != ids_key
            seen = self.sets.setdefault(ids_key, set())
            if event_id not in seen:
                seen.add(event_id)
                self.lists.setdefault(log_key, []).append(raw_json)
            self.sets.setdefault(index_key, set()).add(session_id)
            return 1

        async def delete(self, k: str) -> int:
            removed_set = self.sets.pop(k, None) is not None
            return int(bool(await super().delete(k)) or removed_set)

    redis = _DisjointCheckRedis()
    store = SupervisorRedisStore(redis, health_check_interval=60.0)

    await store.append_session_log("foo", {"sessionId": "foo", "timestamp": "2026-11-23T00:00:01Z", "tag": "plain"})
    await store.append_session_log("ids:foo", {"sessionId": "ids:foo", "timestamp": "2026-11-23T00:00:02Z", "tag": "prefixed"})

    assert len(redis.lists["exec_log:foo"]) == 1
    assert len(redis.lists["exec_log:ids:foo"]) == 1
    assert len(redis.sets["exec_log_ids:foo"]) == 1
    assert len(redis.sets["exec_log_ids:ids:foo"]) == 1

    assert await store.delete_session_logs("ids:foo") is True
    assert "exec_log:ids:foo" not in redis.lists
    assert "exec_log_ids:ids:foo" not in redis.sets
    assert [ev["tag"] for ev in await store.get_session_logs("foo")] == ["plain"]
    assert len(redis.sets["exec_log_ids:foo"]) == 1


async def test_anomaly_endpoint_rejects_incomplete_fallback_logs_with_503() -> None:
    """Redis 장애로 실행 로그가 불완전할 때 이상 분석이 거짓 NORMAL/UNDETERMINED 대신 503을 반환하는지 검증한다."""
    cfg = Settings(_env_file=None, internal_service_token=SecretStr("secret-token"))
    redis = _FullFakeRedis()
    store = SupervisorRedisStore(redis, health_check_interval=0.0)
    headers = {"X-Internal-Token": "secret-token"}

    async def fake_diag(sid: str, findings: list[dict[str, Any]], events: list[dict[str, Any]]) -> str:
        return "미계획 도구 호출이 감지되었습니다. 허용 도구 목록을 점검하세요. 실행 로그를 보완하세요."

    app = create_supervisor_app(cfg)
    app.dependency_overrides[get_anomaly_analyzer] = lambda: SessionAnomalyAnalyzer(diagnosis_fn=fake_diag)

    async with app.router.lifespan_context(app):
        app.state.container.supervisor_store = store
        await store.append_session_log(
            "sess-partial",
            {
                "sessionId": "sess-partial",
                "eventType": "step_finished",
                "timestamp": "2026-11-23T00:00:01Z",
                "payload": {"step": "plan", "steps": ["a2a:log-analyzer-agent"]},
            },
        )

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            redis.up = False
            await store.append_session_log(
                "sess-partial",
                {
                    "sessionId": "sess-partial",
                    "eventType": "tool_call",
                    "timestamp": "2026-11-23T00:00:02Z",
                    "payload": {"tool": "unknown_shell_exec", "args": {"cmd": "ls"}},
                },
            )

            res_partial = await client.get("/api/ag-ui/sessions/sess-partial/anomalies", headers=headers)
            assert res_partial.status_code == 503

            redis.up = True
            store._redis_retry_after = 0.0
            res_recovered = await client.get("/api/ag-ui/sessions/sess-partial/anomalies", headers=headers)
            assert res_recovered.status_code == 200
            body = res_recovered.json()
            assert body["status"] == "ANOMALY"
            assert body["findings"][0]["rule"] == "UNPLANNED_TOOL_CALL"

            # 아직 한 번도 Redis에 연결되지 않은 신규 스토어의 단절 폴백에서도 거짓 NORMAL 대신 503을 반환한다.
            cold_redis = _FullFakeRedis()
            cold_redis.up = False
            cold_store = SupervisorRedisStore(cold_redis, health_check_interval=0.0)
            app.state.container.supervisor_store = cold_store
            await cold_store.append_session_log(
                "sess-cold-outage",
                {
                    "sessionId": "sess-cold-outage",
                    "eventType": "tool_call",
                    "timestamp": "2026-11-23T00:00:03Z",
                    "payload": {"tool": "unknown_shell_exec", "args": {"cmd": "id"}},
                },
            )
            res_cold = await client.get("/api/ag-ui/sessions/sess-cold-outage/anomalies", headers=headers)
            assert res_cold.status_code == 503
