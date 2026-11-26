from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hmac
import json
import time
import uuid
from typing import Any, Awaitable, Callable
import httpx
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, Message, Part, Role
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, PROTOCOL_VERSION_CURRENT, VERSION_HEADER

from common.app_factory import SECURITY_HEADERS
from common.config import INTERNAL_TOKEN_HEADER
from common.observability import (
    end_span,
    extract_incoming_trace_headers,
    extract_trace_headers,
    langsmith_tracing_scope,
    traced_span,
)


def mount_a2a_agent(app: FastAPI, *, agent_card: AgentCard, executor: AgentExecutor) -> None:
    """FastAPI 앱에 A2A v1.0 AgentCard·JSON-RPC 라우트와 내부 토큰 검증·분산 트레이싱 미들웨어를 마운트한다."""

    @app.middleware("http")
    async def verify_a2a_rpc_token(request: Request, call_next):
        path = request.url.path
        # 공개 헬스체크·에이전트 카드와 자체 Depends를 쓰는 /internal 경로는 통과시킨다
        if path in ("/health", AGENT_CARD_WELL_KNOWN_PATH) or path.startswith("/internal/"):
            return await call_next(request)
        container = request.app.state.container
        expected = container.settings.internal_service_token.get_secret_value()
        provided = request.headers.get(INTERNAL_TOKEN_HEADER)
        if not expected or not provided or not hmac.compare_digest(expected, provided):
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"status": 401, "error": "Unauthorized", "message": "유효하지 않은 내부 서비스 토큰입니다."}, headers=SECURITY_HEADERS)

        parent_headers = extract_incoming_trace_headers(request.headers)
        with langsmith_tracing_scope(
            container.settings,
            client=container.langsmith_client,
            parent_headers=parent_headers,
            tags=["a2a", agent_card.name],
            metadata={"service": agent_card.name, "path": path},
        ):
            return await call_next(request)

    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=agent_card)
    add_a2a_routes_to_fastapi(app, agent_card_routes=create_agent_card_routes(agent_card), jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True))


def make_agent_json_message(payload: dict[str, Any], *, context_id: str | None = None) -> Message:
    """딕셔너리 결과를 A2A 에이전트 텍스트 메시지로 직렬화한다."""
    return Message(message_id=str(uuid.uuid4()), context_id=context_id or "", role=Role.ROLE_AGENT, parts=[Part(text=json.dumps(payload, ensure_ascii=False))])


async def send_a2a_json(http_client: httpx.AsyncClient, *, base_url: str, internal_token: str, payload: dict[str, Any], context_id: str | None = None) -> dict[str, Any]:
    """원격 A2A v1.0 워커에 JSON-RPC SendMessage를 전송하고 응답 JSON을 파싱한다."""
    rpc_body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "SendMessage",
        "params": {"message": {"messageId": str(uuid.uuid4()), "contextId": context_id or str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}},
    }
    async with traced_span(
        "a2a.send_message",
        run_type="chain",
        inputs={"base_url": base_url, "context_id": context_id, "payload": payload},
        tags=["a2a"],
    ) as span:
        headers = {
            "Content-Type": "application/json",
            VERSION_HEADER: PROTOCOL_VERSION_CURRENT,
            INTERNAL_TOKEN_HEADER: internal_token,
            **extract_trace_headers(),
        }
        resp = await http_client.post(f"{base_url.rstrip('/')}/", json=rpc_body, headers=headers)
        if resp.status_code != 200:
            end_span(span, error=f"A2A HTTP {resp.status_code}")
            raise RuntimeError(f"A2A 호출 실패 (HTTP {resp.status_code})")

        body = resp.json()
        if "error" in body:
            err = body["error"]
            end_span(span, error=str(err.get("message", err)))
            raise RuntimeError(f"A2A JSON-RPC 오류: {err.get('message', err)}")

        text = _extract_text_from_a2a_result(body.get("result", {}))
        if not text:
            end_span(span, error="empty A2A response text")
            raise RuntimeError("A2A 응답에 텍스트 결과가 없습니다.")
        parsed = json.loads(text)
        end_span(span, outputs={"ok": parsed.get("ok") if isinstance(parsed, dict) else True})
        return parsed


def _extract_text_from_a2a_result(result: dict[str, Any]) -> str:
    """A2A SendMessageResponse의 message 또는 task(artifacts 우선 -> status.message)에서 텍스트 파트를 추출한다."""
    msg = result.get("message")
    if isinstance(msg, dict):
        text = _first_part_text(msg.get("parts"))
        if text:
            return text
    task = result.get("task")
    if isinstance(task, dict):
        for artifact in task.get("artifacts") or []:
            text = _first_part_text(artifact.get("parts"))
            if text:
                return text
        status_msg = (task.get("status") or {}).get("message")
        if isinstance(status_msg, dict):
            text = _first_part_text(status_msg.get("parts"))
            if text:
                return text
    return ""


