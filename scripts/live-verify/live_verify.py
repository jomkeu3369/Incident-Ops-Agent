"""격리 검증 스택(docker-compose.verify.yaml)에서 AG-UI 중단·재개, A2A 계약, MCP 권한 계약을 실측해 JSON으로 남긴다.

    python scripts/live-verify/live_verify.py --env-file <verify.env> --output docs/eval/results/phase7-live-stack.json

실패한 계약도 그대로 기록하며, 하나라도 실패하면 종료 코드 1을 반환한다.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Any, Awaitable, Callable
import uuid

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent" / "src"))

from common.a2a_utils import A2ATaskNotActiveError, cancel_a2a_task, send_a2a_stream  # noqa: E402
from eval.harness import _percentile  # noqa: E402

GATEWAY = "http://127.0.0.1:18088"
SPRING = "http://127.0.0.1:18080"
SUPERVISOR = "http://127.0.0.1:18000"
LOG_AGENT = "http://127.0.0.1:18001"
LOG_AGENT_V2 = "http://127.0.0.1:28011"
TRANSPORTS = {"direct": SUPERVISOR, "caddy": GATEWAY}
SEED_INCIDENT = "INC-2026-001"
STARTED_AT = "2026-11-18T14:04:00+09:00"
PROMPT = "이전 버전의 502 비율이 1% 미만이면 롤백을 제안하고 14:28~14:50 사이에서 5분 작업 창을 찾아줘."
RESOURCE_URI = "incident://evidence-summary/v1"
STAMP = datetime.now().strftime("%d%H%M%S")
VERIFY_LOG_TITLE = "v2.4.1 배포 직후 HikariPool 커넥션 타임아웃·502 급증"
VERIFY_LOG = "2026-11-18 14:02:30 ERROR [payment-service] v2.4.1 배포 직후 HikariPool-1 커넥션 타임아웃 및 HTTP 502 급증"
VERIFY_RUNBOOK_TITLE = "배포 직후 커넥션 누수 롤백 검증 런북"
VERIFY_RUNBOOK = (
    "1. 신규 배포 직후 커넥션 타임아웃이 확인되고 이전 버전의 502 비율이 1% 미만이면 롤백한다.\\n"
    "kubectl rollout undo deployment/payment-service"
)


class Stack:
    def __init__(self, env_file: Path) -> None:
        values = dict(
            line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        self.env_file = env_file
        self.shared = values["INTERNAL_SERVICE_TOKEN"]
        self.readonly = values["INTERNAL_INVESTIGATOR_TOKEN"]
        self.compose = [
            "docker", "compose", "-p", "iopsv", "--env-file", str(env_file),
            "-f", "docker-compose-infra.yaml", "-f", "docker-compose.yaml",
            "-f", "scripts/live-verify/docker-compose.verify.yaml",
        ]
        self._incident_no = 0

    def headers(self, token: str | None = None) -> dict[str, str]:
        return {"X-Internal-Token": token or self.shared}

    async def ledger(self, incident: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{SPRING}/api/incidents/{incident}", headers=self.headers())
            response.raise_for_status()
            return response.json()

    async def new_incident(self) -> str:
        """시드 장애의 로그를 복제한 새 장애를 만들어 실행마다 원장을 분리한다.

        데모 시드 로그만으로는 규칙 기반 판정기가 보류하므로, 검증 장애에는 배포 인과를 명시한 로그 1건을,
        격리 DB에는 검증 전용 런북 1건을 추가한다(검증 대상은 판정 정확도가 아니라 프로토콜·원장 일관성이다).
        """
        self._incident_no += 1
        code = f"INC-LV-{STAMP}-{self._incident_no:03d}"
        body = {"incidentCode": code, "title": "결제 API 502 Bad Gateway 급증 및 응답 지연", "serviceName": "payment-service",
                "severity": "SEV1", "summary": "14:00 배포 직후 502 급증"}
        async with httpx.AsyncClient(timeout=15) as client:
            (await client.post(f"{SPRING}/api/incidents", json=body, headers=self.headers())).raise_for_status()
        suffix = f"{STAMP[-6:]}{self._incident_no:03d}"
        sql = (
            "INSERT INTO corpus_chunks (chunk_id, corpus_type, incident_code, service_name, title, keywords, content, sequence_order) "
            f"SELECT CONCAT(chunk_id, '-{suffix}'), corpus_type, '{code}', service_name, title, keywords, content, sequence_order "
            f"FROM corpus_chunks WHERE incident_code = '{SEED_INCIDENT}';"
            "INSERT INTO corpus_chunks (chunk_id, corpus_type, incident_code, service_name, title, keywords, content, sequence_order) VALUES "
            f"('LOG-LV-{suffix}', 'LOG', '{code}', 'payment-service', '{VERIFY_LOG_TITLE}', 'HikariPool,Connection,timeout,502,deploy,배포', '{VERIFY_LOG}', 7);"
            "INSERT IGNORE INTO corpus_chunks (chunk_id, corpus_type, incident_code, service_name, title, keywords, content, sequence_order) VALUES "
            f"('RUNBOOK-LV-01', 'RUNBOOK', NULL, 'payment-service', '{VERIFY_RUNBOOK_TITLE}', 'HikariPool,leak,rollback,502,배포,롤백', '{VERIFY_RUNBOOK}', 1);"
        )
        subprocess.run(
            ["docker", "exec", "-i", "iopsv-mysql", "mysql", "--default-character-set=utf8mb4", "-uops_user", "-pops_password_1234", "incident_ops"],
            input=sql.encode(), check=True, capture_output=True,
        )
        return code

    async def wait_ready(self, url: str, timeout: float = 90.0) -> None:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=5) as client:
                while True:
                    try:
                        if (await client.get(url)).status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.5)

    async def kill_supervisor(self) -> None:
        """실행 중인 Supervisor 프로세스를 정리 기회 없이 종료한다(SIGKILL)."""
        await asyncio.to_thread(subprocess.run, ["docker", "kill", "iopsv-supervisor"], check=True, capture_output=True)

    async def start_supervisor(self) -> None:
        await asyncio.to_thread(subprocess.run, ["docker", "start", "iopsv-supervisor"], check=False, capture_output=True)
        try:
            await self.wait_ready(f"{SUPERVISOR}/api/ag-ui/status", timeout=30)
        except TimeoutError:
            await self.recreate_supervisor()
        await self.wait_ready(f"{GATEWAY}/api/ag-ui/status")

    async def recreate_supervisor(self, env: dict[str, str] | None = None, attempts: int = 6) -> None:
        """컨테이너를 새로 만든다. Docker Desktop의 간헐적 포트 게시 실패는 다시 시도한다."""
        command = [*self.compose, "up", "-d", "--no-deps", "--force-recreate", "supervisor-agent"]
        for attempt in range(1, attempts + 1):
            done = await asyncio.to_thread(subprocess.run, command, cwd=ROOT, env=env, check=False, capture_output=True)
            if done.returncode == 0:
                try:
                    await self.wait_ready(f"{SUPERVISOR}/api/ag-ui/status", timeout=30)
                    return
                except TimeoutError:
                    pass
            print(f"[stack] supervisor 재생성 재시도 {attempt}/{attempts}", flush=True)
            await asyncio.sleep(5)
        raise RuntimeError("supervisor 컨테이너를 기동하지 못했습니다.")


# --------------------------------------------------------------------------- AG-UI
async def agui(
    base: str, thread: str, run_id: str, incident: str, props: dict[str, Any], text: str | None = None,
    stop: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    body = {
        "threadId": thread, "runId": run_id, "state": {}, "tools": [], "context": [],
        "messages": [{"id": f"u-{run_id}", "role": "user", "content": text}] if text else [],
        "forwardedProps": {"incidentCode": incident, "incidentStartedAt": STARTED_AT, **props},
    }
    result: dict[str, Any] = {"http": None, "events": [], "disconnected": False}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90)) as client:
            async with client.stream("POST", f"{base}/api/ag-ui", json=body, headers={"Accept": "text/event-stream"}) as response:
                result["http"] = response.status_code
                if response.status_code != 200:
                    return result
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:])
                    result["events"].append(event)
                    if stop is not None and await stop(event):
                        result["disconnected"] = True
                        break
    except httpx.TransportError:
        # 의도한 중단 뒤의 연결 정리 오류만 무시한다
        if not result["disconnected"]:
            raise
    return result


def snapshots(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [e["snapshot"] for e in result["events"] if e["type"] == "STATE_SNAPSHOT"]


async def completed_log_calls(thread: str) -> int:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{SUPERVISOR}/api/ag-ui/sessions/{thread}")
    if response.status_code != 200:
        return 0
    return sum(1 for event in response.json()["events"] if "a2a:log-analyzer-agent" in json.dumps(event) and event.get("type", event.get("eventType")) == "tool_call")


def ledger_counts(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "incidentStatus": detail["incident"]["status"], "plans": len(detail["plans"]), "approvals": len(detail["approvals"]),
        "executions": len(detail["executions"]), "postMortem": detail.get("postMortem") is not None,
    }


async def plan(stack: Stack, base: str, thread: str, incident: str) -> dict[str, Any]:
    result = await agui(base, thread, f"{thread}-plan", incident, {}, PROMPT)
    snapshot = snapshots(result)[-1]
    assert snapshot["workflowStatus"] == "AWAITING_APPROVAL", f"planning did not reach approval: {snapshot['workflowStatus']}"
    return snapshot


async def interrupt_during_investigation(stack: Stack, index: int, transport: str, kill: bool) -> dict[str, Any]:
    base, incident, thread = TRANSPORTS[transport], await stack.new_incident(), f"lv-p1-{STAMP}-{index}"

    async def on_progress(event: dict[str, Any]) -> bool:
        if event["type"] != "CUSTOM" or event.get("name") != "a2a_progress":
            return False
        if kill:
            await stack.kill_supervisor()
        return True

    first = await agui(base, thread, f"{thread}-r1", incident, {}, PROMPT, stop=on_progress)
    observed = first["disconnected"] and not snapshots(first)
    if kill:
        await stack.start_supervisor()
    before = ledger_counts(await stack.ledger(incident))
    resumed = await agui(base, thread, f"{thread}-r2", incident, {"resume": True, "resumeRunId": f"{thread}-r1"}, PROMPT)
    detail = await stack.ledger(incident)
    after = ledger_counts(detail)
    final = snapshots(resumed)[-1] if snapshots(resumed) else {}
    approval_ids = [a["approvalId"] for a in detail["approvals"]]
    passed = (
        observed and final.get("workflowStatus") == "AWAITING_APPROVAL" and after["plans"] == 1 and after["approvals"] == 1
        and after["executions"] == 0 and (final.get("approval") or {}).get("approvalId") in approval_ids
    )
    return {"incident": incident, "interruptObserved": observed, "ledgerBeforeResume": before, "ledgerAfter": after,
            "finalUiStatus": final.get("workflowStatus"), "completedInvestigationCalls": await completed_log_calls(thread), "passed": passed}


async def interrupt_while_awaiting_approval(stack: Stack, index: int, transport: str, kill: bool) -> dict[str, Any]:
    base, incident, thread = TRANSPORTS[transport], await stack.new_incident(), f"lv-p2-{STAMP}-{index}"
    planned = await plan(stack, base, thread, incident)
    calls_before, before = await completed_log_calls(thread), ledger_counts(await stack.ledger(incident))
    if kill:
        await stack.kill_supervisor()
        await stack.start_supervisor()
    restored = await agui(base, thread, f"{thread}-restore", incident, {"restore": True})
    after, calls_after = ledger_counts(await stack.ledger(incident)), await completed_log_calls(thread)
    final = snapshots(restored)[-1] if snapshots(restored) else {}
    passed = (
        final.get("workflowStatus") == "AWAITING_APPROVAL" and final.get("approval") == planned["approval"]
        and before == after and after["executions"] == 0 and calls_before == calls_after
    )
    return {"incident": incident, "interruptObserved": True, "ledgerBeforeRestore": before, "ledgerAfter": after,
            "finalUiStatus": final.get("workflowStatus"), "completedInvestigationCalls": [calls_before, calls_after], "passed": passed}


async def interrupt_during_completion(stack: Stack, index: int, transport: str, kill: bool, after_commit: bool) -> dict[str, Any]:
    base, incident, thread = TRANSPORTS[transport], await stack.new_incident(), f"lv-p3-{STAMP}-{index}"
    planned = await plan(stack, base, thread, incident)
    decision = {"approvalDecision": {"approvalId": planned["approval"]["approvalId"], "decision": "APPROVED"}}

    async def at_execution(event: dict[str, Any]) -> bool:
        if event["type"] != "STATE_SNAPSHOT" or event["snapshot"]["workflowStatus"] != "AWAITING_EXECUTION_RESULT":
            return False
        if after_commit:
            # 원장 커밋을 확인한 뒤, 클라이언트가 완료 응답을 받기 전에 끊는다
            async with asyncio.timeout(30):
                while (await stack.ledger(incident))["incident"]["status"] != "RESOLVED":
                    await asyncio.sleep(0.05)
        if kill:
            await stack.kill_supervisor()
        return True

    first = await agui(base, thread, f"{thread}-approve", incident, decision, stop=at_execution)
    observed = first["disconnected"] and all(s["workflowStatus"] != "RESOLVED" for s in snapshots(first))
    if kill:
        await stack.start_supervisor()
    before = ledger_counts(await stack.ledger(incident))
    restored = await agui(base, thread, f"{thread}-restore", incident, {"restore": True})
    restored_status = snapshots(restored)[-1]["workflowStatus"] if snapshots(restored) else None
    status, retried = restored_status, False
    if status != "RESOLVED":
        retried = True
        again = await agui(base, thread, f"{thread}-retry", incident, decision)
        status = snapshots(again)[-1]["workflowStatus"] if snapshots(again) else None
    after = ledger_counts(await stack.ledger(incident))
    passed = (
        observed and status == "RESOLVED" and after["incidentStatus"] == "RESOLVED"
        and after["executions"] == 1 and after["postMortem"] and after["approvals"] == 1
    )
    return {"incident": incident, "interruptObserved": observed, "disconnectAfterCommit": after_commit, "ledgerBeforeRecovery": before,
            "restoredUiStatus": restored_status, "retriedSameApproval": retried, "finalUiStatus": status, "ledgerAfter": after, "passed": passed}


async def cancel_during_investigation(stack: Stack, index: int, transport: str) -> dict[str, Any]:
    base, incident, thread = TRANSPORTS[transport], await stack.new_incident(), f"lv-c-{STAMP}-{index}"
    outcome: dict[str, Any] = {}

    async def cancel_on_progress(event: dict[str, Any]) -> bool:
        if event["type"] == "CUSTOM" and event.get("name") == "a2a_progress" and "task" not in outcome:
            outcome["task"] = asyncio.create_task(agui(base, thread, f"{thread}-cancel", incident, {"explicitCancel": True}))
        return False

    await agui(base, thread, f"{thread}-r1", incident, {}, PROMPT, stop=cancel_on_progress)
    cancelled = await outcome["task"] if "task" in outcome else {"events": []}
    detail = await stack.ledger(incident)
    after = ledger_counts(detail)
    final = snapshots(cancelled)[-1] if snapshots(cancelled) else {}
    expected = "AWAITING_APPROVAL" if after["approvals"] else "CANCELLED"
    passed = "task" in outcome and final.get("workflowStatus") == expected and after["executions"] == 0
    return {"incident": incident, "cancelSent": "task" in outcome, "finalUiStatus": final.get("workflowStatus"), "ledgerAfter": after, "passed": passed}


async def run_agui(stack: Stack, repeats: int) -> dict[str, Any]:
    points: dict[str, list[dict[str, Any]]] = {"during_investigation": [], "awaiting_approval": [], "during_completion": [], "explicit_cancel": []}
    for index in range(repeats):
        transport = "direct" if index % 2 == 0 else "caddy"
        kill = index >= repeats // 2
        cases = (
            ("during_investigation", lambda: interrupt_during_investigation(stack, index, transport, kill)),
            ("awaiting_approval", lambda: interrupt_while_awaiting_approval(stack, index, transport, kill)),
            ("during_completion", lambda: interrupt_during_completion(stack, index, transport, kill, after_commit=(index // 2) % 2 == 1)),
        )
        for name, case in cases:
            started = time.perf_counter()
            try:
                record = await case()
            except Exception as exc:  # 실패도 결과에 남긴다
                record = {"passed": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            record.update(attempt=index + 1, transport=transport, processKilled=kill, seconds=round(time.perf_counter() - started, 2))
            points[name].append(record)
            print(f"[ag-ui] {name} #{index + 1} {transport} kill={kill} -> {'PASS' if record['passed'] else 'FAIL'}", flush=True)
    for index in range(max(1, repeats // 2)):
        transport = "direct" if index % 2 == 0 else "caddy"
        try:
            record = await cancel_during_investigation(stack, index, transport)
        except Exception as exc:
            record = {"passed": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        record.update(attempt=index + 1, transport=transport)
        points["explicit_cancel"].append(record)
        print(f"[ag-ui] explicit_cancel #{index + 1} {transport} -> {'PASS' if record['passed'] else 'FAIL'}", flush=True)
    summary = {name: {"runs": len(runs), "passed": sum(r["passed"] for r in runs)} for name, runs in points.items()}
    return {"summary": summary, "runs": points}


# --------------------------------------------------------------------------- A2A
async def run_a2a(stack: Stack, samples: int) -> dict[str, Any]:
    incident = await stack.new_incident()
    payload = {"incidentCode": incident, "keywords": ["HikariPool", "502", "rollback"]}
    token = stack.readonly
    out: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=30) as http:
        async def call(base: str = LOG_AGENT, key: str | None = None, **kwargs: Any):
            return await send_a2a_stream(http, base_url=base, internal_token=token, payload=payload,
                                         idempotency_key=key or f"lv-{uuid.uuid4().hex}", **kwargs)

        def stages(result) -> list[str]:
            return [e["detail"]["stage"] for e in result.progress_events if isinstance(e.get("detail"), dict) and "stage" in e["detail"]]

        first, final, ordered = [], [], []
        for _ in range(samples):
            result = await call()
            first.append(result.first_progress_ms)
            final.append(result.final_artifact_ms)
            ordered.append(result.payload["ok"] and result.first_progress_ms <= result.final_artifact_ms and "searching_index" in stages(result))
        first_sorted, final_sorted = sorted(first), sorted(final)
        out["streaming"] = {
            "samples": samples, "passed": all(ordered), "transport": "loopback HTTP -> log_agent container (client-side timing)",
            "firstProgressMs": {f"p{p}": _percentile(first_sorted, p) for p in (50, 95, 99)},
            "finalArtifactMs": {f"p{p}": _percentile(final_sorted, p) for p in (50, 95, 99)},
        }

        try:
            await call(timeout_seconds=0.001)
            out["timeout"] = {"passed": False}
        except TimeoutError:
            out["timeout"] = {"passed": True, "clientTimeoutSeconds": 0.001}

        attempts = []
        for _ in range(10):
            ids: list[asyncio.Future[str]] = [asyncio.get_running_loop().create_future()]
            running = asyncio.create_task(call(on_task_id=lambda task_id: ids[0].done() or ids[0].set_result(task_id)))
            task_id = await asyncio.wait_for(ids[0], 10)
            try:
                await cancel_a2a_task(http, base_url=LOG_AGENT, internal_token=token, task_id=task_id)
                result = await running
                attempts.append("canceled" if result.canceled else "cancel_acknowledged_but_completed")
            except A2ATaskNotActiveError:
                await running
                attempts.append("already_finished")
        out["cancel"] = {
            "outcomes": {name: attempts.count(name) for name in sorted(set(attempts))},
            "passed": "canceled" in attempts and "cancel_acknowledged_but_completed" not in attempts,
        }

        key = f"lv-dup-{uuid.uuid4().hex}"
        original, repeated = await call(key=key), await call(key=key)
        concurrent_key = f"lv-con-{uuid.uuid4().hex}"
        pair = await asyncio.gather(call(key=concurrent_key), call(key=concurrent_key))
        out["duplicate"] = {
            "repeatedStages": stages(repeated),
            "passed": "deduplicated_hit" in stages(repeated) and repeated.payload == original.payload and all(r.payload["ok"] for r in pair),
        }

        swapped = await call(base=LOG_AGENT_V2)
        out["workerContractV2"] = {"mode": "agentic", "pulledCount": swapped.payload.get("pulledCount"),
                                   "passed": bool(swapped.payload["ok"] and swapped.payload.get("decisionTrace") and swapped.payload["incidentCode"] == incident)}

    async def supervisor_with(log_url: str | None) -> None:
        env = dict(os.environ)
        if log_url:
            env["LOG_AGENT_A2A_URL"] = log_url
        await stack.recreate_supervisor(env)
        await stack.wait_ready(f"{GATEWAY}/api/ag-ui/status")

    try:
        await supervisor_with("http://log-analyzer-agent-v2:8001")
        swap_incident, thread = await stack.new_incident(), f"lv-swap-{STAMP}"
        result = await agui(SUPERVISOR, thread, f"{thread}-r1", swap_incident, {}, PROMPT)
        status = snapshots(result)[-1]["workflowStatus"]
        out["workerSwapViaSupervisor"] = {"finalUiStatus": status, "passed": status == "AWAITING_APPROVAL"}
    finally:
        await supervisor_with(None)
    return out


# --------------------------------------------------------------------------- MCP
async def raw_mcp_post(base: str, token: str, content: bytes, content_type: str) -> int:
    """SSE 세션을 연 뒤 메시지 POST의 HTTP 상태만 확인한다."""
    async with httpx.AsyncClient(timeout=10) as client:
        async with client.stream("GET", f"{base}/sse", headers={"X-Internal-Token": token}) as stream:
            async for line in stream.aiter_lines():
                if line.startswith("data:"):
                    endpoint = line[5:].strip()
                    break
            response = await client.post(f"{base}{endpoint}", content=content, headers={"X-Internal-Token": token, "Content-Type": content_type})
            return response.status_code


async def run_mcp(stack: Stack) -> dict[str, Any]:
    incident_a, incident_b = await stack.new_incident(), await stack.new_incident()
    thread = f"lv-mcp-{STAMP}"
    planned = await plan(stack, GATEWAY, thread, incident_a)
    pending_approval = planned["approval"]["approvalId"]
    action = next(step["actionCommand"] for step in planned["plan"]["steps"] if step.get("actionCommand"))
    complete_args = {"approvalId": pending_approval, "incidentCode": incident_a, "actionCommand": action, "executedBy": "live-verify",
                     "executionStatus": "SUCCESS", "verificationSummary": "x", "rootCause": "x", "timelineMarkdown": "x", "preventionItems": "x"}
    out: dict[str, Any] = {}
    for label, base in (("spring_direct", SPRING), ("caddy", GATEWAY)):
        record: dict[str, Any] = {}
        for role, token in (("shared", stack.shared), ("investigator", stack.readonly)):
            async with sse_client(f"{base}/sse", headers={"X-Internal-Token": token}) as (read, write), ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 15)
                tools = sorted(t.name for t in (await session.list_tools()).tools)
                resources = [str(r.uri) for r in (await session.list_resources()).resources]
                body = json.loads((await session.read_resource(RESOURCE_URI)).contents[0].text)
                found = await session.call_tool("searchCorpusIndex", {"incidentCode": incident_a, "corpusType": "LOG"})
                chunk_b = json.loads((await session.call_tool("searchCorpusIndex", {"incidentCode": incident_b, "corpusType": "LOG"})).content[0].text)[0]["chunkId"]
                cross = await session.call_tool("pullCorpusChunk", {"incidentCode": incident_a, "chunkId": chunk_b})
                entry = {
                    "tools": tools, "resources": resources,
                    "resourceFields": sorted({key for item in body["incidents"] for key in item}),
                    "readOk": not getattr(found, "is_error", getattr(found, "isError", False)),
                    "crossIncidentRejected": bool(getattr(cross, "is_error", getattr(cross, "isError", False))),
                }
                if role == "shared":
                    unapproved = await session.call_tool("completeIncidentWithPostMortem", complete_args)
                    entry["unapprovedCompletionRejected"] = bool(getattr(unapproved, "is_error", getattr(unapproved, "isError", False)))
                record[role] = entry

        call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "completeIncidentWithPostMortem", "arguments": complete_args}}
        smuggled = ('{"a":"丁\\",\"}":1,' + json.dumps(call)[1:]).encode("utf-8")
        async with httpx.AsyncClient(timeout=10) as client:
            bad_token = (await client.get(f"{base}/sse", headers={"X-Internal-Token": "wrong"})).status_code
            rest_write = (await client.post(f"{base}/api/incidents/{incident_a}/complete", json=complete_args, headers={"X-Internal-Token": stack.readonly})).status_code
        record["negative"] = {
            "invalidTokenStatus": bad_token,
            "investigatorMcpWriteStatus": await raw_mcp_post(base, stack.readonly, json.dumps(call).encode(), "application/json"),
            "investigatorCharsetSmuggleStatus": await raw_mcp_post(base, stack.readonly, smuggled, "application/json; charset=GBK"),
            "investigatorRestWriteStatus": rest_write,
        }
        record["ledgerAfter"] = ledger_counts(await stack.ledger(incident_a))
        negative, shared, investigator = record["negative"], record["shared"], record["investigator"]
        record["passed"] = (
            len(shared["tools"]) == 4 and shared["tools"] == investigator["tools"] and RESOURCE_URI in shared["resources"]
            and "summary" not in shared["resourceFields"] and shared["readOk"] and investigator["readOk"]
            and shared["crossIncidentRejected"] and investigator["crossIncidentRejected"] and shared["unapprovedCompletionRejected"]
            and negative["invalidTokenStatus"] == 401 and negative["investigatorMcpWriteStatus"] == 403
            and negative["investigatorCharsetSmuggleStatus"] == 403 and negative["investigatorRestWriteStatus"] == 403
            and record["ledgerAfter"]["executions"] == 0 and record["ledgerAfter"]["incidentStatus"] != "RESOLVED"
        )
        out[label] = record
        print(f"[mcp] {label} -> {'PASS' if record['passed'] else 'FAIL'}", flush=True)
    return out


def metadata() -> dict[str, Any]:
    def text(*command: str) -> str:
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()

    return {
        "recordedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": text("git", "rev-parse", "HEAD"),
        "workingTreeDirty": bool(text("git", "status", "--porcelain", "--", "agent", "backend", "frontend", "infra", "docker-compose.yaml")),
        "host": {"os": platform.platform(), "python": platform.python_version()},
        "docker": text("docker", "info", "--format", "{{.ServerVersion}} / {{.NCPU}} CPU / {{.MemTotal}} bytes"),
        "libraries": {name: version(name) for name in ("a2a-sdk", "mcp", "ag-ui-protocol", "langgraph")},
        "scope": {
            "real": ["MySQL 8.0", "Redis 7", "Spring Boot MCP/REST", "supervisor", "log_agent", "remediation_agent", "Caddy"],
            "substituted": ["GitHub Deployments API", "Grafana Prometheus API (fixed responses)", "local empty freeze calendar"],
            "judge": "rule_based (no LLM calls)", "execution": "simulated rollback",
            "processKill": "docker kill (SIGKILL) of the supervisor only, while the SSE stream is open when a run is active",
            "verificationFixtures": "per-incident deploy-correlation log chunk and one shared verification runbook (seed demo data alone ends BLOCKED under rule_based)",
        },
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--a2a-samples", type=int, default=20)
    parser.add_argument("--sections", default="mcp,a2a,agui")
    args = parser.parse_args()
    stack = Stack(args.env_file.resolve())
    report: dict[str, Any] = {"schemaVersion": "phase7-live-stack-v1", "metadata": metadata()}
    runners = {"mcp": lambda: run_mcp(stack), "a2a": lambda: run_a2a(stack, args.a2a_samples), "agui": lambda: run_agui(stack, args.repeats)}
    failed = False
    for name in args.sections.split(","):
        try:
            report[name] = await runners[name]()
        except Exception as exc:
            report[name] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
            failed = True
            print(f"[{name}] ERROR {report[name]['error']}", flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def failures(node: Any) -> bool:
        if isinstance(node, dict):
            return node.get("passed") is False or any(failures(v) for v in node.values())
        return any(failures(v) for v in node) if isinstance(node, list) else False

    failed = failed or failures(report)
    print(f"written {args.output} ({'FAIL' if failed else 'PASS'})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
