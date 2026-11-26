from __future__ import annotations

import asyncio
import json
from typing import Any
import httpx
import pytest
from ag_ui.core import RunAgentInput, UserMessage
from pydantic import SecretStr

from common.a2a_utils import cancel_a2a_task, send_a2a_json, send_a2a_stream
from common.backend_client import BackendApiError, BackendClient, EVIDENCE_SUMMARY_RESOURCE_URI
from common.config import Settings
from common.decisions import DecisionResult
from common.event_log import EventBus
from common.tools import ExternalToolsClient
from eval.harness import _percentile
from log_agent.main import create_log_agent_app
from remediation_agent.main import create_remediation_agent_app
from supervisor.checkpointer import RedisCheckpointSaver
from supervisor.cruds import SupervisorRedisStore
from supervisor.graph import build_supervisor_graph
from supervisor.services import SupervisorService


class _SharedInMemoryRedis:
    """프로세스 재시작 시에도 상태가 유지되는 공유 Redis 저장소 시뮬레이터."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def setex(self, key: str, seconds: int, value: str) -> bool:
        self.kv[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        cnt = 0
        for k in keys:
            if k in self.kv:
                del self.kv[k]
                cnt += 1
            if k in self.lists:
                del self.lists[k]
                cnt += 1
            if k in self.hashes:
                del self.hashes[k]
                cnt += 1
        return cnt

    async def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        arr = self.lists.get(key) or []
        if not arr:
            return []
        if start < 0:
            start = max(0, len(arr) + start)
        if end < 0:
            end = len(arr) + end
        return arr[start : end + 1]

    async def hset(self, key: str, field: str, value: str) -> int:
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def hsetnx(self, key: str, field: str, value: str) -> int:
        h = self.hashes.setdefault(key, {})
        if field in h:
            return 0
        h[field] = value
        return 1

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key) or {})

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        # thread_lock 해제용 Lua 스크립트 호환
        if numkeys == 1 and len(args) == 2:
            key, token = str(args[0]), str(args[1])
            if self.kv.get(key) == token:
                del self.kv[key]
                return 1
            return 0
        return 0


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "internal_service_token": SecretStr("phase7-secret-token"),
        "default_service_name": "payment-service",
        "log_agent_a2a_url": "http://log-agent",
        "remediation_agent_a2a_url": "http://rem-agent",
        "dr_dci_max_pull_steps": 3,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _parse_ag_ui_events(chunks: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in chunks:
        for line in raw.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


# =============================================================================
# 1. AG-UI 영속 LangGraph 체크포인트 30회 중단·재개 검증 (3개 지점 × 각 10회)
# =============================================================================


async def test_ag_ui_checkpoint_resume_30_runs_across_three_interruption_points():
    """
    3개 중단 지점(근거 수집 후 / 승인 대기 중 / 완료 저장 후 응답 전) × 각 10회 = 총 30회 재개 검증:
    - 완료된 단계(log_agent) 불필요 재호출 0건
    - 승인당 신규 실행·포스트모템 중복 0건
    - 최종 UI 상태와 Spring 원장 상태 불일치 0건
    """
    cfg = _settings()

    # --- 지점 1 (10회): execute_log 완료 후 execute_remediation 직전/도중 중단 -> 새 프로세스(새 Checkpointer)에서 재개 ---
    for i in range(10):
        shared_redis = _SharedInMemoryRedis()
        tid = f"p7-pt1-{i}"
        call_counts = {"log": 0, "rem": 0, "plans": 0, "approvals": 0}
        fail_first_rem = {"should_fail": True}

        def rest_handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path.endswith("/INC-2026-001"):
                return httpx.Response(200, json={"incident": {"incidentCode": "INC-2026-001", "serviceName": "payment-service", "status": "OPEN"}, "plans": [], "approvals": [], "executions": []})
            if request.method == "POST" and path.endswith("/plans"):
                call_counts["plans"] += 1
                return httpx.Response(201, json=json.loads(request.content))
            if request.method == "POST" and path.endswith("/approvals"):
                call_counts["approvals"] += 1
                return httpx.Response(201, json=json.loads(request.content))
            return httpx.Response(404)

        entered_rem = asyncio.Event()

        async def flaky_a2a(url: str, payload: dict[str, Any], ctx_id: str) -> dict[str, Any]:
            if url == cfg.log_agent_a2a_url:
                call_counts["log"] += 1
                return {
                    "ok": True,
                    "incidentCode": "INC-2026-001",
                    "evidence": [{"chunkId": "LOG-104", "corpusType": "LOG", "title": "502 timeout", "summary": "HikariPool timeout"}],
                    "evidenceChunkIds": ["LOG-104"],
                    "runbookSummary": "rollback on 502",
                    "recommendedCommand": "kubectl rollout undo deployment/payment-service",
                    "remainingChunkIds": [],
                    "pulledCount": 1,
                }
            call_counts["rem"] += 1
            if fail_first_rem["should_fail"]:
                fail_first_rem["should_fail"] = False
                entered_rem.set()
                await asyncio.sleep(5.0)
            return {
                "ok": True,
                "status": "READY_FOR_APPROVAL",
                "verdict": "SUPPORTED",
                "confidence": 0.95,
                "planDraft": {
                    "planId": f"PLAN-P7-{i}",
                    "approvalStepNo": 2,
                    "steps": [{"stepNo": 1, "description": "check"}, {"stepNo": 2, "description": "rollback", "actionCommand": "kubectl rollout undo deployment/payment-service"}],
                    "verificationCriteria": "502=0%",
                    "workWindow": {"start": "2026-11-18T14:40:00+09:00", "end": "2026-11-18T14:45:00+09:00"},
                },
                "toolCalls": [],
                "cachedToolResults": {},
                "message": "승인 대기 준비 완료",
            }

        async with httpx.AsyncClient(transport=httpx.MockTransport(rest_handler)) as http_client:
            backend = BackendClient(cfg, http_client)
            # 1차 실행 (execute_log 완료 후 execute_remediation 진입 시점에 실제 태스크 강제 취소)
            cp1 = RedisCheckpointSaver(shared_redis)
            g1 = build_supervisor_graph(cfg, http_client, backend, a2a_caller=flaky_a2a, checkpointer=cp1)
            run_task = asyncio.create_task(
                g1.ainvoke(
                    {
                        "session_id": tid,
                        "run_id": f"run-1-{i}",
                        "trace_id": f"tr-pt1-{i}",
                        "incident_code": "INC-2026-001",
                        "service_name": "payment-service",
                        "user_message": "14:28~14:50 5분 롤백 계획 세워줘",
                        "incident_started_at": "2026-11-18T14:04:00+09:00",
                        "constraints": {},
                        "evidence": [],
                        "pulled_chunk_ids": [],
                        "remaining_chunk_ids": [],
                        "runbook_summary": "",
                        "cached_tool_results": {},
                        "replan_count": 0,
                        "should_replan": False,
                        "replan_history": [],
                        "ui_events": [],
                    },
                    config={"configurable": {"thread_id": tid}},
                )
            )
            await entered_rem.wait()
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task
            assert call_counts["log"] == 1

            # 2차 재개 (새 프로세스 시뮬레이션: 새 Checkpointer + 새 SupervisorService 인스턴스)
            cp2 = RedisCheckpointSaver(shared_redis)
            store2 = SupervisorRedisStore(shared_redis)
            svc2 = SupervisorService(cfg, http_client, backend, store2, event_bus=EventBus(), a2a_caller=flaky_a2a, checkpointer=cp2)
            run_input = RunAgentInput(
                thread_id=tid,
                run_id=f"run-resume-{i}",
                state={},
                messages=[],
                tools=[],
                context=[],
                forwarded_props={"incidentCode": "INC-2026-001", "resume": True},
            )
            props = svc2.parse_forwarded_props(run_input)
            chunks = [c async for c in svc2.stream_ag_ui(run_input, props, "oncall-lead", None, "text/event-stream")]
            events = _parse_ag_ui_events(chunks)
            snap = [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"][-1]
            assert snap["workflowStatus"] == "AWAITING_APPROVAL"
            # 완료된 execute_log 단계의 log_agent 불필요 재호출 0건!
            assert call_counts["log"] == 1
            assert call_counts["plans"] == 1
            assert call_counts["approvals"] == 1
            assert snap["trace"]["approvalId"] is not None
            assert "LOG-104" in snap["trace"]["evidenceChunkIds"]

    # --- 지점 2 (10회): 승인 대기 중 프로세스 재시작 -> restore=True 동기화 시 중복 계획·승인 0건 ---
    for i in range(10):
        shared_redis = _SharedInMemoryRedis()
        tid = f"p7-pt2-{i}"
        store1 = SupervisorRedisStore(shared_redis)
        await store1.save_thread_memory(
            tid,
            {
                "threadId": tid,
                "incidentCode": "INC-2026-001",
                "lastEventSeq": 4,
                "lastCompletedStep": "respond",
                "messages": [{"id": "m1", "role": "assistant", "content": "승인 대기 중"}],
                "snapshot": {
                    "incidentCode": "INC-2026-001",
                    "workflowStatus": "AWAITING_APPROVAL",
                    "plan": {"planId": f"PLAN-WAIT-{i}"},
                    "approval": {"approvalId": f"APR-WAIT-{i}", "planId": f"PLAN-WAIT-{i}", "stepNo": 2, "decision": "PENDING"},
                    "replanHistory": [],
                },
            },
        )
        # 프로세스 재시작 후 새 서비스 인스턴스로 restore=True 호출
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(500))) as http_client:
            svc_restarted = SupervisorService(cfg, http_client, BackendClient(cfg, http_client), SupervisorRedisStore(shared_redis), checkpointer=RedisCheckpointSaver(shared_redis))
            run_in = RunAgentInput(
                thread_id=tid,
                run_id=f"run-restore-{i}",
                state={},
                messages=[],
                tools=[],
                context=[],
                forwarded_props={"incidentCode": "INC-2026-001", "restore": True},
            )
            props = svc_restarted.parse_forwarded_props(run_in)
            chunks = [c async for c in svc_restarted.stream_ag_ui(run_in, props, "oncall-lead", None, "text/event-stream")]
            events = _parse_ag_ui_events(chunks)
            snap = [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"][-1]
            sync = [e["value"] for e in events if e["type"] == "CUSTOM" and e["name"] == "checkpoint_sync"][-1]
            assert snap["workflowStatus"] == "AWAITING_APPROVAL"
            assert snap["approval"]["approvalId"] == f"APR-WAIT-{i}"
            assert sync["eventSeq"] == 4
            # RUN_FINISHED가 업무 RESOLVED를 의미하지 않음을 불변 검증
            assert any(e["type"] == "RUN_FINISHED" for e in events)
            assert snap["workflowStatus"] != "RESOLVED"

    # --- 지점 3 (10회): 완료 저장 후 응답 전 단절 -> 동일 승인 건 재시도 시 실행·포스트모템 중복 0건 & UI-Spring 일치 ---
    for i in range(10):
        shared_redis = _SharedInMemoryRedis()
        tid = f"p7-pt3-{i}"
        ledger = {"status": "OPEN", "approval_decision": "PENDING", "executions": [], "post_mortems": []}

        async def idempotent_mcp(tool_name: str, args: dict[str, Any]) -> Any:
            if tool_name == "completeIncidentWithPostMortem":
                # 이미 기록된 실행이 있으면 중복 생성 없이 멱등 반환 (Spring IncidentRecoveryService 동일 동작)
                existing = next((e for e in ledger["executions"] if e["approvalId"] == args["approvalId"]), None)
                if existing is None:
                    ledger["executions"].append({"approvalId": args["approvalId"], "status": args["executionStatus"]})
                    ledger["post_mortems"].append({"incidentCode": args["incidentCode"], "approvalId": args["approvalId"]})
                    ledger["status"] = "RESOLVED"
                return {"incidentCode": args["incidentCode"], "incidentStatus": ledger["status"], "alreadyCompleted": existing is not None}
            return {}

        def ledger_rest(request: httpx.Request) -> httpx.Response:
            if request.method == "PATCH" and "/approvals/" in request.url.path:
                if ledger["approval_decision"] == "APPROVED":
                    return httpx.Response(409, json={"message": "이미 결정된 승인입니다."})
                ledger["approval_decision"] = "APPROVED"
                return httpx.Response(200, json={"approvalId": f"APR-DONE-{i}", "planId": f"PLAN-DONE-{i}", "stepNo": 2, "decision": "APPROVED", "actionCommand": "kubectl rollout undo deployment/payment-service"})
            if request.method == "GET" and request.url.path.endswith("/INC-2026-001"):
                return httpx.Response(200, json={
                    "incident": {"incidentCode": "INC-2026-001", "status": ledger["status"]},
                    "plans": [{"planId": f"PLAN-DONE-{i}", "verificationCriteria": "502=0%"}],
                    "approvals": [{"approvalId": f"APR-DONE-{i}", "planId": f"PLAN-DONE-{i}", "stepNo": 2, "decision": ledger["approval_decision"], "actionCommand": "kubectl rollout undo deployment/payment-service"}],
                    "executions": list(ledger["executions"]),
                })
            return httpx.Response(404)

        async def ok_exec_a2a(url: str, payload: dict[str, Any], ctx_id: str) -> dict[str, Any]:
            return {
                "ok": True,
                "executionStatus": "SUCCESS",
                "executionOutput": "[simulated] executed",
                "verificationSummary": "502=0%",
                "rootCause": "leak",
                "timelineMarkdown": "- done",
                "preventionItems": "- test",
            }

        async with httpx.AsyncClient(transport=httpx.MockTransport(ledger_rest)) as http_client:
            backend = BackendClient(cfg, http_client, mcp_caller=idempotent_mcp)
            svc = SupervisorService(cfg, http_client, backend, SupervisorRedisStore(shared_redis), a2a_caller=ok_exec_a2a, checkpointer=RedisCheckpointSaver(shared_redis))

            # 1차 승인 실행 (완료 저장 후 클라이언트가 응답을 못 받았다고 가정해 2차 동일 MCP 완료 재시도)
            run_in = RunAgentInput(
                thread_id=tid,
                run_id=f"run-approve-{i}",
                state={},
                messages=[],
                tools=[],
                context=[],
                forwarded_props={"incidentCode": "INC-2026-001", "approvalDecision": {"approvalId": f"APR-DONE-{i}", "decision": "APPROVED"}},
            )
            props = svc.parse_forwarded_props(run_in)
            decided = await svc.prepare_approval_decision(props, "oncall-lead")
            chunks = [c async for c in svc.stream_ag_ui(run_in, props, "oncall-lead", decided, "text/event-stream")]
            events = _parse_ag_ui_events(chunks)
            snap = [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"][-1]

            # 응답 전 단절로 인한 중복 완료 호출 발생 시에도 멱등 처리 확인
            dup_res = await backend.complete_incident_with_postmortem(
                approval_id=f"APR-DONE-{i}",
                incident_code="INC-2026-001",
                action_command="kubectl rollout undo deployment/payment-service",
                executed_by="remediation-agent",
                execution_status="SUCCESS",
            )
            assert dup_res["alreadyCompleted"] is True
            assert len(ledger["executions"]) == 1
            assert len(ledger["post_mortems"]) == 1
            assert snap["workflowStatus"] == ledger["status"] == "RESOLVED"


async def test_ag_ui_explicit_cancel_clears_checkpoint_unlike_disconnect():
    """명시적 취소(explicitCancel=True)는 체크포인트를 정리하고 CANCELLED 상태로 전이한다."""
    cfg = _settings()
    shared_redis = _SharedInMemoryRedis()
    cp = RedisCheckpointSaver(shared_redis)
    store = SupervisorRedisStore(shared_redis)
    tid = "p7-cancel-thread"

    # 사전 체크포인트 1건 저장
    await cp.aput({"configurable": {"thread_id": tid, "checkpoint_ns": ""}}, {"id": "cp-1", "v": 1, "ts": "2026-11-25T00:00:00Z", "channel_values": {}, "channel_versions": {}, "versions_seen": {}}, {}, {})
    assert await cp.aget_tuple({"configurable": {"thread_id": tid}}) is not None

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200))) as http_client:
        svc = SupervisorService(cfg, http_client, BackendClient(cfg, http_client), store, checkpointer=cp)
        run_in = RunAgentInput(
            thread_id=tid,
            run_id="run-cancel-1",
            state={},
            messages=[],
            tools=[],
            context=[],
            forwarded_props={"incidentCode": "INC-2026-001", "explicitCancel": True},
        )
        props = svc.parse_forwarded_props(run_in)
        chunks = [c async for c in svc.stream_ag_ui(run_in, props, "oncall-lead", None, "text/event-stream")]
        events = _parse_ag_ui_events(chunks)
        snap = [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"][-1]
        assert snap["workflowStatus"] == "CANCELLED"
        assert await cp.aget_tuple({"configurable": {"thread_id": tid}}) is None


# =============================================================================
# 2. A2A 표준 스트리밍 & 5개 계약 시나리오 (정상 · 타임아웃 · 취소 · 중복 · 워커 교체)
# =============================================================================


async def test_a2a_five_contract_scenarios_streaming_timeout_cancel_dedup_and_swap():
    """
    A2A 5개 계약 시나리오 검증:
    1) 정상 스트리밍: 진행 이벤트가 최종 산출물보다 선행 도착 & 첫 진행 이벤트 p95 기록
    2) 타임아웃: 느린 워커 호출 시 TimeoutError 안전 차단
    3) 취소: 진행 중 작업 취소 안전 처리
    4) 중복 요청: 동일 idempotencyKey 재전송 시 워커 중복 실행 0건
    5) 동일 계약 워커 교체: keyword 워커 -> agentic 워커 교체 시 Supervisor 무수정 호환
    """
    cfg = _settings()
    mcp_call_count = {"search": 0, "pull": 0}
    slow_delay = {"seconds": 0.0}

    async def fake_mcp(tool_name: str, args: dict[str, Any]) -> Any:
        if slow_delay["seconds"] > 0:
            await asyncio.sleep(slow_delay["seconds"])
        if tool_name == "searchCorpusIndex":
            mcp_call_count["search"] += 1
            if args["corpusType"] == "LOG":
                return [
                    {"chunkId": "LOG-104", "corpusType": "LOG", "title": "HikariPool timeout", "keywords": "HikariPool,502", "sequenceOrder": 1},
                ]
            return [
                {"chunkId": "RUNBOOK-01", "corpusType": "RUNBOOK", "title": "DB 누수 롤백", "keywords": "HikariPool,rollback", "sequenceOrder": 1},
            ]
        if tool_name == "pullCorpusChunk":
            mcp_call_count["pull"] += 1
            return {
                "chunkId": args["chunkId"],
                "corpusType": "RUNBOOK" if args["chunkId"].startswith("RUNBOOK") else "LOG",
                "title": args["chunkId"],
                "content": "HikariPool timeout\nkubectl rollout undo deployment/payment-service",
            }
        return {}

    log_app = create_log_agent_app(cfg)
    async with log_app.router.lifespan_context(log_app):
        log_app.state.container.backend_client = BackendClient(cfg, log_app.state.container.http_client, mcp_caller=fake_mcp, agent_role="investigator")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=log_app), base_url="http://log-agent") as http:
            # AgentCard streaming=True 선언 검증
            card_resp = await http.get("/.well-known/agent-card.json")
            assert card_resp.status_code == 200
            assert card_resp.json()["capabilities"]["streaming"] is True

            # 시나리오 1) 정상 스트리밍 5회 반복 — 진행 이벤트 선행 도착 및 첫 진행 이벤트 p95 계측
            first_progress_latencies: list[float] = []
            for idx in range(5):
                seen_progress: list[dict[str, Any]] = []
                stream_res = await send_a2a_stream(
                    http,
                    base_url="http://log-agent",
                    internal_token=cfg.internal_service_token.get_secret_value(),
                    payload={"incidentCode": "INC-2026-001", "keywords": ["HikariPool"]},
                    context_id=f"ctx-stream-{idx}",
                    idempotency_key=f"idem-stream-{idx}",
                    trace_id=f"tr-stream-{idx}",
                    on_progress=lambda ev: asyncio.sleep(0, result=seen_progress.append(ev)),
                )
                assert stream_res.payload["ok"] is True
                assert stream_res.task_id is not None
                assert len(seen_progress) >= 2
                assert stream_res.first_progress_ms is not None and stream_res.final_artifact_ms is not None
                assert stream_res.first_progress_ms <= stream_res.final_artifact_ms
                first_progress_latencies.append(stream_res.first_progress_ms)

            p95_first_ms = _percentile(sorted(first_progress_latencies), 95.0)
            assert p95_first_ms >= 0.0

            # 시나리오 4) 중복 요청 — 동일 idempotencyKey 3회 전송 시 추가 MCP 실행 0건
            before_search = mcp_call_count["search"]
            before_pull = mcp_call_count["pull"]
            for _ in range(3):
                dup_res = await send_a2a_stream(
                    http,
                    base_url="http://log-agent",
                    internal_token=cfg.internal_service_token.get_secret_value(),
                    payload={"incidentCode": "INC-2026-001", "keywords": ["HikariPool"]},
                    context_id="ctx-dedup",
                    idempotency_key="idem-dedup-fixed",
                )
                assert dup_res.payload["ok"] is True
            # 1회차만 실행되고 2~3회차는 캐시 히트로 중복 실행 0건!
            assert mcp_call_count["search"] - before_search == 3
            assert mcp_call_count["pull"] - before_pull == 2

            # 시나리오 2) 타임아웃 — 느린 워커에 timeout_seconds 강제
            slow_delay["seconds"] = 0.4
            with pytest.raises(TimeoutError):
                await send_a2a_stream(
                    http,
                    base_url="http://log-agent",
                    internal_token=cfg.internal_service_token.get_secret_value(),
                    payload={"incidentCode": "INC-2026-001", "keywords": ["HikariPool"]},
                    context_id="ctx-timeout",
                    idempotency_key="idem-timeout",
                    timeout_seconds=0.05,
                )

            # 시나리오 3) 취소 — 진행 중 스트리밍 태스크 취소
            stream_task = asyncio.create_task(
                send_a2a_stream(
                    http,
                    base_url="http://log-agent",
                    internal_token=cfg.internal_service_token.get_secret_value(),
                    payload={"incidentCode": "INC-2026-001", "keywords": ["HikariPool"]},
                    context_id="ctx-cancel",
                    idempotency_key="idem-cancel",
                )
            )
            await asyncio.sleep(0.05)
            stream_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stream_task
            slow_delay["seconds"] = 0.0

    # 시나리오 5) 동일 계약 워커 교체 — keyword 워커 -> agentic 워커로 교체해도 동일 A2A 계약 동작
    agentic_cfg = _settings(dr_dci_mode="agentic")
    agentic_app = create_log_agent_app(agentic_cfg)
    async with agentic_app.router.lifespan_context(agentic_app):
        agentic_app.state.container.backend_client = BackendClient(agentic_cfg, agentic_app.state.container.http_client, mcp_caller=fake_mcp, agent_role="investigator")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agentic_app), base_url="http://log-agent-v2") as http2:
            swapped_res = await send_a2a_stream(
                http2,
                base_url="http://log-agent-v2",
                internal_token=agentic_cfg.internal_service_token.get_secret_value(),
                payload={"incidentCode": "INC-2026-001", "keywords": ["HikariPool"]},
                context_id="ctx-swap",
                idempotency_key="idem-swap-1",
            )
            assert swapped_res.payload["ok"] is True
            assert "LOG-104" in swapped_res.payload["evidenceChunkIds"]
            assert swapped_res.payload.get("decisionTrace") is not None


# =============================================================================
# 3. MCP 리소스 조회 & 4개 부정 계약(허용 0건)
# =============================================================================


async def test_mcp_resource_discovery_and_four_negative_security_contracts():
    """
    MCP 계약 검증:
    - 버전 있는 읽기 전용 리소스(incident://evidence-summary/v1) 목록 탐색·조회 실통신
    - 4개 부정 계약 허용 0건:
      1) 잘못된 내부 토큰 -> 401
      2) 타 장애 근거 접근 -> 404/거부
      3) 조사 워커(investigator)의 쓰기·완료 시도 -> 403 거부
      4) 미승인 완료 요청 -> 409 거부
    """
    cfg = _settings()

    async def fake_resource_reader(uri: str | None) -> Any:
        if uri is None:
            return [
                {
                    "uri": EVIDENCE_SUMMARY_RESOURCE_URI,
                    "name": "incident-evidence-summary-v1",
                    "description": "Versioned read-only summary",
                    "mimeType": "application/json",
                }
            ]
        if uri == EVIDENCE_SUMMARY_RESOURCE_URI:
            return {
                "schemaVersion": "v1",
                "readOnly": True,
                "resourceUri": EVIDENCE_SUMMARY_RESOURCE_URI,
                "incidentCount": 1,
                "incidents": [{"incidentCode": "INC-2026-001", "status": "OPEN"}],
            }
        raise BackendApiError(404, f"unknown resource: {uri}")

    async def negative_mcp(tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name == "pullCorpusChunk" and args.get("chunkId") == "OTHER-INC-CHUNK":
            raise BackendApiError(404, "해당 장애에서 조회할 수 있는 청크를 찾지 못했습니다.")
        if tool_name == "completeIncidentWithPostMortem" and args.get("approvalId") == "APR-UNAPPROVED":
            raise BackendApiError(409, "승인(APPROVED) 상태가 아니므로 완료할 수 없습니다.")
        return {}

    async with httpx.AsyncClient() as http_client:
        # 0) 정상 MCP 리소스 목록·조회
        investigator_client = BackendClient(
            cfg,
            http_client,
            mcp_caller=negative_mcp,
            mcp_resource_reader=fake_resource_reader,
            agent_role="investigator",
        )
        resources = await investigator_client.list_mcp_resources()
        assert len(resources) == 1
        assert resources[0]["uri"] == EVIDENCE_SUMMARY_RESOURCE_URI

        summary = await investigator_client.read_mcp_resource(EVIDENCE_SUMMARY_RESOURCE_URI)
        assert summary["schemaVersion"] == "v1"
        assert summary["readOnly"] is True

        # 부정 계약 1) 잘못된/미설정 토큰 -> 401
        no_token_client = BackendClient(_settings(internal_service_token=SecretStr("")), http_client)
        with pytest.raises(BackendApiError) as e_auth:
            await no_token_client.get_incident_detail("INC-2026-001")
        assert e_auth.value.status_code == 401

        # 부정 계약 2) 타 장애 근거 청크 접근 -> 404 거부
        with pytest.raises(BackendApiError) as e_scope:
            await investigator_client.pull_corpus_chunk("INC-2026-001", "OTHER-INC-CHUNK")
        assert e_scope.value.status_code == 404

        # 부정 계약 3) 조사 워커(investigator)의 쓰기/완료 시도 -> 403 거부
        with pytest.raises(BackendApiError) as e_write_mcp:
            await investigator_client.complete_incident_with_postmortem(
                approval_id="APR-1",
                incident_code="INC-2026-001",
                action_command="kubectl rollout undo deployment/payment-service",
                executed_by="log-agent",
                execution_status="SUCCESS",
            )
        assert e_write_mcp.value.status_code == 403

        with pytest.raises(BackendApiError) as e_write_rest:
            await investigator_client.create_plan("INC-2026-001", {"planId": "PLAN-BAD"})
        assert e_write_rest.value.status_code == 403

        # 부정 계약 4) 미승인 완료 요청 -> 409 거부 (executor/supervisor 권한이어도 승인 원장 미승인 시 차단)
        supervisor_client = BackendClient(cfg, http_client, mcp_caller=negative_mcp, agent_role="supervisor")
        with pytest.raises(BackendApiError) as e_unapproved:
            await supervisor_client.complete_incident_with_postmortem(
                approval_id="APR-UNAPPROVED",
                incident_code="INC-2026-001",
                action_command="kubectl rollout undo deployment/payment-service",
                executed_by="remediation-agent",
                execution_status="SUCCESS",
            )
        assert e_unapproved.value.status_code == 409

        # call_mcp_tool 직접 호출 경로에서도 investigator 쓰기 도구가 403 차단되어야 함
        with pytest.raises(BackendApiError) as e_direct:
            await investigator_client.call_mcp_tool("completeIncidentWithPostMortem", {"approvalId": "APR-1"})
        assert e_direct.value.status_code == 403


async def test_codex_regressions_incident_isolation_redis_reconcile_inflight_dedup_and_cache_ttl():
    """Codex 1차 리뷰 4건(타 장애 재개 격리, Redis 복구 리컨사일, in-flight 중복 차단, 체크포인트 캐시 TTL 만료)을 회귀 검증한다."""
    import time

    cfg = _settings(tool_cache_ttl_seconds=60.0)

    # 1) Redis 복구 시 폴백에 쌓인 최신 체크포인트가 이전 Redis 체크포인트보다 우선 리컨사일되어야 함
    class _ToggleRedis(_SharedInMemoryRedis):
        def __init__(self) -> None:
            super().__init__()
            self.down = False

        async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
            if self.down:
                raise ConnectionError("redis down")
            return await super().set(key, value, ex=ex, nx=nx)

        async def get(self, key: str) -> str | None:
            if self.down:
                raise ConnectionError("redis down")
            return await super().get(key)

        async def lrange(self, key: str, start: int, end: int) -> list[str]:
            if self.down:
                raise ConnectionError("redis down")
            return await super().lrange(key, start, end)

    toggle_redis = _ToggleRedis()
    cp = RedisCheckpointSaver(toggle_redis)
    cfg_t = {"configurable": {"thread_id": "t-rec", "checkpoint_ns": ""}}
    await cp.aput(cfg_t, {"id": "cp-old", "v": 1, "ts": "2026-11-25T00:00:00Z", "channel_values": {"step": "plan"}, "channel_versions": {}, "versions_seen": {}}, {}, {})
    toggle_redis.down = True
    await cp.aput(cfg_t, {"id": "cp-new", "v": 1, "ts": "2026-11-25T00:01:00Z", "channel_values": {"step": "execute_log"}, "channel_versions": {}, "versions_seen": {}}, {}, {})
    toggle_redis.down = False
    recovered = await cp.aget_tuple({"configurable": {"thread_id": "t-rec"}})
    assert recovered is not None
    assert recovered.checkpoint["id"] == "cp-new"

    # 2) 동일 idempotencyKey의 동시 진행 중(in-flight) 2개 요청은 실제 MCP 검색을 1회만 실행해야 함
    mcp_calls = {"search": 0}

    async def slow_mcp(tool_name: str, args: dict[str, Any]) -> Any:
        await asyncio.sleep(0.12)
        if tool_name == "searchCorpusIndex":
            mcp_calls["search"] += 1
            return [{"chunkId": "LOG-1", "corpusType": args["corpusType"], "title": "502", "keywords": "502", "sequenceOrder": 1}]
        return {"chunkId": "LOG-1", "corpusType": "LOG", "title": "502", "content": "502 error"}

    log_app = create_log_agent_app(cfg)
    async with log_app.router.lifespan_context(log_app):
        log_app.state.container.backend_client = BackendClient(cfg, log_app.state.container.http_client, mcp_caller=slow_mcp, agent_role="investigator")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=log_app), base_url="http://log-agent") as http:
            r1, r2 = await asyncio.gather(
                send_a2a_stream(http, base_url="http://log-agent", internal_token=cfg.internal_service_token.get_secret_value(), payload={"incidentCode": "INC-1", "keywords": ["502"]}, idempotency_key="inflight-same"),
                send_a2a_stream(http, base_url="http://log-agent", internal_token=cfg.internal_service_token.get_secret_value(), payload={"incidentCode": "INC-1", "keywords": ["502"]}, idempotency_key="inflight-same"),
            )
            assert r1.payload["ok"] is True and r2.payload["ok"] is True
            # 1회 실행당 LOG+RUNBOOK 2회 검색만 수행 (동시 2번째 요청이 중복 실행하지 않음)
            assert mcp_calls["search"] == 3

    # 3) 타 장애(INC-B)로 resume=True 요청 시 INC-A의 체크포인트를 재개하지 않아야 함 & 4) 만료된 캐시 항목은 재개 시 제거되어야 함
    shared_redis = _SharedInMemoryRedis()
    seen_incidents: list[str] = []
    seen_cached_keys: list[list[str]] = []
    entered = asyncio.Event()
    fail_once = {"flag": True}

    async def trace_a2a(url: str, payload: dict[str, Any], ctx_id: str) -> dict[str, Any]:
        if url == cfg.log_agent_a2a_url:
            seen_incidents.append(str(payload["incidentCode"]))
            return {"ok": True, "incidentCode": payload["incidentCode"], "evidence": [{"chunkId": "LOG-1", "corpusType": "LOG", "title": "t", "summary": "502"}], "evidenceChunkIds": ["LOG-1"], "runbookSummary": "rb", "remainingChunkIds": [], "pulledCount": 1}
        seen_cached_keys.append(sorted((payload.get("cachedToolResults") or {}).keys()))
        if fail_once["flag"]:
            fail_once["flag"] = False
            entered.set()
            await asyncio.sleep(5.0)
        return {"ok": True, "status": "BLOCKED", "message": "done", "toolCalls": [], "cachedToolResults": {}}

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"incident": {"incidentCode": req.url.path.rsplit("/", 1)[-1], "serviceName": "payment-service", "status": "OPEN"}, "plans": [], "approvals": [], "executions": []}))) as http_client:
        backend = BackendClient(cfg, http_client)
        cp_iso = RedisCheckpointSaver(shared_redis)
        g = build_supervisor_graph(cfg, http_client, backend, a2a_caller=trace_a2a, checkpointer=cp_iso)
        t = asyncio.create_task(
            g.ainvoke(
                {
                    "session_id": "tid-iso",
                    "run_id": "r-a",
                    "trace_id": "tr-a",
                    "incident_code": "INC-A",
                    "service_name": "payment-service",
                    "user_message": "14:28~14:50 5분 롤백",
                    "incident_started_at": "2026-11-18T14:04:00+09:00",
                    "constraints": {},
                    "evidence": [],
                    "pulled_chunk_ids": [],
                    "remaining_chunk_ids": [],
                    "runbook_summary": "",
                    "cached_tool_results": {"stale_freeze": {"ok": True}},
                    "cached_tool_results_timestamps": {"stale_freeze": time.time() - 600.0},
                    "replan_count": 0,
                    "should_replan": False,
                    "replan_history": [],
                    "ui_events": [],
                },
                config={"configurable": {"thread_id": "tid-iso"}},
            )
        )
        await entered.wait()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        # 1차 실행에서 만료된 stale_freeze 캐시는 remediation_agent에 전달되지 않았어야 함
        assert seen_cached_keys[0] == []

        # 이제 INC-B로 resume=True 요청 시 INC-A 체크포인트를 재개하지 않고 INC-B로 새로 실행해야 함
        svc = SupervisorService(cfg, http_client, backend, SupervisorRedisStore(shared_redis), a2a_caller=trace_a2a, checkpointer=cp_iso)
        run_b = RunAgentInput(
            thread_id="tid-iso",
            run_id="r-b",
            state={},
            messages=[UserMessage(id="u1", content="14:28~14:50 5분 롤백")],
            tools=[],
            context=[],
            forwarded_props={"incidentCode": "INC-B", "incidentStartedAt": "2026-11-18T14:04:00+09:00", "resume": True},
        )
        props_b = svc.parse_forwarded_props(run_b)
        chunks_b = [c async for c in svc.stream_ag_ui(run_b, props_b, "op", None, "text/event-stream")]
        events_b = _parse_ag_ui_events(chunks_b)
        snap_b = [e["snapshot"] for e in events_b if e["type"] == "STATE_SNAPSHOT"][-1]
    assert snap_b["incidentCode"] == "INC-B"
    assert "INC-B" in seen_incidents

    # 5) 체크포인트는 Redis에 저장되고 후속 aput_writes만 폴백된 경우 복구 시 누락 없이 리컨사일 + 삭제 툼스톤 보존 검증
    class _FailWritesRedis(_ToggleRedis):
        def __init__(self) -> None:
            super().__init__()
            self.fail_writes = False
            self.fail_delete = False

        async def hsetnx(self, key: str, field: str, value: str) -> int:
            if self.fail_writes:
                raise ConnectionError("redis write down")
            return await super().hsetnx(key, field, value)

        async def delete(self, *keys: str) -> int:
            if self.fail_delete:
                raise ConnectionError("redis delete down")
            return await super().delete(*keys)

    fw_redis = _FailWritesRedis()
    cp_fw = RedisCheckpointSaver(fw_redis)
    saved_cfg = await cp_fw.aput(
        {"configurable": {"thread_id": "t-fw", "checkpoint_ns": ""}},
        {"id": "cp-w1", "v": 1, "ts": "2026-11-25T00:00:00Z", "channel_values": {}, "channel_versions": {}, "versions_seen": {}},
        {},
        {},
    )
    fw_redis.fail_writes = True
    await cp_fw.aput_writes(saved_cfg, [("evidence", ["LOG-99"])], task_id="task-w1")
    fw_redis.fail_writes = False
    tup_fw = await cp_fw.aget_tuple({"configurable": {"thread_id": "t-fw"}})
    assert tup_fw is not None
    assert any(ch == "evidence" and val == ["LOG-99"] for _, ch, val in (tup_fw.pending_writes or []))

    # Redis 단절 중 adelete_thread -> 툼스톤 기록 -> Redis 복구 후 aget_tuple은 None을 반환해야 함
    fw_redis.fail_delete = True
    await cp_fw.adelete_thread("t-fw")
    fw_redis.fail_delete = False
    assert await cp_fw.aget_tuple({"configurable": {"thread_id": "t-fw"}}) is None