@dataclass
class A2AStreamResult:
    """A2A SendStreamingMessage 호출의 최종 산출물과 진행 이벤트·지연 메타데이터."""

    payload: dict[str, Any]
    task_id: str | None = None
    progress_events: list[dict[str, Any]] = field(default_factory=list)
    first_progress_ms: float | None = None
    final_artifact_ms: float | None = None
    canceled: bool = False


# 작업이 없거나 이미 끝나 취소 대상이 아니라는 A2A 오류 코드(TaskNotFound, TaskNotCancelable)
_A2A_TASK_GONE_CODES = frozenset({-32001, -32002})


class A2ATaskNotActiveError(RuntimeError):
    """원격에 취소할 작업이 남아 있지 않아 다시 시도할 필요가 없는 응답."""


async def cancel_a2a_task(
    http_client: httpx.AsyncClient,
    *,
    base_url: str,
    internal_token: str,
    task_id: str,
) -> dict[str, Any]:
    """실행 중인 원격 A2A 태스크에 CancelTask JSON-RPC 요청을 전송한다."""
    rpc_body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "CancelTask",
        "params": {"id": task_id},
    }
    headers = {
        "Content-Type": "application/json",
        VERSION_HEADER: PROTOCOL_VERSION_CURRENT,
        INTERNAL_TOKEN_HEADER: internal_token,
        **extract_trace_headers(),
    }
    resp = await http_client.post(f"{base_url.rstrip('/')}/", json=rpc_body, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"A2A CancelTask 실패 (HTTP {resp.status_code})")
    body = resp.json()
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("code") in _A2A_TASK_GONE_CODES:
        raise A2ATaskNotActiveError("A2A CancelTask 대상 작업이 남아 있지 않습니다.")
    if not isinstance(body, dict) or "error" in body or not isinstance(body.get("result"), dict):
        raise RuntimeError("A2A CancelTask JSON-RPC 오류")
    return body


# A2A SDK의 SSE ping 간격(15초)보다 커야 조용한 조사 중에 스트림이 끊기지 않는다
_STREAM_MIN_READ_TIMEOUT_SECONDS = 45.0


def _stream_timeout(base: httpx.Timeout) -> httpx.Timeout:
    read = None if base.read is None else max(base.read, _STREAM_MIN_READ_TIMEOUT_SECONDS)
    return httpx.Timeout(connect=base.connect, read=read, write=base.write, pool=base.pool)


