from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import re
import time
import uuid
from typing import Any, Awaitable, Callable, TypedDict
import httpx
from langgraph.graph import END, START, StateGraph

from common.a2a_utils import send_a2a_json, send_a2a_stream
from common.backend_client import BackendApiError, BackendClient
from common.config import Settings
from common.event_log import EventBus, publish_safe
from common.planner_math import parse_rfc3339
from supervisor.ledger import approval_status, approval_view, find_approval, find_open_approval, find_plan

# 취소 시점에 원격 작업 ID를 아직 못 받았으면 이만큼 기다려 CancelTask 대상을 확보한다
_REMOTE_TASK_ID_GRACE_SECONDS = 2.0

A2ACaller = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]
UiEventSink = Callable[[dict[str, Any]], Awaitable[None]]

_GREETING_PATTERNS = ("안녕", "hello", "hi", "도움말", "사용법", "기능", "무엇을 할 수", "뭐 할 수")
_TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\s*[~-]\s*(\d{1,2}:\d{2})")
_DURATION_RE = re.compile(r"(\d+)\s*분")
_RATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_DEFAULT_LOG_KEYWORDS = ["HikariPool", "502", "rollback"]
_STOPWORDS = frozenset({"http", "https", "api", "service", "the", "and", "for", "with", "from", "this", "that", "into", "when"})
_KO_INCIDENT_KEYWORDS = (
    "커넥션",
    "타임아웃",
    "누수",
    "롤백",
    "배포",
    "지연",
    "메모리",
    "과부하",
    "풀",
    "오류",
    "실패",
    "장애",
    "차단",
)


class SupervisorState(TypedDict, total=False):
    """LangGraph 5단계(analyze -> plan -> execute -> evaluate -> respond) 공유 상태."""

    session_id: str
    run_id: str
    trace_id: str
    a2a_task_ids: list[str]
    last_completed_step: str
    incident_code: str
    service_name: str
    incident_title: str
    incident_summary: str
    user_message: str
    incident_started_at: str | None
    intent: str
    intent_reason: str
    constraints: dict[str, Any]
    plan_steps_order: list[str]
    evidence: list[dict[str, Any]]
    pulled_chunk_ids: list[str]
    remaining_chunk_ids: list[str]
    runbook_summary: str
    recommended_command: str | None
    cached_tool_results: dict[str, Any]
    cached_tool_results_timestamps: dict[str, float]
    remediation_result: dict[str, Any]
    replan_count: int
    should_replan: bool
    replan_history: list[dict[str, Any]]
    workflow_status: str
    snapshot: dict[str, Any]
    reply_text: str
    ui_events: list[dict[str, Any]]


