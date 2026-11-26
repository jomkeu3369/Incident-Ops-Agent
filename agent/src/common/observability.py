from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
import json
import logging
import re
from typing import Any, AsyncIterator, Iterator, Mapping
from urllib.parse import quote, unquote

from langsmith import Client, RunTree, get_current_run_tree, trace
from langsmith.run_helpers import get_tracing_context, tracing_context
from pydantic import SecretStr

from common.config import Settings

logger = logging.getLogger(__name__)

LANGSMITH_TRACE_HEADER = "langsmith-trace"
LANGSMITH_BAGGAGE_HEADER = "baggage"
REDACTED_VALUE = "[REDACTED]"

_BAGGAGE_METADATA_KEYS = frozenset({
    "incident_code", "thread_id", "run_id", "trace_id", "approval_id", "resume", "service",
})
_BAGGAGE_TAGS = frozenset({
    "a2a", "ag-ui", "agentic", "approval-execution", "dr-dci", "execution",
    "log-agent", "log-analyzer-agent", "mcp", "planner", "remediation-agent",
    "resume", "run", "streaming", "supervisor",
})
_BAGGAGE_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")

_SENSITIVE_KEY_TOKENS = (
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "credential",
    "private_key",
)

_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\blsv2_[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bglsa_[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
)


_TOKEN_COUNT_KEYS = frozenset({
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
})


def _is_sensitive_key(key: str, value: Any = None) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    # 숫자형 LLM 토큰 사용량 카운터(input_tokens/output_tokens 등)는 인증 토큰이 아니므로 보존한다
    if (
        (normalized in _TOKEN_COUNT_KEYS or normalized.endswith("_tokens"))
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return False
    return any(tok in normalized for tok in _SENSITIVE_KEY_TOKENS)


def _redact_string(text: str) -> str:
    redacted = text
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        redacted = pattern.sub(REDACTED_VALUE, redacted)
    return redacted


def redact_sensitive_value(value: Any) -> Any:
    """중첩 구조 내 시크릿 키·토큰 패턴·SecretStr을 재귀적으로 마스킹한다."""
    if isinstance(value, SecretStr):
        return REDACTED_VALUE
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if _is_sensitive_key(key_str, v):
                sanitized[key_str] = REDACTED_VALUE
            else:
                sanitized[key_str] = redact_sensitive_value(v)
        return sanitized
    if isinstance(value, list):
        return [redact_sensitive_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive_value(item) for item in value]
    return value


def redact_sensitive_dict(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """LangSmith Client hide_inputs/hide_outputs/hide_metadata 호환 딕셔너리 마스킹 함수."""
    if not payload:
        return {}
    result = redact_sensitive_value(dict(payload))
    return result if isinstance(result, dict) else {}


def _log_tracing_error(exc: Exception) -> None:
    """LangSmith 전송 오류가 에이전트 본 흐름을 중단하지 않도록 경고 로그만 남긴다."""
    logger.warning("LangSmith tracing error ignored (fail-safe): %s", type(exc).__name__)


def build_langsmith_client(settings: Settings) -> Client | None:
    """설정이 활성화된 경우에만 시크릿 마스킹 콜백이 적용된 LangSmith 클라이언트를 생성한다."""
    if not settings.langsmith_enabled:
        return None
    try:
        return Client(
            api_url=settings.langsmith_endpoint,
            api_key=settings.langsmith_api_key.get_secret_value().strip(),
            hide_inputs=redact_sensitive_dict,
            hide_outputs=redact_sensitive_dict,
            hide_metadata=redact_sensitive_dict,
            tracing_error_callback=_log_tracing_error,
        )
    except Exception as exc:
        logger.warning("LangSmith client initialization failed (fail-safe): %s", type(exc).__name__)
        return None


def flush_langsmith_client(client: Any | None) -> None:
    """컨테이너 종료 시 버퍼링된 트레이스를 안전하게 플러시한다."""
    if client is None:
        return
    flush = getattr(client, "flush", None)
    if callable(flush):
        try:
            flush()
        except Exception as exc:
            logger.warning("LangSmith flush failed (fail-safe): %s", type(exc).__name__)


def _sanitize_baggage(raw: str) -> str:
    members: dict[str, str] = {}
    for item in raw.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep or key in members:
            continue
        decoded = unquote(value)
        if key == "langsmith-metadata":
            try:
                metadata = json.loads(decoded)
            except (ValueError, RecursionError):
                continue
            if not isinstance(metadata, dict):
                continue
            allowed = {
                field: content
                for field, content in metadata.items()
                if field in _BAGGAGE_METADATA_KEYS and (
                    isinstance(content, bool) or
                    (isinstance(content, str) and _BAGGAGE_VALUE.fullmatch(content) and _redact_string(content) == content)
                )
            }
            if allowed:
                members[key] = quote(json.dumps(allowed, separators=(",", ":")), safe="")
        elif key == "langsmith-tags":
            tags = [tag for tag in decoded.split(",") if tag in _BAGGAGE_TAGS]
            if tags:
                members[key] = quote(",".join(tags), safe="")
    return ",".join(f"{key}={value}" for key, value in members.items())


def extract_trace_headers() -> dict[str, str]:
    """현재 활성 LangSmith RunTree에서 A2A HTTP 전파용 트레이스 헤더만 추출한다."""
    try:
        if get_tracing_context().get("enabled") is False:
            return {}
        tree = get_current_run_tree()
        if tree is None:
            return {}
        raw = tree.to_headers()
        if not isinstance(raw, Mapping):
            return {}
        return extract_incoming_trace_headers(raw) or {}
    except Exception:
        return {}


def extract_incoming_trace_headers(headers: Mapping[str, str]) -> dict[str, str] | None:
    """수신 HTTP 요청 헤더에서 LangSmith 분산 트레이싱 부모 헤더만 추출한다."""
    lowered = {str(k).lower(): str(v) for k, v in headers.items() if v}
    trace_val = lowered.get(LANGSMITH_TRACE_HEADER, "").strip()
    if not trace_val:
        return None
    extracted = {LANGSMITH_TRACE_HEADER: trace_val}
    baggage_val = _sanitize_baggage(lowered.get(LANGSMITH_BAGGAGE_HEADER, ""))
    if baggage_val:
        extracted[LANGSMITH_BAGGAGE_HEADER] = baggage_val
    return extracted


@contextmanager
def langsmith_tracing_scope(
    settings: Settings,
    *,
    client: Any | None = None,
    parent_headers: Mapping[str, str] | None = None,
    tags: list[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[bool]:
    """요청·그래프 단위 LangSmith 트레이싱 컨텍스트를 연다 (비활성 시 명시적 격리 no-op)."""
    if not settings.langsmith_enabled:
        with tracing_context(enabled=False):
            yield False
        return

    resolved_client = client or build_langsmith_client(settings)
    if resolved_client is None:
        with tracing_context(enabled=False):
            yield False
        return

    safe_meta = redact_sensitive_dict(metadata) if metadata else None
    parent: Any = None
    safe_parent_headers = extract_incoming_trace_headers(parent_headers) if parent_headers else None
    if safe_parent_headers:
        try:
            parent = RunTree.from_headers(
                safe_parent_headers,
                client=resolved_client,
                project_name=settings.langsmith_project,
            )
        except Exception as exc:
            logger.warning("Invalid incoming LangSmith trace header ignored: %s", type(exc).__name__)
            parent = None
    with tracing_context(
        enabled=True,
        client=resolved_client,
        project_name=settings.langsmith_project,
        parent=parent,
        tags=tags,
        metadata=safe_meta,
    ):
        yield True


@asynccontextmanager
async def traced_span(
    name: str,
    run_type: str = "chain",
    *,
    inputs: Mapping[str, Any] | None = None,
    tags: list[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
    client: Any | None = None,
    project_name: str | None = None,
) -> AsyncIterator[Any]:
    """활성 트레이싱 컨텍스트가 있을 때만 자식 스팬을 기록하는 fail-safe 비동기 컨텍스트 매니저."""
    try:
        state = get_tracing_context().get("enabled")
        enabled = state is not False and (bool(state) or client is not None)
    except Exception:
        enabled = False

    if not enabled:
        yield None
        return

    safe_inputs = redact_sensitive_dict(inputs) if inputs else None
    safe_meta = redact_sensitive_dict(metadata) if metadata else None
    try:
        span_cm = trace(
            name=name,
            run_type=run_type,  # type: ignore[arg-type]
            inputs=safe_inputs,
            tags=tags,
            metadata=safe_meta,
            client=client,
            project_name=project_name,
        )
        run = await span_cm.__aenter__()
    except Exception as exc:
        logger.warning("LangSmith span start failed (fail-safe): %s", type(exc).__name__)
        yield None
        return

    caught_exc: BaseException | None = None
    try:
        yield run
    except BaseException as exc:
        caught_exc = exc
        raise
    finally:
        try:
            if caught_exc is not None and run is not None:
                # 원본 예외/트레이스백에 시크릿이 포함될 수 있으므로 마스킹된 오류 문자열만 기록하고
                # __aexit__에는 예외를 넘기지 않아 SDK가 원본 예외로 덮어쓰지 못하게 차단한다
                existing_error = getattr(run, "error", None)
                if not existing_error:
                    end_span(run, error=f"{type(caught_exc).__name__}: {caught_exc}")
                else:
                    run.error = _redact_string(str(existing_error))
            await span_cm.__aexit__(None, None, None)
        except Exception as exit_exc:
            logger.warning("LangSmith span close failed (fail-safe): %s", type(exit_exc).__name__)


def end_span(run: Any | None, outputs: Mapping[str, Any] | None = None, *, error: str | None = None) -> None:
    """스팬 출력을 시크릿 마스킹 후 안전하게 기록한다."""
    if run is None:
        return
    try:
        safe_outputs = redact_sensitive_dict(outputs) if outputs is not None else None
        run.end(outputs=safe_outputs, error=_redact_string(error) if error else None)
    except Exception as exc:
        logger.warning("LangSmith span end failed (fail-safe): %s", type(exc).__name__)
