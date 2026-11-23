from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import json
import time
import uuid
from typing import Any, AsyncIterator

from common.event_log import now_iso_utc
from supervisor.schemas import ManagedPlanCreate, ManagedPlanItem, ManagedPlanMutationResponse, ManagedPlanUpdate, SessionSummary

# Redis 키 프리픽스 분리: 대화 메모리(memory:), 실행 로그(exec_log:), 작업 계획(planner_plan:)
_MEM_PREFIX = "memory:"
_LOG_PREFIX = "exec_log:"
_LOG_IDS_PREFIX = "exec_log_ids:"
_LOG_INDEX_KEY = "exec_log:index"
_PLAN_PREFIX = "planner_plan:"
_PLAN_INDEX_KEY = "planner_plan:index"

_APPEND_LOG_LUA = """
if redis.call('SADD', KEYS[2], ARGV[1]) == 1 then
    redis.call('RPUSH', KEYS[1], ARGV[2])
end
redis.call('SADD', KEYS[3], ARGV[3])
return 1
"""


class _LogIndexSyncError(RuntimeError):
    """eval 미지원 테스트 대역에서 RPUSH 성공 후 인덱스 동기화만 실패했을 때 내부 전파하는 예외."""


def _empty_thread_memory(thread_id: str) -> dict[str, Any]:
    return {
        "threadId": thread_id,
        "messages": [],
        "constraints": {},
        "snapshot": None,
        "replanHistory": [],
        "pulledChunkIds": [],
        "cachedToolResults": {},
        "cachedToolResultsTimestamps": {},
    }


