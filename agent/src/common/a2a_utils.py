from __future__ import annotations

import hmac
import json
import uuid
from typing import Any
import httpx
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, Message, Part, Role
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, PROTOCOL_VERSION_CURRENT, VERSION_HEADER

from common.config import INTERNAL_TOKEN_HEADER


def mount_a2a_agent(app: FastAPI, *, agent_card: AgentCard, executor: AgentExecutor) -> None:
    """FastAPI 앱에 A2A v1.0 AgentCard·JSON-RPC 라우트와 내부 토큰 검증 미들웨어를 마운트한다."""

    @app.middleware("http")
    async def verify_a2a_rpc_token(request: Request, call_next):
        path = request.url.path
        # 공개 헬스체크·에이전트 카드와 자체 Depends를 쓰는 /internal 경로는 통과시킨다
        if path in ("/health", AGENT_CARD_WELL_KNOWN_PATH) or path.startswith("/internal/"):
            return await call_next(request)
        expected = request.app.state.container.settings.internal_service_token.get_secret_value()
        provided = request.headers.get(INTERNAL_TOKEN_HEADER)
        if not expected or not provided or not hmac.compare_digest(expected, provided):
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"status": 401, "error": "Unauthorized", "message": "유효하지 않은 내부 서비스 토큰입니다."})
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
    headers = {"Content-Type": "application/json", VERSION_HEADER: PROTOCOL_VERSION_CURRENT, INTERNAL_TOKEN_HEADER: internal_token}
    resp = await http_client.post(f"{base_url.rstrip('/')}/", json=rpc_body, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"A2A 호출 실패 (HTTP {resp.status_code})")

    body = resp.json()
    if "error" in body:
        err = body["error"]
        raise RuntimeError(f"A2A JSON-RPC 오류: {err.get('message', err)}")

    text = _extract_text_from_a2a_result(body.get("result", {}))
    if not text:
        raise RuntimeError("A2A 응답에 텍스트 결과가 없습니다.")
    return json.loads(text)


def _extract_text_from_a2a_result(result: dict[str, Any]) -> str:
    """A2A SendMessageResponse의 message 또는 task에서 첫 텍스트 파트를 추출한다."""
    msg = result.get("message")
    if isinstance(msg, dict):
        text = _first_part_text(msg.get("parts"))
        if text:
            return text
    task = result.get("task")
    if isinstance(task, dict):
        status_msg = (task.get("status") or {}).get("message")
        if isinstance(status_msg, dict):
            text = _first_part_text(status_msg.get("parts"))
            if text:
                return text
        for artifact in task.get("artifacts") or []:
            text = _first_part_text(artifact.get("parts"))
            if text:
                return text
    return ""


def _first_part_text(parts: Any) -> str:
    """파트 배열에서 첫 번째 텍스트 문자열을 반환한다."""
    if not isinstance(parts, list):
        return ""
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            return str(part["text"])
    return ""
