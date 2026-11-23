from __future__ import annotations

import asyncio
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
_LOG_INDEX_KEY = "exec_log:index"
_PLAN_PREFIX = "planner_plan:"
_PLAN_INDEX_KEY = "planner_plan:index"


def _empty_thread_memory(thread_id: str) -> dict[str, Any]:
    return {
        "threadId": thread_id,
        "messages": [],
        "constraints": {},
        "snapshot": None,
        "replanHistory": [],
        "pulledChunkIds": [],
        "cachedToolResults": {},
        "cachedToolResultsAt": None,
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


class SupervisorRedisStore:
    """기존 Redis에 대화 메모리·실행 로그·작업 계획을 키 프리픽스로 분리 저장하는 CRUD 계층."""

    def __init__(self, redis_client: Any = None, *, health_check_interval: float = 5.0) -> None:
        self._redis = redis_client
        self._health_check_interval = health_check_interval
        self._redis_healthy_until = 0.0
        self._redis_retry_after = 0.0
        self._reconcile_lock = asyncio.Lock()
        self._thread_locks: dict[str, list[Any]] = {}
        # 로컬 테스트나 Redis 일시 단절 시 사용하는 프로세스 메모리 폴백 저장소 및 복구 재반영 큐
        self._mem_threads: dict[str, dict[str, Any]] = {}
        self._mem_logs: dict[str, list[dict[str, Any]]] = {}
        self._mem_plans: dict[str, dict[str, Any]] = {}
        self._dirty_threads: set[str] = set()
        self._pending_log_events: dict[str, list[dict[str, Any]]] = {}
        self._deleted_logs: set[str] = set()
        self._dirty_plans: set[str] = set()
        self._deleted_plans: set[str] = set()

    @asynccontextmanager
    async def thread_lock(self, thread_id: str) -> AsyncIterator[None]:
        """동일 스레드 ID의 상태 읽기·수정·저장을 직렬화하고 해제 시 락 참조를 정리한다."""
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

    def _mark_redis_down(self) -> None:
        self._redis_healthy_until = 0.0
        self._redis_retry_after = time.monotonic() + 30.0

    def _has_pending_reconcile(self) -> bool:
        return bool(
            self._dirty_threads
            or self._deleted_logs
            or self._pending_log_events
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
                    await self._redis.set(f"{_MEM_PREFIX}{tid}", json.dumps(self._mem_threads[tid], ensure_ascii=False))
                self._dirty_threads.discard(tid)
                self._mem_threads.pop(tid, None)

            for sid in list(self._deleted_logs):
                await self._redis.delete(f"{_LOG_PREFIX}{sid}")
                await self._redis.srem(_LOG_INDEX_KEY, sid)
                self._deleted_logs.discard(sid)

            for sid in list(self._pending_log_events):
                events = self._pending_log_events.get(sid)
                if not events:
                    self._pending_log_events.pop(sid, None)
                    self._mem_logs.pop(sid, None)
                    continue
                pushed = False
                try:
                    while events:
                        await self._redis.rpush(f"{_LOG_PREFIX}{sid}", json.dumps(events[0], ensure_ascii=False))
                        pushed = True
                        events.pop(0)
                finally:
                    if pushed:
                        await self._redis.sadd(_LOG_INDEX_KEY, sid)
                self._pending_log_events.pop(sid, None)
                self._mem_logs.pop(sid, None)

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
                    return json.loads(raw)
                return _empty_thread_memory(thread_id)
            except Exception:
                self._mark_redis_down()
        return dict(self._mem_threads.get(thread_id) or _empty_thread_memory(thread_id))

    async def save_thread_memory(self, thread_id: str, data: dict[str, Any]) -> None:
        """스레드 대화 메모리를 저장한다."""
        if await self._use_redis():
            try:
                await self._redis.set(f"{_MEM_PREFIX}{thread_id}", json.dumps(data, ensure_ascii=False))
                self._dirty_threads.discard(thread_id)
                self._mem_threads.pop(thread_id, None)
                return
            except Exception:
                self._mark_redis_down()
        self._mem_threads[thread_id] = data
        self._dirty_threads.add(thread_id)

    # --- 2) 세션 실행 로그 CRUD (exec_log:{session_id}, TTL 없음, 수정 불가) ---

    async def append_session_log(self, session_id: str, event: dict[str, Any]) -> None:
        """세션 실행 로그 끝에 이벤트를 추가한다 (수정 불가, 추가 전용)."""
        if await self._use_redis():
            try:
                await self._redis.rpush(f"{_LOG_PREFIX}{session_id}", json.dumps(event, ensure_ascii=False))
                await self._redis.sadd(_LOG_INDEX_KEY, session_id)
                return
            except Exception:
                self._mark_redis_down()
        self._mem_logs.setdefault(session_id, []).append(event)
        self._pending_log_events.setdefault(session_id, []).append(event)

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

    async def delete_session_logs(self, session_id: str) -> bool:
        """특정 세션의 실행 로그를 삭제한다."""
        existed = session_id in self._mem_logs or session_id in self._pending_log_events
        self._mem_logs.pop(session_id, None)
        self._pending_log_events.pop(session_id, None)
        if await self._use_redis():
            try:
                removed = await self._redis.delete(f"{_LOG_PREFIX}{session_id}")
                await self._redis.srem(_LOG_INDEX_KEY, session_id)
                self._deleted_logs.discard(session_id)
                return bool(removed or existed)
            except Exception:
                self._mark_redis_down()
        self._deleted_logs.add(session_id)
        return existed

    # --- 3) 작업 계획 관리 CRUD (F3015~F3016, 동일 날짜 시간 겹침 경고) ---

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
        existing = await self.list_plans()
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
        current = await self.get_plan(plan_id)
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

        existing = await self.list_plans()
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
        """저장된 작업 계획을 삭제한다."""
        existed = plan_id in self._mem_plans
        self._mem_plans.pop(plan_id, None)
        self._dirty_plans.discard(plan_id)
        if await self._use_redis():
            try:
                removed = await self._redis.delete(f"{_PLAN_PREFIX}{plan_id}")
                await self._redis.srem(_PLAN_INDEX_KEY, plan_id)
                self._deleted_plans.discard(plan_id)
                return bool(removed or existed)
            except Exception:
                self._mark_redis_down()
        self._deleted_plans.add(plan_id)
        return existed