def _parse_log_timestamp(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        ts = raw.get("timestamp")
        return str(ts) if ts is not None else None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("timestamp") is not None:
            return str(parsed["timestamp"])
    except (TypeError, ValueError):
        return None
    return None


class StoreUnavailableError(RuntimeError):
    """Redis 연결 단절로 존재 여부를 확인할 수 없는 파괴적 연산을 거부할 때 발생한다."""


class SupervisorRedisStore:
    """기존 Redis에 대화 메모리·실행 로그·작업 계획을 키 프리픽스로 분리 저장하는 CRUD 계층."""

    def __init__(
        self,
        redis_client: Any = None,
        *,
        health_check_interval: float = 5.0,
        max_cached_threads: int = 128,
    ) -> None:
        self._redis = redis_client
        self._health_check_interval = health_check_interval
        self._max_cached_threads = max(0, max_cached_threads)
        self._redis_healthy_until = 0.0
        self._redis_retry_after = 0.0
        self._redis_ever_connected = False
        self._reconcile_lock = asyncio.Lock()
        self._thread_locks: dict[str, list[Any]] = {}
        # 로컬 테스트나 Redis 일시 단절 시 사용하는 프로세스 메모리 폴백 저장소 및 복구 재반영 큐
        self._mem_threads: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._mem_logs: dict[str, list[dict[str, Any]]] = {}
        self._mem_plans: dict[str, dict[str, Any]] = {}
        self._dirty_threads: set[str] = set()
        self._unverified_fallback_threads: set[str] = set()
        self._pending_log_events: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self._pending_log_indices: set[str] = set()
        self._deleted_logs: set[str] = set()
        self._dirty_plans: set[str] = set()
        self._deleted_plans: set[str] = set()

    @asynccontextmanager
    async def thread_lock(self, thread_id: str) -> AsyncIterator[None]:
        """동일 스레드 ID의 상태 읽기·수정·저장을 직렬화하고 해제 시 락 참조·캐시를 정리한다."""
        entry = self._thread_locks.get(thread_id)
        if entry is None:
            entry = [asyncio.Lock(), 0]
            self._thread_locks[thread_id] = entry
        lock: asyncio.Lock = entry[0]
        entry[1] += 1
        try:
            async with lock:
                yield
        finally:
            entry[1] -= 1
            if entry[1] <= 0 and not lock.locked():
                self._thread_locks.pop(thread_id, None)
                self._trim_thread_cache()

    def _trim_thread_cache(self, keep: str | None = None) -> None:
        """LRU 상한을 초과한 clean·비잠금 스레드 캐시 항목을 오래된 순으로 제거한다."""
        if len(self._mem_threads) <= self._max_cached_threads:
            return
        # 아직 Redis에 반영되지 않은 dirty 스레드와 실행 중인(thread_lock 보유) 스레드는 보존한다.
        for candidate in list(self._mem_threads):
            if len(self._mem_threads) <= self._max_cached_threads:
                break
            if candidate == keep or candidate in self._dirty_threads or candidate in self._thread_locks:
                continue
            self._mem_threads.pop(candidate, None)

    def _cache_thread(self, thread_id: str, data: dict[str, Any]) -> None:
        """정상·복구 구간의 스레드 메모리를 Bounded LRU 캐시에 유지해 일시 단절 시 유실을 막는다."""
        if self._max_cached_threads <= 0 and thread_id not in self._dirty_threads and thread_id not in self._thread_locks:
            self._mem_threads.pop(thread_id, None)
            return
        self._mem_threads[thread_id] = dict(data)
        self._mem_threads.move_to_end(thread_id)
        self._trim_thread_cache(keep=thread_id if self._max_cached_threads > 0 else None)

    def _mark_redis_down(self) -> None:
        self._redis_healthy_until = 0.0
        self._redis_retry_after = time.monotonic() + 30.0

    def _has_pending_reconcile(self) -> bool:
        return bool(
            self._dirty_threads
            or self._deleted_logs
            or self._pending_log_events
            or self._pending_log_indices
            or self._deleted_plans
            or self._dirty_plans
        )

    async def _reconcile_fallback_to_redis(self) -> None:
        """Redis 복구 직후 폴백 구간 동안 누적된 쓰기·삭제 변경분을 Redis에 중복 없이 재반영한다."""
        if not self._has_pending_reconcile():
            return
        async with self._reconcile_lock:
            for tid in list(self._dirty_threads):
                if tid in self._mem_threads:
                    if tid in self._unverified_fallback_threads:
                        existing_raw = await self._redis.get(f"{_MEM_PREFIX}{tid}")
                        if existing_raw:
                            # 단절 전 기존 Redis 이력을 덮어쓰지 않고 Redis 상태를 기준으로 캐시를 재동기화한다.
                            self._dirty_threads.discard(tid)
                            self._unverified_fallback_threads.discard(tid)
                            self._cache_thread(tid, json.loads(existing_raw))
                            continue
                    await self._redis.set(f"{_MEM_PREFIX}{tid}", json.dumps(self._mem_threads[tid], ensure_ascii=False))
                self._dirty_threads.discard(tid)
                self._unverified_fallback_threads.discard(tid)
                if tid in self._mem_threads:
                    self._cache_thread(tid, self._mem_threads[tid])

            for sid in list(self._deleted_logs):
                await self._redis.delete(f"{_LOG_PREFIX}{sid}")
                await self._redis.delete(f"{_LOG_IDS_PREFIX}{sid}")
                await self._redis.srem(_LOG_INDEX_KEY, sid)
                self._deleted_logs.discard(sid)

            for sid in list(self._pending_log_events):
                events = self._pending_log_events.get(sid)
                if not events:
                    self._pending_log_events.pop(sid, None)
                    self._mem_logs.pop(sid, None)
                    continue
                while events:
                    event_id, ev = events[0]
                    try:
                        await self._append_log_to_redis(sid, event_id, json.dumps(ev, ensure_ascii=False))
                    except _LogIndexSyncError:
                        events.pop(0)
                        self._pending_log_indices.add(sid)
                        if not events:
                            self._pending_log_events.pop(sid, None)
                            self._mem_logs.pop(sid, None)
                        raise
                    events.pop(0)
                self._pending_log_events.pop(sid, None)
                self._mem_logs.pop(sid, None)

            for sid in list(self._pending_log_indices):
                await self._redis.sadd(_LOG_INDEX_KEY, sid)
                self._pending_log_indices.discard(sid)

            for pid in list(self._deleted_plans):
                await self._redis.delete(f"{_PLAN_PREFIX}{pid}")
                await self._redis.srem(_PLAN_INDEX_KEY, pid)
                self._deleted_plans.discard(pid)

            for pid in list(self._dirty_plans):
                if pid in self._mem_plans:
                    await self._redis.set(f"{_PLAN_PREFIX}{pid}", json.dumps(self._mem_plans[pid], ensure_ascii=False))
                    await self._redis.sadd(_PLAN_INDEX_KEY, pid)
                self._dirty_plans.discard(pid)
                self._mem_plans.pop(pid, None)

    async def _use_redis(self) -> bool:
        """Redis 가용 여부를 TTL 캐시로 판별하며, 복구 시 폴백 변경분을 먼저 동기화한다."""
        if self._redis is None:
            return False
        now = time.monotonic()
        if now < self._redis_retry_after:
            return False
        if now < self._redis_healthy_until:
            if not self._has_pending_reconcile():
                return True
            try:
                await self._reconcile_fallback_to_redis()
                return True
            except Exception:
                self._mark_redis_down()
                return False
        try:
            await self._redis.ping()
            self._redis_ever_connected = True
            await self._reconcile_fallback_to_redis()
            self._redis_healthy_until = time.monotonic() + self._health_check_interval
            return True
        except Exception:
            self._mark_redis_down()
            return False

    # --- 1) 대화 메모리 CRUD (memory:{thread_id}) ---

    async def get_thread_memory(self, thread_id: str) -> dict[str, Any]:
        """스레드별 대화 이력·추출 조건·최신 스냅샷·캐시 요약을 조회한다."""
        key = f"{_MEM_PREFIX}{thread_id}"
        if await self._use_redis():
            try:
                raw = await self._redis.get(key)
                if raw:
                    data = json.loads(raw)
                    self._cache_thread(thread_id, data)
                    return dict(data)
                return _empty_thread_memory(thread_id)
            except Exception:
                self._mark_redis_down()
        cached = self._mem_threads.get(thread_id)
        if cached is not None:
            self._mem_threads.move_to_end(thread_id)
            return dict(cached)
        return _empty_thread_memory(thread_id)

    async def save_thread_memory(self, thread_id: str, data: dict[str, Any]) -> None:
        """스레드 대화 메모리를 저장한다."""
        if await self._use_redis():
            try:
                await self._redis.set(f"{_MEM_PREFIX}{thread_id}", json.dumps(data, ensure_ascii=False))
                self._dirty_threads.discard(thread_id)
                self._unverified_fallback_threads.discard(thread_id)
                self._cache_thread(thread_id, data)
                return
            except Exception:
                self._mark_redis_down()
        if self._redis is not None and thread_id not in self._mem_threads:
            self._unverified_fallback_threads.add(thread_id)
        self._dirty_threads.add(thread_id)
        self._cache_thread(thread_id, data)

    # --- 2) 세션 실행 로그 CRUD (exec_log:{session_id}, TTL 없음, 수정 불가) ---

    async def _append_log_to_redis(self, session_id: str, event_id: str, raw_json: str) -> None:
        """고유 이벤트 ID 기반으로 실행 로그를 멱등·중복 없이 Redis에 추가한다."""
        log_key = f"{_LOG_PREFIX}{session_id}"
        ids_key = f"{_LOG_IDS_PREFIX}{session_id}"
        if hasattr(self._redis, "eval"):
            await self._redis.eval(_APPEND_LOG_LUA, 3, log_key, ids_key, _LOG_INDEX_KEY, event_id, raw_json, session_id)
            return

        seen_ids = await self._redis.smembers(ids_key)
        pushed = event_id in seen_ids
        try:
            if not pushed:
                await self._redis.rpush(log_key, raw_json)
                pushed = True
                await self._redis.sadd(ids_key, event_id)
            await self._redis.sadd(_LOG_INDEX_KEY, session_id)
        except Exception as exc:
            if pushed:
                raise _LogIndexSyncError from exc
            raise

    async def append_session_log(self, session_id: str, event: dict[str, Any]) -> None:
        """세션 실행 로그 끝에 이벤트를 추가한다 (수정 불가, 추가 전용)."""
        event_id = str(event.get("eventId") or uuid.uuid4().hex)
        raw_json = json.dumps(event, ensure_ascii=False)
        if await self._use_redis():
            try:
                await self._append_log_to_redis(session_id, event_id, raw_json)
                return
            except _LogIndexSyncError:
                self._mark_redis_down()
                self._pending_log_indices.add(session_id)
                return
            except Exception:
                self._mark_redis_down()
        self._mem_logs.setdefault(session_id, []).append(event)
        self._pending_log_events.setdefault(session_id, []).append((event_id, event))

    async def list_sessions(self) -> list[SessionSummary]:
        """저장된 실행 로그 세션 목록을 경량 조회(LLEN/LINDEX 우선)로 반환한다."""
        if await self._use_redis():
            try:
                ids = sorted(await self._redis.smembers(_LOG_INDEX_KEY))
                if not ids:
                    return []
                if hasattr(self._redis, "llen") and hasattr(self._redis, "lindex"):
                    out: list[SessionSummary] = []
                    if hasattr(self._redis, "pipeline"):
                        async with self._redis.pipeline(transaction=False) as pipe:
                            for sid in ids:
                                key = f"{_LOG_PREFIX}{sid}"
                                pipe.llen(key)
                                pipe.lindex(key, 0)
                                pipe.lindex(key, -1)
                            rows = await pipe.execute()
                        for idx, sid in enumerate(ids):
                            count = int(rows[idx * 3] or 0)
                            first_raw = rows[idx * 3 + 1] if count > 0 else None
                            last_raw = rows[idx * 3 + 2] if count > 0 else None
                            out.append(
                                SessionSummary(
                                    sessionId=sid,
                                    eventCount=count,
                                    startedAt=_parse_log_timestamp(first_raw),
                                    lastEventAt=_parse_log_timestamp(last_raw),
                                )
                            )
                        return out

                    for sid in ids:
                        key = f"{_LOG_PREFIX}{sid}"
                        count = int(await self._redis.llen(key) or 0)
                        first_raw = await self._redis.lindex(key, 0) if count > 0 else None
                        last_raw = await self._redis.lindex(key, -1) if count > 0 else None
                        out.append(
                            SessionSummary(
                                sessionId=sid,
                                eventCount=count,
                                startedAt=_parse_log_timestamp(first_raw),
                                lastEventAt=_parse_log_timestamp(last_raw),
                            )
                        )
                    return out

                fallback_out: list[SessionSummary] = []
                for sid in ids:
                    rows = await self._redis.lrange(f"{_LOG_PREFIX}{sid}", 0, -1)
                    events = [json.loads(r) for r in rows]
                    fallback_out.append(
                        SessionSummary(
                            sessionId=sid,
                            eventCount=len(events),
                            startedAt=events[0]["timestamp"] if events else None,
                            lastEventAt=events[-1]["timestamp"] if events else None,
                        )
                    )
                return fallback_out
            except Exception:
                self._mark_redis_down()

        return [
            SessionSummary(sessionId=sid, eventCount=len(events), startedAt=events[0]["timestamp"] if events else None, lastEventAt=events[-1]["timestamp"] if events else None)
            for sid, events in sorted(self._mem_logs.items())
        ]

    async def get_session_logs(self, session_id: str) -> list[dict[str, Any]]:
        """특정 세션의 실행 로그를 발생 순서대로 조회한다."""
        if await self._use_redis():
            try:
                rows = await self._redis.lrange(f"{_LOG_PREFIX}{session_id}", 0, -1)
                return [json.loads(r) for r in rows]
            except Exception:
                self._mark_redis_down()
        return list(self._mem_logs.get(session_id, []))

    async def get_session_logs_for_analysis(self, session_id: str) -> tuple[list[dict[str, Any]], bool]:
        """이상 징후 분석용 실행 로그와 전체 이력 완전성(is_complete) 여부를 함께 반환한다."""
        if await self._use_redis():
            try:
                rows = await self._redis.lrange(f"{_LOG_PREFIX}{session_id}", 0, -1)
                return [json.loads(r) for r in rows], True
            except Exception:
                self._mark_redis_down()
        if self._redis is not None and self._redis_ever_connected:
            raise StoreUnavailableError("Redis 연결이 일시 중단되어 전체 실행 로그 기반 이상 징후 분석을 수행할 수 없습니다.")
        return list(self._mem_logs.get(session_id, [])), self._redis is None

    async def delete_session_logs(self, session_id: str) -> bool:
        """특정 세션의 실행 로그를 삭제한다. Redis 단절로 존재 미확인 시 StoreUnavailableError를 발생시킨다."""
        existed = (
            session_id in self._mem_logs
            or session_id in self._pending_log_events
            or session_id in self._pending_log_indices
        )
        if await self._use_redis():
            deleted_redis = False
            removed = 0
            try:
                removed = int(await self._redis.delete(f"{_LOG_PREFIX}{session_id}") or 0)
                deleted_redis = True
                await self._redis.delete(f"{_LOG_IDS_PREFIX}{session_id}")
                await self._redis.srem(_LOG_INDEX_KEY, session_id)
                self._mem_logs.pop(session_id, None)
                self._pending_log_events.pop(session_id, None)
                self._pending_log_indices.discard(session_id)
                self._deleted_logs.discard(session_id)
                return bool(removed or existed)
            except Exception:
                self._mark_redis_down()
                if deleted_redis:
                    self._mem_logs.pop(session_id, None)
                    self._pending_log_events.pop(session_id, None)
                    self._pending_log_indices.discard(session_id)
                    if removed or existed:
                        self._deleted_logs.add(session_id)
                    return bool(removed or existed)
        if not existed:
            if self._redis is not None:
                raise StoreUnavailableError("Redis 연결이 일시 중단되어 실행 로그 존재 여부를 확인하거나 삭제할 수 없습니다.")
            return False
        self._mem_logs.pop(session_id, None)
        self._pending_log_events.pop(session_id, None)
        self._pending_log_indices.discard(session_id)
        if self._redis is not None:
            self._deleted_logs.add(session_id)
        return True

    # --- 3) 작업 계획 관리 CRUD (F3015~F3016, 동일 날짜 시간 겹침 경고) ---

    async def _list_plans_for_mutation(self) -> list[ManagedPlanItem]:
        """작업 계획 변경 전 기존 일정 전체를 조회하며, Redis 단절로 불완전한 경우 StoreUnavailableError를 발생시킨다."""
        if await self._use_redis():
            try:
                ids = sorted(await self._redis.smembers(_PLAN_INDEX_KEY))
                items: list[ManagedPlanItem] = []
                for pid in ids:
                    raw = await self._redis.get(f"{_PLAN_PREFIX}{pid}")
                    if raw:
                        items.append(ManagedPlanItem.model_validate(json.loads(raw)))
                return items
            except Exception:
                self._mark_redis_down()
        if self._redis is not None and self._redis_ever_connected:
            raise StoreUnavailableError("Redis 연결이 일시 중단되어 기존 작업 계획과의 시간 겹침 여부를 확인할 수 없습니다.")
        return [ManagedPlanItem.model_validate(v) for _, v in sorted(self._mem_plans.items())]

    async def _get_plan_for_mutation(self, plan_id: str) -> ManagedPlanItem | None:
        """작업 계획 수정 전 대상 계획을 조회하며, Redis 단절로 확인 불가 시 StoreUnavailableError를 발생시킨다."""
        if await self._use_redis():
            try:
                raw = await self._redis.get(f"{_PLAN_PREFIX}{plan_id}")
                return ManagedPlanItem.model_validate(json.loads(raw)) if raw else None
            except Exception:
                self._mark_redis_down()
        if self._redis is not None and self._redis_ever_connected:
            raise StoreUnavailableError("Redis 연결이 일시 중단되어 기존 작업 계획을 확인하거나 수정할 수 없습니다.")
        raw = self._mem_plans.get(plan_id)
        return ManagedPlanItem.model_validate(raw) if raw else None

    async def list_plans(self) -> list[ManagedPlanItem]:
        """저장된 작업 계획 전체 목록을 조회한다."""
        if await self._use_redis():
            try:
                ids = sorted(await self._redis.smembers(_PLAN_INDEX_KEY))
                items: list[ManagedPlanItem] = []
                for pid in ids:
                    raw = await self._redis.get(f"{_PLAN_PREFIX}{pid}")
                    if raw:
                        items.append(ManagedPlanItem.model_validate(json.loads(raw)))
                return items
            except Exception:
                self._mark_redis_down()
        return [ManagedPlanItem.model_validate(v) for _, v in sorted(self._mem_plans.items())]

    async def get_plan(self, plan_id: str) -> ManagedPlanItem | None:
        """단건 작업 계획을 조회한다."""
        if await self._use_redis():
            try:
                raw = await self._redis.get(f"{_PLAN_PREFIX}{plan_id}")
                return ManagedPlanItem.model_validate(json.loads(raw)) if raw else None
            except Exception:
                self._mark_redis_down()
        raw = self._mem_plans.get(plan_id)
        return ManagedPlanItem.model_validate(raw) if raw else None

    async def create_plan(self, body: ManagedPlanCreate) -> ManagedPlanMutationResponse:
        """작업 계획을 저장하고, 같은 날짜에 시간이 겹치는 기존 계획이 있으면 경고와 함께 반환한다."""
        now = now_iso_utc()
        plan_id = body.planId or f"PLAN-{uuid.uuid4().hex[:8].upper()}"
        existing = await self._list_plans_for_mutation()
        overlaps = [p for p in existing if p.planId != plan_id and p.planDate == body.planDate and body.startTime < p.endTime and p.startTime < body.endTime]

        item = ManagedPlanItem(planId=plan_id, incidentCode=body.incidentCode, title=body.title, serviceName=body.serviceName, planDate=body.planDate, startTime=body.startTime, endTime=body.endTime, actionCommand=body.actionCommand, evidenceChunkIds=body.evidenceChunkIds, notes=body.notes, createdAt=now, updatedAt=now)
        item_dump = item.model_dump()
        self._deleted_plans.discard(plan_id)

        wrote_redis = False
        if await self._use_redis():
            try:
                await self._redis.set(f"{_PLAN_PREFIX}{plan_id}", json.dumps(item_dump, ensure_ascii=False))
                await self._redis.sadd(_PLAN_INDEX_KEY, plan_id)
                self._dirty_plans.discard(plan_id)
                self._mem_plans.pop(plan_id, None)
                wrote_redis = True
            except Exception:
                self._mark_redis_down()

        if not wrote_redis:
            self._mem_plans[plan_id] = item_dump
            self._dirty_plans.add(plan_id)

        warning = f"같은 날짜({body.planDate})에 시간이 겹치는 계획이 {len(overlaps)}건 있습니다." if overlaps else None
        return ManagedPlanMutationResponse(plan=item, overlappingPlans=overlaps, warning=warning)

    async def update_plan(self, plan_id: str, body: ManagedPlanUpdate) -> ManagedPlanMutationResponse | None:
        """저장된 작업 계획의 핵심 값을 수정하고 시간 겹침 여부를 다시 확인한다."""
        current = await self._get_plan_for_mutation(plan_id)
        if current is None:
            return None
        updated_data = current.model_dump()
        for field, value in body.model_dump(exclude_none=True).items():
            updated_data[field] = value
        if updated_data["startTime"] >= updated_data["endTime"]:
            raise ValueError("startTime은 endTime보다 이전이어야 합니다.")
        updated_data["updatedAt"] = now_iso_utc()
        item = ManagedPlanItem.model_validate(updated_data)
        item_dump = item.model_dump()

        existing = await self._list_plans_for_mutation()
        overlaps = [p for p in existing if p.planId != plan_id and p.planDate == item.planDate and item.startTime < p.endTime and p.startTime < item.endTime]
        self._deleted_plans.discard(plan_id)

        wrote_redis = False
        if await self._use_redis():
            try:
                await self._redis.set(f"{_PLAN_PREFIX}{plan_id}", json.dumps(item_dump, ensure_ascii=False))
                await self._redis.sadd(_PLAN_INDEX_KEY, plan_id)
                self._dirty_plans.discard(plan_id)
                self._mem_plans.pop(plan_id, None)
                wrote_redis = True
            except Exception:
                self._mark_redis_down()

        if not wrote_redis:
            self._mem_plans[plan_id] = item_dump
            self._dirty_plans.add(plan_id)

        warning = f"같은 날짜({item.planDate})에 시간이 겹치는 계획이 {len(overlaps)}건 있습니다." if overlaps else None
        return ManagedPlanMutationResponse(plan=item, overlappingPlans=overlaps, warning=warning)

    async def delete_plan(self, plan_id: str) -> bool:
        """저장된 작업 계획을 삭제한다. Redis 단절로 존재 미확인 시 StoreUnavailableError를 발생시킨다."""
        existed = plan_id in self._mem_plans
        if await self._use_redis():
            deleted_redis = False
            removed = 0
            try:
                removed = int(await self._redis.delete(f"{_PLAN_PREFIX}{plan_id}") or 0)
                deleted_redis = True
                await self._redis.srem(_PLAN_INDEX_KEY, plan_id)
                self._mem_plans.pop(plan_id, None)
                self._dirty_plans.discard(plan_id)
                self._deleted_plans.discard(plan_id)
                return bool(removed or existed)
            except Exception:
                self._mark_redis_down()
                if deleted_redis:
                    self._mem_plans.pop(plan_id, None)
                    self._dirty_plans.discard(plan_id)
                    if removed or existed:
                        self._deleted_plans.add(plan_id)
                    return bool(removed or existed)
        if not existed:
            if self._redis is not None:
                raise StoreUnavailableError("Redis 연결이 일시 중단되어 작업 계획 존재 여부를 확인하거나 삭제할 수 없습니다.")
            return False
        self._mem_plans.pop(plan_id, None)
        self._dirty_plans.discard(plan_id)
        if self._redis is not None:
            self._deleted_plans.add(plan_id)
        return True