def _parse_hm_range(start_hm: str, end_hm: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """HH:MM 시작/종료 문자열을 검증해 (시, 분) 튜플 쌍으로 반환하며, 유효하지 않으면 None을 반환한다."""
    try:
        sh, sm = (int(x) for x in str(start_hm).split(":"))
        eh, em = (int(x) for x in str(end_hm).split(":"))
    except ValueError:
        return None
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        return None
    if (sh, sm) == (eh, em):
        return None
    return (sh, sm), (eh, em)


def _extract_log_keywords(user_message: str, incident_title: str = "", incident_summary: str = "") -> list[str]:
    """장애 제목·요약·사용자 요청에서 동적 로그 검색 키워드를 추출하고 비어 있으면 기본값으로 폴백한다."""
    combined = " ".join(part for part in (incident_title, incident_summary, user_message) if part)
    tokens: list[str] = []
    seen_lower: set[str] = set()

    def _add(tok: str) -> None:
        key = tok.lower()
        if key in _STOPWORDS or key in seen_lower:
            return
        seen_lower.add(key)
        tokens.append(tok)

    for m in re.finditer(r"\b(?:500|502|503|504|429)\b", combined):
        _add(m.group(0))
    for m in re.finditer(r"\b[A-Za-z][A-Za-z0-9_-]{2,}\b", combined):
        _add(m.group(0))
    for ko_kw in _KO_INCIDENT_KEYWORDS:
        if ko_kw in combined:
            _add(ko_kw)

    return tokens[:6] if tokens else list(_DEFAULT_LOG_KEYWORDS)


def _approval_id_for(session_id: str, run_id: str, plan_id: str, replan_count: int, incident_code: str) -> str:
    """재개된 실행이 자신이 이미 등록한 승인을 알아볼 수 있도록 실행·계획에서 승인 ID를 결정적으로 만든다."""
    digest = hashlib.sha256(f"{session_id}|{run_id}|{plan_id}|{replan_count}".encode()).hexdigest()[:8].upper()
    return f"APR-{incident_code}-{digest}"


def _action_signature(steps: Any) -> list[tuple[int, str]]:
    return sorted(
        (int(step.get("stepNo") or 0), str(step.get("actionCommand") or ""))
        for step in (steps or [])
        if isinstance(step, dict)
    )


async def _register_plan_and_approval(
    backend: BackendClient, incident_code: str, plan_id: str, draft: dict[str, Any], approval_id: str, step_no: int
) -> tuple[dict[str, Any], str]:
    """계획과 승인을 등록하고 (승인, 워크플로 상태)를 반환한다. 이미 커밋된 행은 원장 상태를 그대로 따른다."""
    try:
        await backend.create_plan(incident_code, {"planId": plan_id, "createdBy": "supervisor-agent", "steps": draft["steps"], "verificationCriteria": draft["verificationCriteria"]})
    except BackendApiError as exc:
        saved = find_plan(await backend.get_incident_detail(incident_code), plan_id)
        if saved is None or _action_signature(saved.get("steps")) != _action_signature(draft["steps"]):
            raise exc
    try:
        await backend.create_approval(incident_code, {"approvalId": approval_id, "planId": plan_id, "stepNo": step_no, "proposedBy": "supervisor-agent"})
    except BackendApiError as exc:
        detail = await backend.get_incident_detail(incident_code)
        saved = find_approval(detail, approval_id)
        if saved is None or saved.get("planId") != plan_id:
            raise exc
        return approval_view(saved), approval_status(detail, approval_id) or "BLOCKED"
    return {"approvalId": approval_id, "planId": plan_id, "stepNo": step_no, "decision": "PENDING"}, "AWAITING_APPROVAL"


def _extract_constraints(message: str, prev: dict[str, Any], incident_started_at: str | None) -> tuple[dict[str, Any], str, str]:
    """사용자 메시지에서 의도와 제약 조건을 추출해 이전 대화 조건과 병합한다."""
    text = message.strip()
    lower = text.lower()
    merged = dict(prev or {})

    if incident_started_at:
        merged["incidentStartedAt"] = incident_started_at

    # 시간 범위(예: 14:28~14:50), 작업 시간(예: 5분), 오류율 기준(예: 1%) 추출
    range_match = _TIME_RANGE_RE.search(text)
    if range_match:
        if _parse_hm_range(range_match.group(1), range_match.group(2)) is None:
            return (
                merged,
                "invalid_time_range",
                f"허용 작업 시간 범위({range_match.group(1)}~{range_match.group(2)})가 올바르지 않습니다. 00:00~23:59 형식의 서로 다른 시작·종료 시각을 입력해 주세요.",
            )
        merged["allowedRangeHm"] = (range_match.group(1), range_match.group(2))
    dur_match = _DURATION_RE.search(text)
    if dur_match:
        dur_val = int(dur_match.group(1))
        if dur_val <= 0:
            return merged, "invalid_time_range", "작업 시간(분)은 1분 이상이어야 합니다."
        merged["durationMinutes"] = dur_val
    rate_match = _RATE_RE.search(text)
    if rate_match:
        rate_val = float(rate_match.group(1))
        if rate_val <= 0:
            return merged, "invalid_time_range", "허용 502 비율 기준(%)은 0보다 커야 합니다."
        merged["max502RatePercent"] = rate_val

    # Tool 호출이 불필요한 인사·사용법 질문 분류
    has_ops_signal = any(tok in lower for tok in ("롤백", "502", "장애", "복구", "계획", "작업 창", "시간", "분", "hikari", "커넥션", "바꿔", "다시"))
    if (not has_ops_signal and any(tok in lower for tok in _GREETING_PATTERNS)) or not text:
        return merged, "general_chat", "인사 또는 사용법 안내 요청으로 외부 Tool 호출이 불필요합니다."

    if not merged.get("incidentStartedAt"):
        return merged, "missing_info", "롤백 후보 조회 기준인 장애 시작 시각(incidentStartedAt)이 누락되었습니다."

    return merged, "plan_remediation", "장애 근거 조사 및 복구 계획 수립·검증이 필요한 요청입니다."


def _resolve_time_bounds(constraints: dict[str, Any]) -> tuple[str, str, str, str]:
    """장애 시작 시각의 날짜·오프셋을 기준으로 관측 구간과 허용 작업 범위를 RFC 3339로 계산한다."""
    started = parse_rfc3339(str(constraints["incidentStartedAt"]))
    metrics_from = (started + timedelta(minutes=1)).isoformat()
    metrics_to = (started + timedelta(minutes=6)).isoformat()

    hm_range = constraints.get("allowedRangeHm")
    parsed = (
        _parse_hm_range(str(hm_range[0]), str(hm_range[1]))
        if isinstance(hm_range, (list, tuple)) and len(hm_range) == 2
        else None
    )
    if parsed is not None:
        (sh, sm), (eh, em) = parsed
        win_start = started.replace(hour=sh, minute=sm, second=0, microsecond=0)
        win_end = started.replace(hour=eh, minute=em, second=0, microsecond=0)
        # 자정 넘김(예: 23:50~00:15)과 익일 새벽 창(예: 장애 23:50, 요청 00:10~00:30) 보정
        if win_end <= win_start:
            win_end = win_end + timedelta(days=1)
        if started - win_start > timedelta(hours=12):
            win_start = win_start + timedelta(days=1)
            win_end = win_end + timedelta(days=1)
    else:
        win_start = started + timedelta(minutes=24)
        win_end = started + timedelta(minutes=46)
    return metrics_from, metrics_to, win_start.isoformat(), win_end.isoformat()


def build_supervisor_graph(settings: Settings, http_client: httpx.AsyncClient, backend: BackendClient, event_bus: EventBus | None = None, a2a_caller: A2ACaller | None = None, on_ui_event: UiEventSink | None = None, checkpointer: Any | None = None, ledger_writes: list[asyncio.Future[Any]] | None = None, remote_tasks: dict[str, tuple[str, str]] | None = None):
    """요구사항 4.2 기준 LangGraph(analyze->plan->execute_log->execute_remediation->evaluate->respond)를 컴파일한다."""

    async def _emit(ui: list[dict[str, Any]], ev: dict[str, Any]) -> None:
        ui.append(ev)
        if on_ui_event is not None:
            await on_ui_event(ev)

    async def _call_worker(url: str, payload: dict[str, Any], context_id: str) -> dict[str, Any]:
        if a2a_caller is not None:
            return await a2a_caller(url, payload, context_id)
        token = settings.internal_service_token.get_secret_value()
        return await send_a2a_json(http_client, base_url=url, internal_token=token, payload=payload, context_id=context_id)

    async def _investigate(state: SupervisorState, request: dict[str, Any], ui: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
        """조사 워커에 위임하고 (결과, 작업 참조)를 반환한다. 실서비스 경로는 A2A 스트리밍을 사용한다."""
        sid = state["session_id"]
        url = settings.log_agent_a2a_url
        if a2a_caller is not None:
            return await a2a_caller(url, request, sid), request["idempotencyKey"]

        identified = asyncio.Event()

        def on_task_id(task_id: str) -> None:
            if remote_tasks is not None:
                remote_tasks[task_id] = (url, request["idempotencyKey"])
            identified.set()

        async def on_progress(event: dict[str, Any]) -> None:
            detail = event.get("detail")
            stage = detail.get("stage") if isinstance(detail, dict) else None
            if not stage or not event.get("taskId"):
                return
            await _emit(ui, {"kind": "a2a_progress", "value": {
                "version": 1, "incidentCode": state["incident_code"], "runId": state["run_id"],
                "worker": "log-analyzer-agent", "taskId": event["taskId"], "state": str(event.get("state") or ""),
                "stage": str(stage), "elapsedMs": event.get("elapsedMs") or 0,
            }})

        stream = asyncio.ensure_future(send_a2a_stream(
            http_client, base_url=url, internal_token=settings.log_agent_token,
            payload=request, context_id=sid, on_progress=on_progress, on_task_id=on_task_id,
        ))
        try:
            streamed = await asyncio.shield(stream)
        except asyncio.CancelledError:
            if not stream.done():
                waiting = asyncio.ensure_future(identified.wait())
                await asyncio.wait({waiting, stream}, timeout=_REMOTE_TASK_ID_GRACE_SECONDS, return_when=asyncio.FIRST_COMPLETED)
                waiting.cancel()
                stream.cancel()
                await asyncio.wait([stream])
            if not stream.cancelled():
                stream.exception()
            raise
        # 조사가 끝나면 같은 논리 작업의 대기자(단절 전 요청 포함)도 모두 끝난다.
        # 단절·취소·오류로 빠져나간 작업은 목록에 남겨 이후 명시적 취소가 중단할 수 있게 한다
        if remote_tasks is not None and not streamed.canceled:
            for task_id, (_, key) in list(remote_tasks.items()):
                if key == request["idempotencyKey"]:
                    remote_tasks.pop(task_id, None)
        return streamed.payload, streamed.task_id

    async def analyze_node(state: SupervisorState) -> SupervisorState:
        """1단계: 요청 의도 분류 및 기준·제약 조건 추출."""
        sid = state["session_id"]
        ui = list(state.get("ui_events") or [])
        await _emit(ui, {"kind": "step_start", "step": "analyze"})
        await publish_safe(event_bus, sid, "step_started", {"step": "analyze"})

        constraints, intent, reason = _extract_constraints(state.get("user_message", ""), state.get("constraints") or {}, state.get("incident_started_at"))
        service_name = state.get("service_name") or settings.default_service_name
        incident_title = state.get("incident_title") or ""
        incident_summary = state.get("incident_summary") or ""
        # 백엔드에 장애가 등록되어 있으면 서비스명·제목·요약을 동기화한다
        if intent == "plan_remediation":
            try:
                detail = await backend.get_incident_detail(state["incident_code"])
                inc = detail.get("incident") if isinstance(detail, dict) else None
                if isinstance(inc, dict):
                    if inc.get("serviceName"):
                        service_name = str(inc["serviceName"])
                    if inc.get("title"):
                        incident_title = str(inc["title"])
                    if inc.get("summary"):
                        incident_summary = str(inc["summary"])
            except BackendApiError:
                pass

        await publish_safe(event_bus, sid, "llm_decision", {"step": "analyze", "intent": intent, "reason": reason})
        await publish_safe(event_bus, sid, "step_finished", {"step": "analyze", "intent": intent})
        await _emit(ui, {"kind": "step_finish", "step": "analyze"})
        return {
            "service_name": service_name,
            "incident_title": incident_title,
            "incident_summary": incident_summary,
            "constraints": constraints,
            "intent": intent,
            "intent_reason": reason,
            "ui_events": ui,
        }

    async def plan_node(state: SupervisorState) -> SupervisorState:
        """2단계: 필요한 A2A 워커 호출 계획 수립(재계획 시 캐시 유지)."""
        sid = state["session_id"]
        ui = list(state.get("ui_events") or [])
        await _emit(ui, {"kind": "step_start", "step": "plan"})
        await publish_safe(event_bus, sid, "step_started", {"step": "plan", "replanCount": state.get("replan_count", 0)})

        steps = ["a2a:log-analyzer-agent", "a2a:remediation-agent"]
        await publish_safe(event_bus, sid, "step_finished", {"step": "plan", "steps": steps})
        await _emit(ui, {"kind": "step_finish", "step": "plan"})
        return {"plan_steps_order": steps, "should_replan": False, "last_completed_step": "plan", "ui_events": ui}

    async def execute_log_node(state: SupervisorState) -> SupervisorState:
        """3-1단계: A2A로 로그 분석(DR-DCI) 워커를 위임 실행하고 체크포인트에 근거를 저장한다."""
        sid = state["session_id"]
        trace_id = state.get("trace_id") or state["run_id"]
        ui = list(state.get("ui_events") or [])
        await _emit(ui, {"kind": "step_start", "step": "execute"})
        await publish_safe(event_bus, sid, "step_started", {"step": "execute", "replanCount": state.get("replan_count", 0)})

        pulled_ids = list(state.get("pulled_chunk_ids") or [])
        evidence = list(state.get("evidence") or [])
        runbook_summary = state.get("runbook_summary") or ""
        recommended_cmd = state.get("recommended_command")
        remaining_ids = list(state.get("remaining_chunk_ids") or [])
        a2a_task_ids = list(state.get("a2a_task_ids") or [])

        # 로그 분석 A2A 호출 (이미 인출한 청크는 제외해 추가 조회만 수행; 멱등 키 및 traceId 전달)
        keywords = _extract_log_keywords(
            state.get("user_message", ""),
            state.get("incident_title", ""),
            state.get("incident_summary", ""),
        )
        replan_count = int(state.get("replan_count", 0))
        log_task_key = f"a2a-log-{state['run_id']}-r{replan_count}"
        log_req = {
            "incidentCode": state["incident_code"],
            "keywords": keywords,
            "excludeChunkIds": pulled_ids,
            "maxPulls": 2 if pulled_ids else settings.dr_dci_max_pull_steps,
            "idempotencyKey": log_task_key,
            "traceId": trace_id,
        }
        log_call_id = f"tc-log-{uuid.uuid4().hex[:6]}"
        t0 = time.perf_counter()
        try:
            log_res, task_ref = await _investigate(state, log_req, ui)
        except Exception:
            log_res, task_ref = {"ok": False, "error": {"code": "A2A_ERROR", "message": "로그 분석 에이전트 호출에 실패했습니다."}, "evidence": []}, None
        if task_ref:
            a2a_task_ids.append(task_ref)
        if log_res.get("ok") and log_res.get("incidentCode") not in (None, state["incident_code"]):
            # 다른 장애의 근거는 계획에 섞지 않는다
            log_res = {"ok": False, "error": {"code": "A2A_INCIDENT_MISMATCH", "message": "다른 장애의 조사 결과를 폐기했습니다."}, "evidence": []}
        log_ms = round((time.perf_counter() - t0) * 1000, 1)
        await publish_safe(event_bus, sid, "tool_call", {"tool": "a2a:log-analyzer-agent", "args": {"incidentCode": state["incident_code"], "excludeCount": len(pulled_ids)}, "ok": bool(log_res.get("ok")), "elapsedMs": log_ms})
        await _emit(ui, {"kind": "tool_call", "id": log_call_id, "name": "log-analyzer-agent", "args": log_req, "result": {"ok": bool(log_res.get("ok")), "pulledCount": log_res.get("pulledCount", 0), "chunkIds": log_res.get("evidenceChunkIds", [])}})

        if log_res.get("ok"):
            existing_set = set(pulled_ids)
            for item in log_res.get("evidence") or []:
                cid = item.get("chunkId")
                if cid and cid not in existing_set:
                    existing_set.add(cid)
                    pulled_ids.append(cid)
                    evidence.append(item)
            if log_res.get("runbookSummary"):
                runbook_summary = (runbook_summary + "\n" + str(log_res["runbookSummary"])).strip()
            if not recommended_cmd and log_res.get("recommendedCommand"):
                recommended_cmd = str(log_res["recommendedCommand"])
            remaining_ids = list(log_res.get("remainingChunkIds") or [])

        return {
            "evidence": evidence,
            "pulled_chunk_ids": pulled_ids,
            "remaining_chunk_ids": remaining_ids,
            "runbook_summary": runbook_summary,
            "recommended_command": recommended_cmd,
            "a2a_task_ids": a2a_task_ids,
            "last_completed_step": "execute_log",
            "ui_events": ui,
        }

    async def execute_remediation_node(state: SupervisorState) -> SupervisorState:
        """3-2단계: 체크포인트에 보존된 근거와 캐시로 복구 계획(외부 Tool 3종+Decisions) 워커를 위임 실행한다."""
        sid = state["session_id"]
        ui = list(state.get("ui_events") or [])
        evidence = list(state.get("evidence") or [])
        runbook_summary = state.get("runbook_summary") or ""
        recommended_cmd = state.get("recommended_command")
        a2a_task_ids = list(state.get("a2a_task_ids") or [])

        metrics_from, metrics_to, win_start, win_end = _resolve_time_bounds(state["constraints"])
        raw_cache = dict(state.get("cached_tool_results") or {})
        raw_timestamps = dict(state.get("cached_tool_results_timestamps") or {})
        now_ts = time.time()
        ttl_s = settings.tool_cache_ttl_seconds
        # 체크포인트 재개 시에도 TTL(tool_cache_ttl_seconds)이 지난 캐시 항목은 만료 처리해 최신 변경 금지 시간 등을 재조회한다
        cached_tools: dict[str, Any] = {}
        fresh_timestamps: dict[str, float] = {}
        for k, v in raw_cache.items():
            saved_at = raw_timestamps.get(k)
            if saved_at is None or (now_ts - float(saved_at)) <= ttl_s:
                cached_tools[k] = v
                if saved_at is not None:
                    fresh_timestamps[k] = float(saved_at)
        rem_task_key = f"a2a-rem-{state['run_id']}-r{int(state.get('replan_count', 0))}"
        a2a_task_ids.append(rem_task_key)
        rem_req = {
            "action": "plan",
            "incidentCode": state["incident_code"],
            "serviceName": state["service_name"],
            "incidentStartedAt": state["constraints"]["incidentStartedAt"],
            "metricsFrom": metrics_from,
            "metricsTo": metrics_to,
            "allowedWindowStart": win_start,
            "allowedWindowEnd": win_end,
            "durationMinutes": int(state["constraints"].get("durationMinutes", 5)),
            "max502RatePercent": float(state["constraints"].get("max502RatePercent", 1.0)),
            "evidence": [{"chunkId": e["chunkId"], "summary": e["summary"], "corpusType": e.get("corpusType", "LOG")} for e in evidence],
            "runbookSummary": runbook_summary,
            "recommendedCommand": recommended_cmd,
            "cachedToolResults": cached_tools,
        }
        rem_call_id = f"tc-rem-{uuid.uuid4().hex[:6]}"
        t1 = time.perf_counter()
        try:
            rem_res = await _call_worker(settings.remediation_agent_a2a_url, rem_req, sid)
        except Exception:
            rem_res = {"ok": False, "status": "BLOCKED", "message": "복구 계획 에이전트 호출에 실패했습니다.", "toolCalls": [], "cachedToolResults": cached_tools}
        rem_ms = round((time.perf_counter() - t1) * 1000, 1)

        # 워커 내부 외부 Tool 3종 호출 이력도 로깅 이벤트와 AG-UI에 기록한다
        for tc in rem_res.get("toolCalls") or []:
            await publish_safe(event_bus, sid, "tool_call", tc)
            await _emit(ui, {"kind": "tool_call", "id": f"tc-{uuid.uuid4().hex[:6]}", "name": tc.get("tool", "external_tool"), "args": tc.get("args", {}), "result": {"ok": tc.get("ok"), "errorCode": tc.get("errorCode"), "elapsedMs": tc.get("elapsedMs")}})

        await publish_safe(
            event_bus,
            sid,
            "tool_call",
            {
                "tool": "a2a:remediation-agent",
                "args": {
                    "incidentCode": state["incident_code"],
                    "evidenceChunkIds": sorted(e["chunkId"] for e in evidence),
                    "allowedWindowStart": win_start,
                    "allowedWindowEnd": win_end,
                },
                "ok": bool(rem_res.get("ok")),
                "status": rem_res.get("status"),
                "verdict": rem_res.get("verdict"),
                "elapsedMs": rem_ms,
            },
        )
        await _emit(ui, {"kind": "tool_call", "id": rem_call_id, "name": "remediation-agent", "args": {"incidentCode": state["incident_code"], "serviceName": state["service_name"]}, "result": {"status": rem_res.get("status"), "verdict": rem_res.get("verdict")}})

        await publish_safe(event_bus, sid, "step_finished", {"step": "execute"})
        await _emit(ui, {"kind": "step_finish", "step": "execute"})
        updated_cache = rem_res.get("cachedToolResults") or cached_tools
        end_ts = time.time()
        updated_timestamps = {
            k: (fresh_timestamps[k] if k in cached_tools and cached_tools[k] == v and k in fresh_timestamps else end_ts)
            for k, v in updated_cache.items()
        }
        return {
            "cached_tool_results": updated_cache,
            "cached_tool_results_timestamps": updated_timestamps,
            "remediation_result": rem_res,
            "a2a_task_ids": a2a_task_ids,
            "last_completed_step": "execute_remediation",
            "ui_events": ui,
        }

    async def evaluate_node(state: SupervisorState) -> SupervisorState:
        """4단계: 제약 충족·근거 판정 평가, 재계획 상한 검사, 승인 원장 등록(POST /plans -> POST /approvals)."""
        sid = state["session_id"]
        ui = list(state.get("ui_events") or [])
        await _emit(ui, {"kind": "step_start", "step": "evaluate"})
        await publish_safe(event_bus, sid, "step_started", {"step": "evaluate"})

        rem = state.get("remediation_result") or {}
        status_val = rem.get("status", "BLOCKED")
        replan_count = int(state.get("replan_count", 0))
        replan_history = list(state.get("replan_history") or [])

        # 시간 충돌 재계획이 발생했으면 CUSTOM replan 이벤트와 이력에 기록한다
        replan_ev = rem.get("replanEvent")
        if replan_ev and replan_ev not in replan_history:
            replan_history.append(replan_ev)
            await _emit(ui, {"kind": "replan", "value": replan_ev})
            await publish_safe(event_bus, sid, "replan", replan_ev)

        # 근거 부족(NEED_MORE_EVIDENCE) 시 남은 청크와 재계획 상한 내에서 재조회 루프로 분기한다
        if status_val == "NEED_MORE_EVIDENCE":
            if replan_count < settings.max_replan_attempts and state.get("remaining_chunk_ids"):
                next_count = replan_count + 1
                await publish_safe(event_bus, sid, "llm_decision", {"step": "evaluate", "decision": "replan", "replanCount": next_count, "reason": "근거 부족(NEED_MORE_EVIDENCE)으로 범위 내 추가 청크 조회를 재계획합니다."})
                await publish_safe(event_bus, sid, "step_finished", {"step": "evaluate", "shouldReplan": True})
                await _emit(ui, {"kind": "step_finish", "step": "evaluate"})
                return {"replan_count": next_count, "should_replan": True, "replan_history": replan_history, "ui_events": ui}

            snapshot = {"incidentCode": state["incident_code"], "workflowStatus": "BLOCKED", "plan": None, "approval": None, "replanHistory": replan_history}
            await publish_safe(event_bus, sid, "step_finished", {"step": "evaluate", "workflowStatus": "BLOCKED", "reason": "replan_limit_or_no_chunks"})
            await _emit(ui, {"kind": "step_finish", "step": "evaluate"})
            return {"should_replan": False, "workflow_status": "BLOCKED", "snapshot": snapshot, "replan_history": replan_history, "ui_events": ui}

        if status_val == "READY_FOR_APPROVAL" and rem.get("planDraft"):
            draft = dict(rem["planDraft"])
            plan_id = draft["planId"]
            step_no = int(draft.get("approvalStepNo") or 2)
            approval_id = _approval_id_for(sid, state["run_id"], plan_id, replan_count, state["incident_code"])

            # 미완료 계획 연결 승인이 이미 있으면 POST /plans로 고아 계획을 남기지 않고 즉시 차단한다
            try:
                detail = await backend.get_incident_detail(state["incident_code"])
            except BackendApiError:
                detail = None
            own_apr = find_approval(detail, approval_id)
            if own_apr is not None and own_apr.get("planId") == plan_id:
                # 이 실행이 중단 전에 이미 등록한 승인: 새로 쓰지 않고 원장 상태를 그대로 보여 준다.
                wf_status = approval_status(detail, approval_id) or "BLOCKED"
                snapshot = {"incidentCode": state["incident_code"], "workflowStatus": wf_status, "plan": {k: v for k, v in draft.items() if k != "approvalStepNo"}, "approval": approval_view(own_apr), "replanHistory": replan_history}
                await publish_safe(event_bus, sid, "step_finished", {"step": "evaluate", "workflowStatus": wf_status, "reason": "approval_already_registered"})
                await _emit(ui, {"kind": "step_finish", "step": "evaluate"})
                return {"should_replan": False, "workflow_status": wf_status, "snapshot": snapshot, "replan_history": replan_history, "ui_events": ui}

            existing_apr = find_open_approval(detail)
            if existing_apr is not None:
                existing_aid = str(existing_apr.get("approvalId") or "")
                existing_decision = str(existing_apr.get("decision") or "")
                if existing_decision == "PENDING":
                    msg = f"이미 처리 대기 중인 승인 요청({existing_aid})이 있어 새 계획을 등록하지 않았습니다. 기존 승인 요청을 검토·반려한 뒤 다시 진행해 주세요."
                else:
                    msg = f"이미 승인된 미실행 요청({existing_aid})이 있어 새 계획을 등록하지 않았습니다. 해당 승인 건의 실행 완료를 먼저 처리해 주세요."
                rem = {**rem, "message": msg}
                snapshot = {"incidentCode": state["incident_code"], "workflowStatus": "BLOCKED", "plan": None, "approval": None, "replanHistory": replan_history}
                await publish_safe(event_bus, sid, "step_finished", {"step": "evaluate", "workflowStatus": "BLOCKED", "reason": "unfinished_approval_exists"})
                await _emit(ui, {"kind": "step_finish", "step": "evaluate"})
                return {"should_replan": False, "workflow_status": "BLOCKED", "snapshot": snapshot, "remediation_result": rem, "replan_history": replan_history, "ui_events": ui}

            # Spring 백엔드에 복구 계획과 승인 대기 요청을 등록한다 (MCP 미노출 REST 2종)
            # 원장 쓰기는 취소와 분리한다. 취소 처리기는 이 작업이 끝난 뒤에 원장을 읽는다.
            writes = asyncio.ensure_future(_register_plan_and_approval(backend, state["incident_code"], plan_id, draft, approval_id, step_no))
            if ledger_writes is not None:
                ledger_writes.append(writes)
            try:
                try:
                    approval, wf_status = await asyncio.shield(writes)
                except asyncio.CancelledError:
                    await asyncio.wait([writes])
                    if not writes.cancelled():
                        writes.exception()  # 취소가 우선이므로 쓰기 오류는 원장 재조회로 확인한다
                    raise
            except BackendApiError as exc:
                approval = None
                wf_status = "BLOCKED"
                rem = {**rem, "message": f"백엔드 계획/승인 등록 실패: {exc.message}"}

            snapshot = {"incidentCode": state["incident_code"], "workflowStatus": wf_status, "plan": {k: v for k, v in draft.items() if k != "approvalStepNo"}, "approval": approval, "replanHistory": replan_history}
            await publish_safe(event_bus, sid, "step_finished", {"step": "evaluate", "workflowStatus": wf_status})
            await _emit(ui, {"kind": "step_finish", "step": "evaluate"})
            return {"should_replan": False, "workflow_status": wf_status, "snapshot": snapshot, "remediation_result": rem, "replan_history": replan_history, "ui_events": ui}

        if status_val == "REVISED_NO_ROLLBACK":
            snapshot = {"incidentCode": state["incident_code"], "workflowStatus": "BLOCKED", "plan": rem.get("planDraft"), "approval": None, "replanHistory": replan_history}
            await publish_safe(event_bus, sid, "step_finished", {"step": "evaluate", "workflowStatus": "BLOCKED", "verdict": "CONTRADICTED"})
            await _emit(ui, {"kind": "step_finish", "step": "evaluate"})
            return {"should_replan": False, "workflow_status": "BLOCKED", "snapshot": snapshot, "replan_history": replan_history, "ui_events": ui}

        snapshot = {"incidentCode": state["incident_code"], "workflowStatus": "BLOCKED", "plan": None, "approval": None, "replanHistory": replan_history}
        await publish_safe(event_bus, sid, "step_finished", {"step": "evaluate", "workflowStatus": "BLOCKED"})
        await _emit(ui, {"kind": "step_finish", "step": "evaluate"})
        return {"should_replan": False, "workflow_status": "BLOCKED", "snapshot": snapshot, "replan_history": replan_history, "ui_events": ui}

    async def respond_node(state: SupervisorState) -> SupervisorState:
        """5단계: 근거·판단 기준·제외 사유·재계획 내역을 포함한 최종 응답을 생성한다."""
        sid = state["session_id"]
        ui = list(state.get("ui_events") or [])
        await _emit(ui, {"kind": "step_start", "step": "respond"})
        await publish_safe(event_bus, sid, "step_started", {"step": "respond"})

        intent = state.get("intent")
        if intent == "general_chat":
            reply = "안녕하세요! Incident-Ops-Agent입니다. 장애 코드와 조건(예: 이전 버전 502 비율 기준, 허용 작업 시간)을 알려주시면 로그 조사부터 롤백 계획·승인 요청까지 도와드립니다."
            snapshot = state.get("snapshot") or {"incidentCode": state["incident_code"], "workflowStatus": "IDLE", "plan": None, "approval": None, "replanHistory": list(state.get("replan_history") or [])}
        elif intent == "missing_info":
            reply = "롤백 후보 배포를 조회하려면 장애 시작 시각(`incidentStartedAt`, RFC 3339 형식)이 필요합니다. 장애 발생 시각을 알려주시면 분석을 시작하겠습니다."
            snapshot = {"incidentCode": state["incident_code"], "workflowStatus": "IDLE", "plan": None, "approval": None, "replanHistory": list(state.get("replan_history") or [])}
        elif intent == "invalid_time_range":
            reply = state.get("intent_reason") or "허용 작업 시간 또는 조건 입력이 올바르지 않습니다. 다시 확인해 주세요."
            snapshot = state.get("snapshot") or {"incidentCode": state["incident_code"], "workflowStatus": "IDLE", "plan": None, "approval": None, "replanHistory": list(state.get("replan_history") or [])}
        else:
            rem = state.get("remediation_result") or {}
            snapshot = state.get("snapshot") or {"incidentCode": state["incident_code"], "workflowStatus": "BLOCKED", "plan": None, "approval": None, "replanHistory": list(state.get("replan_history") or [])}
            parts = [rem.get("message") or "복구 계획 평가를 완료했습니다."]
            sel = rem.get("selectedCandidate")
            if sel:
                parts.append(f"- 선택 후보: `{sel.get('ref', sel.get('gitSha'))}` (502 비율 `{sel.get('http502RatePercent')}%`)")
            if snapshot.get("plan") and snapshot["plan"].get("workWindow"):
                ww = snapshot["plan"]["workWindow"]
                parts.append(f"- 작업 창: `{ww['start']}` ~ `{ww['end']}`")
            if rem.get("replanEvent"):
                prev_w = rem["replanEvent"]["previousWindow"]
                parts.append(f"- 재계획 사유: {rem['replanEvent']['reason']} (초안 `{prev_w['start']}~{prev_w['end']}` 제외)")
            if rem.get("rejectedCandidates"):
                reasons = ", ".join(f"{r.get('gitSha')}: {r.get('reason')}" for r in rem["rejectedCandidates"])
                parts.append(f"- 제외 후보: {reasons}")
            if rem.get("remainingGaps") and snapshot.get("workflowStatus") == "BLOCKED":
                gaps_txt = ", ".join(f"{g['start']}~{g['end']}" for g in rem["remainingGaps"])
                parts.append(f"- 남은 가용 시간대: {gaps_txt} (허용 범위를 조정해 다시 요청해 주세요.)")
            if state.get("pulled_chunk_ids"):
                parts.append(f"- 인용 근거 청크: {', '.join(state['pulled_chunk_ids'])}")
            if snapshot.get("workflowStatus") == "AWAITING_APPROVAL":
                parts.append("- 운영자 검토 후 승인/반려를 선택해 주세요.")
            elif state.get("replan_count", 0) >= settings.max_replan_attempts and rem.get("status") == "NEED_MORE_EVIDENCE":
                parts.append(f"- 재계획 상한({settings.max_replan_attempts}회)에 도달해 중단했습니다. 조건이나 추가 로그를 확인해 주세요.")
            reply = "\n".join(parts)

        # 공통 종단 트레이스 메타데이터(AG-UI runId -> A2A taskIds -> MCP chunkIds -> Spring approvalId)를 스냅샷에 연결한다
        approval_obj = snapshot.get("approval") if isinstance(snapshot, dict) else None
        snapshot = {
            **snapshot,
            "trace": {
                "runId": state.get("run_id"),
                "traceId": state.get("trace_id") or state.get("run_id"),
                "a2aTaskIds": list(state.get("a2a_task_ids") or []),
                "evidenceChunkIds": list(state.get("pulled_chunk_ids") or []),
                "approvalId": approval_obj.get("approvalId") if isinstance(approval_obj, dict) else None,
            },
        }
        await publish_safe(event_bus, sid, "step_finished", {"step": "respond", "workflowStatus": snapshot.get("workflowStatus")})
        await _emit(ui, {"kind": "step_finish", "step": "respond"})
        return {"reply_text": reply, "snapshot": snapshot, "workflow_status": snapshot.get("workflowStatus", "IDLE"), "last_completed_step": "respond", "ui_events": ui}

    def _route_after_analyze(state: SupervisorState) -> str:
        return "respond" if state.get("intent") in ("general_chat", "missing_info", "invalid_time_range") else "plan"

    def _route_after_evaluate(state: SupervisorState) -> str:
        return "plan" if state.get("should_replan") else "respond"

    graph = StateGraph(SupervisorState)
    graph.add_node("analyze", analyze_node)
    graph.add_node("plan", plan_node)
    graph.add_node("execute_log", execute_log_node)
    graph.add_node("execute_remediation", execute_remediation_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("respond", respond_node)

    graph.add_edge(START, "analyze")
    graph.add_conditional_edges("analyze", _route_after_analyze, {"plan": "plan", "respond": "respond"})
    graph.add_edge("plan", "execute_log")
    graph.add_edge("execute_log", "execute_remediation")
    graph.add_edge("execute_remediation", "evaluate")
    graph.add_conditional_edges("evaluate", _route_after_evaluate, {"plan": "plan", "respond": "respond"})
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer)
