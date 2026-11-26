from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"

# 내부 서비스 간 인증, 역할 권한 분리 및 운영자 식별 헤더 상수
INTERNAL_TOKEN_HEADER = "X-Internal-Token"
AGENT_ROLE_HEADER = "X-Agent-Role"
OPERATOR_ID_HEADER = "X-Operator-Id"
VALID_EVIDENCE_JUDGE_MODES = frozenset({"decisions_api", "structured_output", "rule_based", "auto"})
VALID_DR_DCI_MODES = frozenset({"keyword", "agentic"})
VALID_DR_DCI_SELECTOR_MODES = frozenset({"adaptive", "openai"})


@dataclass(frozen=True)
class ServiceExternalConfig:
    """서비스별 외부 API(GitHub·Grafana·Calendar) 연결 정보."""

    service_name: str
    github_repo: str
    github_environment: str
    grafana_prometheus_url: str
    grafana_service_label: str
    google_calendar_id: str


class Settings(BaseSettings):
    """supervisor·log_agent·remediation_agent 공통 런타임 설정."""

    model_config = SettingsConfigDict(env_file=_ROOT_ENV if _ROOT_ENV.is_file() else None, env_file_encoding="utf-8", extra="ignore")

    # LLM 및 판정 모델 설정
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini"
    decisions_model: str = "gpt-6-luna"
    evidence_judge_mode: str = "decisions_api"
    evidence_judge_model: str = ""

    # LangSmith 관측성·분산 트레이싱 설정 (기본 비활성, opt-in)
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "incident-ops-agent"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    @property
    def log_agent_token(self) -> str:
        """조사 워커의 자격증명: 전용 읽기 토큰이 있으면 그것만, 없으면 공유 토큰."""
        return (
            self.internal_investigator_token.get_secret_value().strip()
            or self.internal_service_token.get_secret_value()
        )

    @property
    def langsmith_enabled(self) -> bool:
        """LANGSMITH_TRACING=true이고 유효한 API 키가 설정된 경우에만 트레이싱을 활성화한다."""
        key = self.langsmith_api_key.get_secret_value().strip()
        return self.langsmith_tracing and bool(key) and key != "your-langsmith-api-key-here"

    @field_validator("evidence_judge_mode")
    @classmethod
    def _validate_evidence_judge_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_EVIDENCE_JUDGE_MODES:
            allowed = ", ".join(sorted(VALID_EVIDENCE_JUDGE_MODES))
            raise ValueError(f"EVIDENCE_JUDGE_MODE must be one of: {allowed}")
        return normalized

    # 보안 및 운영자 식별 설정
    internal_service_token: SecretStr = SecretStr("")
    internal_investigator_token: SecretStr = SecretStr("")
    default_operator_id: str = "oncall-operator"
    trust_operator_header: bool = False
    cors_allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost", "http://localhost:3000"])

    # 인프라 및 서비스 엔드포인트
    redis_url: str = "redis://localhost:6379/0"
    backend_base_url: str = ""
    spring_mcp_sse_url: str = "http://localhost:8080/sse"
    log_agent_a2a_url: str = "http://localhost:8001"
    remediation_agent_a2a_url: str = "http://localhost:8002"

    # DR-DCI 및 플래너 제한 상수
    dr_dci_mode: str = "keyword"
    dr_dci_selector_mode: str = "adaptive"
    dr_dci_max_chunk_chars: int = 4000
    dr_dci_max_pull_steps: int = 5
    dr_dci_max_context_chars: int = 6000
    dr_dci_timeout_seconds: float = 10.0
    max_replan_attempts: int = 3

    @field_validator("dr_dci_mode")
    @classmethod
    def _validate_dr_dci_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_DR_DCI_MODES:
            allowed = ", ".join(sorted(VALID_DR_DCI_MODES))
            raise ValueError(f"DR_DCI_MODE must be one of: {allowed}")
        return normalized

    @field_validator("dr_dci_selector_mode")
    @classmethod
    def _validate_dr_dci_selector_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_DR_DCI_SELECTOR_MODES:
            allowed = ", ".join(sorted(VALID_DR_DCI_SELECTOR_MODES))
            raise ValueError(f"DR_DCI_SELECTOR_MODE must be one of: {allowed}")
        return normalized
    http_timeout_seconds: float = 15.0
    tool_cache_ttl_seconds: float = 300.0

    # 외부 API Tool 설정 (Phase 2A)
    default_service_name: str = "payment-service"
    external_service_map_json: str = ""
    github_api_base_url: str = "https://api.github.com"
    github_token: SecretStr = SecretStr("")
    github_repo: str = ""
    github_environment: str = "production"
    grafana_prometheus_url: str = ""
    grafana_instance_id: str = ""
    grafana_metrics_token: SecretStr = SecretStr("")
    grafana_metric_name: str = "http_requests_total"
    google_calendar_base_url: str = "https://www.googleapis.com/calendar/v3"
    google_calendar_id: str = ""
    google_calendar_token: SecretStr = SecretStr("")
    calendar_adapter_mode: str = "google"
    local_freeze_calendar_path: str = ""

    @model_validator(mode="after")
    def _derive_backend_base_url(self) -> "Settings":
        """BACKEND_BASE_URL 미지정 시 SPRING_MCP_SSE_URL의 origin에서 REST 기준 URL을 유도한다."""
        if not self.backend_base_url.strip():
            parsed = urlsplit(self.spring_mcp_sse_url)
            self.backend_base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "http://localhost:8080"
        return self

    def resolve_service_config(self, service_name: str) -> ServiceExternalConfig | None:
        """서비스 식별자로 GitHub·Grafana·Calendar 설정을 조회한다."""
        name = service_name.strip()
        if not name:
            return None

        if self.external_service_map_json.strip():
            try:
                raw_map = json.loads(self.external_service_map_json)
            except json.JSONDecodeError:
                raw_map = {}
            entry = raw_map.get(name) if isinstance(raw_map, dict) else None
            if isinstance(entry, dict):
                return ServiceExternalConfig(
                    service_name=name,
                    github_repo=str(entry.get("github_repo", self.github_repo)),
                    github_environment=str(entry.get("github_environment", self.github_environment)),
                    grafana_prometheus_url=str(entry.get("grafana_prometheus_url", self.grafana_prometheus_url)),
                    grafana_service_label=str(entry.get("grafana_service_label", name)),
                    google_calendar_id=str(entry.get("google_calendar_id", self.google_calendar_id)),
                )

        if name == self.default_service_name:
            return ServiceExternalConfig(
                service_name=name,
                github_repo=self.github_repo,
                github_environment=self.github_environment,
                grafana_prometheus_url=self.grafana_prometheus_url,
                grafana_service_label=name,
                google_calendar_id=self.google_calendar_id,
            )
        return None