async def send_a2a_stream(
    http_client: httpx.AsyncClient,
    *,
    base_url: str,
    internal_token: str,
    payload: dict[str, Any],
    context_id: str | None = None,
    idempotency_key: str | None = None,
    trace_id: str | None = None,
    timeout_seconds: float | None = None,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_task_id: Callable[[str], None] | None = None,
) -> A2AStreamResult:
    """원격 A2A v1.0 워커에 SendStreamingMessage SSE 요청을 전송하고 진행 상태·최종 산출물을 수신한다."""
    enriched_payload = dict(payload)
    if idempotency_key and "idempotencyKey" not in enriched_payload:
        enriched_payload["idempotencyKey"] = idempotency_key
    if trace_id and "traceId" not in enriched_payload:
        enriched_payload["traceId"] = trace_id

    rpc_body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "SendStreamingMessage",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "contextId": context_id or str(uuid.uuid4()),
                "role": "ROLE_USER",
                "parts": [{"text": json.dumps(enriched_payload, ensure_ascii=False)}],
            }
        },
    }

    async def _consume() -> A2AStreamResult:
        t0 = time.perf_counter()
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            VERSION_HEADER: PROTOCOL_VERSION_CURRENT,
            INTERNAL_TOKEN_HEADER: internal_token,
            **extract_trace_headers(),
        }
        progress_events: list[dict[str, Any]] = []
        first_progress_ms: float | None = None
        final_artifact_ms: float | None = None
        final_text: str = ""
        direct_message: str = ""
        artifact_parts: dict[str, str] = {}
        task_id: str | None = None
        terminal_state = ""

        def state_name(raw: Any) -> str:
            return str(raw or "").upper().removeprefix("TASK_STATE_")

        def note_task(raw: Any) -> None:
            """원격 작업 ID를 처음 알게 된 즉시 알려 호출자가 취소할 수 있게 한다."""
            nonlocal task_id
            if raw and str(raw) != task_id:
                task_id = str(raw)
                if on_task_id is not None:
                    on_task_id(task_id)

        def record_status(raw: Any) -> None:
            nonlocal terminal_state
            state = state_name(raw)
            if state in {"COMPLETED", "FAILED", "CANCELED", "CANCELLED", "REJECTED"}:
                terminal_state = state

        async with http_client.stream("POST", f"{base_url.rstrip('/')}/", json=rpc_body, headers=headers, timeout=_stream_timeout(http_client.timeout)) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"A2A 스트리밍 호출 실패 (HTTP {resp.status_code})")

            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                envelope = json.loads(data_str)
                if "error" in envelope:
                    raise RuntimeError("A2A 스트리밍 JSON-RPC 오류")

                result = envelope.get("result") or {}
                elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)

                status_update = result.get("statusUpdate") or result.get("status_update")
                if isinstance(status_update, dict):
                    note_task(status_update.get("taskId") or status_update.get("task_id"))
                    status_obj = status_update.get("status") or {}
                    state_str = str(status_obj.get("state") or "")
                    record_status(state_str)
                    msg_text = _first_part_text((status_obj.get("message") or {}).get("parts"))
                    parsed_detail: Any = None
                    if msg_text:
                        try:
                            parsed_detail = json.loads(msg_text)
                        except json.JSONDecodeError:
                            parsed_detail = msg_text
                    ev = {
                        "type": "statusUpdate",
                        "taskId": task_id,
                        "state": state_str,
                        "detail": parsed_detail,
                        "elapsedMs": elapsed_ms,
                    }
                    progress_events.append(ev)
                    if first_progress_ms is None:
                        first_progress_ms = elapsed_ms
                    if on_progress is not None:
                        await on_progress(ev)
                    continue

                artifact_update = result.get("artifactUpdate") or result.get("artifact_update")
                if isinstance(artifact_update, dict):
                    note_task(artifact_update.get("taskId") or artifact_update.get("task_id"))
                    art = artifact_update.get("artifact") or {}
                    artifact_id = str(art.get("artifactId") or art.get("artifact_id") or "")
                    text = "".join(str(part["text"]) for part in (art.get("parts") or []) if part.get("text"))
                    if artifact_update.get("append") is True:
                        text = artifact_parts.get(artifact_id, "") + text
                    artifact_parts[artifact_id] = text
                    if text and (artifact_update.get("lastChunk") or artifact_update.get("last_chunk")):
                        final_text = artifact_parts.pop(artifact_id)
                        final_artifact_ms = elapsed_ms
                    continue

                task = result.get("task")
                if isinstance(task, dict):
                    note_task(task.get("id"))
                    record_status((task.get("status") or {}).get("state"))
                    for artifact in task.get("artifacts") or []:
                        if (text := _first_part_text(artifact.get("parts"))):
                            final_text = text
                            final_artifact_ms = elapsed_ms
                            break
                message = result.get("message")
                if isinstance(message, dict):
                    direct_message = _first_part_text(message.get("parts"))

        if terminal_state in {"CANCELED", "CANCELLED"}:
            return A2AStreamResult(
                payload={"ok": False, "canceled": True},
                task_id=task_id,
                progress_events=progress_events,
                first_progress_ms=first_progress_ms,
                final_artifact_ms=final_artifact_ms,
                canceled=True,
            )
        if direct_message and task_id is None:
            final_text = direct_message
        elif not final_text:
            raise RuntimeError("A2A 스트리밍 응답에 최종 산출물이 없습니다.")
        elif terminal_state not in {"COMPLETED", "FAILED", "REJECTED"}:
            raise RuntimeError("A2A 스트리밍 응답에 작업 종료 상태가 없습니다.")
        parsed = json.loads(final_text)
        if not isinstance(parsed, dict) or (terminal_state in {"FAILED", "REJECTED"} and parsed.get("ok") is not False):
            raise RuntimeError("A2A 스트리밍 최종 산출물이 작업 상태와 일치하지 않습니다.")

        return A2AStreamResult(
            payload=parsed,
            task_id=task_id,
            progress_events=progress_events,
            first_progress_ms=first_progress_ms,
            final_artifact_ms=final_artifact_ms,
        )

    async with traced_span(
        "a2a.send_streaming_message",
        run_type="chain",
        inputs={"base_url": base_url, "context_id": context_id, "payload": enriched_payload},
        tags=["a2a", "streaming"],
    ) as span:
        try:
            coro = _consume()
            res = await (asyncio.wait_for(coro, timeout=timeout_seconds) if timeout_seconds is not None else coro)
            end_span(
                span,
                outputs={
                    "taskId": res.task_id,
                    "progressCount": len(res.progress_events),
                    "firstProgressMs": res.first_progress_ms,
                    "canceled": res.canceled,
                },
            )
            return res
        except asyncio.TimeoutError as exc:
            end_span(span, error="A2A streaming timeout")
            raise TimeoutError(f"A2A 스트리밍 타임아웃({timeout_seconds}s)") from exc
        except asyncio.CancelledError:
            end_span(span, error="A2A streaming canceled")
            raise


def _first_part_text(parts: Any) -> str:
    """파트 배열에서 첫 번째 텍스트 문자열을 반환한다."""
    if not isinstance(parts, list):
        return ""
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            return str(part["text"])
    return ""
