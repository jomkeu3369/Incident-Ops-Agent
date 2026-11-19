from __future__ import annotations

import json
import time
import uuid
from typing import Any

from common.event_log import now_iso_utc
from supervisor.schemas import ManagedPlanCreate, ManagedPlanItem, ManagedPlanMutationResponse, ManagedPlanUpdate, SessionSummary

# Redis 키 프리픽스 분리: 대화 메모리(memory:), 실행 로그(exec_log:), 작업 계획(planner_plan:)
_MEM_PREFIX = "memory:"
_LOG_PREFIX = "exec_log:"
_LOG_INDEX_KEY = "exec_log:index"
_PLAN_PREFIX = "planner_plan:"
_PLAN_INDEX_KEY = "planner_plan:index"


class SupervisorRedisStore:
    """기존 Redis에 대화 메모리·실행 로그·작업 계획을 키 프리픽스로 분리 저장하는 CRUD 계층."""

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client
        self._redis_retry_after = 0.0
        # 로컬 테스트나 Redis 일시 단절 시 사용하는 프로세스 메모리 폴백 저장소
        self._mem_threads: dict[str, dict[str, Any]] = {}
        self._mem_logs: dict[str, list[dict[str, Any]]] = {}
        self._mem_plans: dict[str, dict[str, Any]] = {}

    async def _use_redis(self) -> bool:
        """Redis가 연결 가능할 때만 True를 반환하며, 실패 시 30초 쿨다운으로 지연을 방지한다."""
        if self._redis is None or time.monotonic() < self._redis_retry_after:
            return False
        try:
            await self._redis.ping()
            return True
        except Exception:
            self._redis_retry_after = time.monotonic() + 30.0
            return False

    # --- 1) 대화 메모리 CRUD (memory:{thread_id}) ---

    async def get_thread_memory(self, thread_id: str) -> dict[str, Any]:
        """스레드별 대화 이력·추출 조건·최신 스냅샷·캐시 요약을 조회한다."""
        key = f"{_MEM_PREFIX}{thread_id}"
        if await self._use_redis():
            raw = await self._redis.get(key)
            if raw:
                return json.loads(raw)
        return dict(self._mem_threads.get(thread_id) or {"threadId": thread_id, "messages": [], "constraints": {}, "snapshot": None, "replanHistory": [], "pulledChunkIds": [], "cachedToolResults": {}})

    async def save_thread_memory(self, thread_id: str, data: dict[str, Any]) -> None:
        """스레드 대화 메모리를 저장한다."""
        self._mem_threads[thread_id] = data
        if await self._use_redis():
            await self._redis.set(f"{_MEM_PREFIX}{thread_id}", json.dumps(data, ensure_ascii=False))

    # --- 2) 세션 실행 로그 CRUD (exec_log:{session_id}, TTL 없음, 수정 불가) ---

    async def append_session_log(self, session_id: str, event: dict[str, Any]) -> None:
        """세션 실행 로그 끝에 이벤트를 추가한다 (수정 불가, 추가 전용)."""
        self._mem_logs.setdefault(session_id, []).append(event)
        if await self._use_redis():
            await self._redis.rpush(f"{_LOG_PREFIX}{session_id}", json.dumps(event, ensure_ascii=False))
            await self._redis.sadd(_LOG_INDEX_KEY, session_id)

    async def list_sessions(self) -> list[SessionSummary]:
        """저장된 실행 로그 세션 목록을 요약 정보와 함께 조회한다."""
        if await self._use_redis():
            ids = sorted(await self._redis.smembers(_LOG_INDEX_KEY))
            out: list[SessionSummary] = []
            for sid in ids:
                events = await self.get_session_logs(sid)
                out.append(SessionSummary(sessionId=sid, eventCount=len(events), startedAt=events[0]["timestamp"] if events else None, lastEventAt=events[-1]["timestamp"] if events else None))
            return out

        return [
            SessionSummary(sessionId=sid, eventCount=len(events), startedAt=events[0]["timestamp"] if events else None, lastEventAt=events[-1]["timestamp"] if events else None)
            for sid, events in sorted(self._mem_logs.items())
        ]

    async def get_session_logs(self, session_id: str) -> list[dict[str, Any]]:
        """특정 세션의 실행 로그를 발생 순서대로 조회한다."""
        if await self._use_redis():
            rows = await self._redis.lrange(f"{_LOG_PREFIX}{session_id}", 0, -1)
            return [json.loads(r) for r in rows]
        return list(self._mem_logs.get(session_id, []))

    async def delete_session_logs(self, session_id: str) -> bool:
        """특정 세션의 실행 로그를 삭제한다."""
        existed = session_id in self._mem_logs
        self._mem_logs.pop(session_id, None)
        if await self._use_redis():
            removed = await self._redis.delete(f"{_LOG_PREFIX}{session_id}")
            await self._redis.srem(_LOG_INDEX_KEY, session_id)
            return bool(removed or existed)
        return existed

    # --- 3) 작업 계획 관리 CRUD (F3015~F3016, 동일 날짜 시간 겹침 경고) ---

    async def list_plans(self) -> list[ManagedPlanItem]:
        """저장된 작업 계획 전체 목록을 조회한다."""
        if await self._use_redis():
            ids = sorted(await self._redis.smembers(_PLAN_INDEX_KEY))
            items: list[ManagedPlanItem] = []
            for pid in ids:
                raw = await self._redis.get(f"{_PLAN_PREFIX}{pid}")
                if raw:
                    items.append(ManagedPlanItem.model_validate(json.loads(raw)))
            return items
        return [ManagedPlanItem.model_validate(v) for _, v in sorted(self._mem_plans.items())]

    async def get_plan(self, plan_id: str) -> ManagedPlanItem | None:
        """단건 작업 계획을 조회한다."""
        if await self._use_redis():
            raw = await self._redis.get(f"{_PLAN_PREFIX}{plan_id}")
            return ManagedPlanItem.model_validate(json.loads(raw)) if raw else None
        raw = self._mem_plans.get(plan_id)
        return ManagedPlanItem.model_validate(raw) if raw else None

    async def create_plan(self, body: ManagedPlanCreate) -> ManagedPlanMutationResponse:
        """작업 계획을 저장하고, 같은 날짜에 시간이 겹치는 기존 계획이 있으면 경고와 함께 반환한다."""
        now = now_iso_utc()
        plan_id = body.planId or f"PLAN-{uuid.uuid4().hex[:8].upper()}"
        existing = await self.list_plans()
        overlaps = [p for p in existing if p.planId != plan_id and p.planDate == body.planDate and body.startTime < p.endTime and p.startTime < body.endTime]

        item = ManagedPlanItem(planId=plan_id, incidentCode=body.incidentCode, title=body.title, serviceName=body.serviceName, planDate=body.planDate, startTime=body.startTime, endTime=body.endTime, actionCommand=body.actionCommand, evidenceChunkIds=body.evidenceChunkIds, notes=body.notes, createdAt=now, updatedAt=now)
        self._mem_plans[plan_id] = item.model_dump()
        if await self._use_redis():
            await self._redis.set(f"{_PLAN_PREFIX}{plan_id}", json.dumps(item.model_dump(), ensure_ascii=False))
            await self._redis.sadd(_PLAN_INDEX_KEY, plan_id)

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

        existing = await self.list_plans()
        overlaps = [p for p in existing if p.planId != plan_id and p.planDate == item.planDate and item.startTime < p.endTime and p.startTime < item.endTime]
        self._mem_plans[plan_id] = item.model_dump()
        if await self._use_redis():
            await self._redis.set(f"{_PLAN_PREFIX}{plan_id}", json.dumps(item.model_dump(), ensure_ascii=False))

        warning = f"같은 날짜({item.planDate})에 시간이 겹치는 계획이 {len(overlaps)}건 있습니다." if overlaps else None
        return ManagedPlanMutationResponse(plan=item, overlappingPlans=overlaps, warning=warning)

    async def delete_plan(self, plan_id: str) -> bool:
        """저장된 작업 계획을 삭제한다."""
        existed = plan_id in self._mem_plans
        self._mem_plans.pop(plan_id, None)
        if await self._use_redis():
            removed = await self._redis.delete(f"{_PLAN_PREFIX}{plan_id}")
            await self._redis.srem(_PLAN_INDEX_KEY, plan_id)
            return bool(removed or existed)
        return existed
