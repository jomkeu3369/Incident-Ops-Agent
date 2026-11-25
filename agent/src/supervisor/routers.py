from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from ag_ui.core import RunAgentInput

from common.anomaly import SessionAnomalyAnalyzer
from common.backend_client import BackendClient
from common.config import Settings
from common.di import AppContainer, get_backend_client, get_container, get_settings
from common.security import get_operator_id
from supervisor.cruds import StoreUnavailableError, SupervisorRedisStore
from supervisor.schemas import ManagedPlanCreate, ManagedPlanItem, ManagedPlanMutationResponse, ManagedPlanUpdate, SessionAnomalyResponse, SessionLogsResponse, SessionSummary
from supervisor.services import SupervisorService

router = APIRouter(prefix="/api/ag-ui", tags=["ag-ui"])


def get_supervisor_store(container: AppContainer = Depends(get_container)) -> SupervisorRedisStore:
    """Redis 기반 총괄 저장소(메모리·실행 로그·계획 CRUD)를 DI로 주입한다."""
    if container.supervisor_store is None:
        container.supervisor_store = SupervisorRedisStore(container.redis_client)
    return container.supervisor_store


def get_supervisor_service(container: AppContainer = Depends(get_container), backend: BackendClient = Depends(get_backend_client), store: SupervisorRedisStore = Depends(get_supervisor_store)) -> SupervisorService:
    """AG-UI·LangGraph 총괄 오케스트레이션 서비스를 DI로 주입한다."""
    return SupervisorService(
        container.settings,
        container.http_client,
        backend,
        store,
        event_bus=container.event_bus,
        a2a_caller=container.a2a_caller,
        langsmith_client=container.langsmith_client,
    )


@router.get("/status")
async def ag_ui_status(operator_id: str = Depends(get_operator_id), cfg: Settings = Depends(get_settings)) -> dict[str, str]:
    """AG-UI 총괄 서비스 상태와 서버 측 운영자 식별자를 확인한다."""
    return {
        "service": "supervisor-agent",
        "protocol": "ag-ui",
        "operator": operator_id,
        "model": cfg.openai_model,
        "langsmith": "enabled" if cfg.langsmith_enabled else "disabled",
    }


@router.post("")
async def run_ag_ui(run_input: RunAgentInput, request: Request, operator_id: str = Depends(get_operator_id), service: SupervisorService = Depends(get_supervisor_service)) -> StreamingResponse:
    """AG-UI 표준 SSE 엔드포인트 (계획 요청 · 승인/반려 · 새로고침 복원)."""
    props = service.parse_forwarded_props(run_input)
    decided = await service.prepare_approval_decision(props, operator_id)
    stream = service.stream_ag_ui(run_input, props, operator_id, decided, request.headers.get("accept"))
    return StreamingResponse(stream, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- 세션 실행 로그 조회·삭제 API (수정 API 미제공) ---

@router.get("/sessions", response_model=list[SessionSummary])
async def list_sessions(store: SupervisorRedisStore = Depends(get_supervisor_store)) -> list[SessionSummary]:
    """저장된 실행 로그 세션 목록을 조회한다."""
    return await store.list_sessions()


@router.get("/sessions/{session_id}", response_model=SessionLogsResponse)
async def get_session_logs(session_id: str, store: SupervisorRedisStore = Depends(get_supervisor_store)) -> SessionLogsResponse:
    """특정 세션의 실행 로그 상세를 실행 순서대로 조회한다."""
    events = await store.get_session_logs(session_id)
    if not events:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"실행 로그를 찾을 수 없습니다: {session_id}")
    return SessionLogsResponse(sessionId=session_id, events=events)


def get_anomaly_analyzer(cfg: Settings = Depends(get_settings)) -> SessionAnomalyAnalyzer:
    """세션 실행 로그 이상 징후 분석기를 DI로 주입한다."""
    return SessionAnomalyAnalyzer(settings=cfg)


@router.get("/sessions/{session_id}/anomalies", response_model=SessionAnomalyResponse)
async def analyze_session_anomalies(
    session_id: str,
    store: SupervisorRedisStore = Depends(get_supervisor_store),
    analyzer: SessionAnomalyAnalyzer = Depends(get_anomaly_analyzer),
) -> SessionAnomalyResponse:
    """저장된 세션 실행 로그의 이상 징후를 규칙 기반으로 판정하고 이상 건에만 3문장 진단을 반환한다."""
    try:
        events, is_complete = await store.get_session_logs_for_analysis(session_id)
    except StoreUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if not events:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"실행 로그를 찾을 수 없습니다: {session_id}")
    report = await analyzer.analyze(session_id, events)
    if not is_complete and report.status != "ANOMALY":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis 연결이 일시 중단되어 전체 실행 로그를 확인할 수 없으므로 정상/판정 불가 응답을 보류합니다.",
        )
    return SessionAnomalyResponse.model_validate(report.to_dict())


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session_logs(session_id: str, store: SupervisorRedisStore = Depends(get_supervisor_store)) -> None:
    """특정 세션의 실행 로그를 삭제한다."""
    try:
        deleted = await store.delete_session_logs(session_id)
    except StoreUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"실행 로그를 찾을 수 없습니다: {session_id}")


# --- 플래너 작업 계획 관리 API (날짜 포함, 시간 겹침 경고) ---

@router.get("/plans", response_model=list[ManagedPlanItem])
async def list_managed_plans(store: SupervisorRedisStore = Depends(get_supervisor_store)) -> list[ManagedPlanItem]:
    """저장된 플래너 작업 계획 목록을 조회한다."""
    return await store.list_plans()


@router.get("/plans/{plan_id}", response_model=ManagedPlanItem)
async def get_managed_plan(plan_id: str, store: SupervisorRedisStore = Depends(get_supervisor_store)) -> ManagedPlanItem:
    """단건 플래너 작업 계획을 조회한다."""
    item = await store.get_plan(plan_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"계획을 찾을 수 없습니다: {plan_id}")
    return item


@router.post("/plans", response_model=ManagedPlanMutationResponse, status_code=status.HTTP_201_CREATED)
async def create_managed_plan(body: ManagedPlanCreate, store: SupervisorRedisStore = Depends(get_supervisor_store)) -> ManagedPlanMutationResponse:
    """작업 계획을 저장하며, 같은 날짜에 시간이 겹치는 계획이 있으면 경고와 함께 반환한다."""
    try:
        return await store.create_plan(body)
    except StoreUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.patch("/plans/{plan_id}", response_model=ManagedPlanMutationResponse)
async def update_managed_plan(plan_id: str, body: ManagedPlanUpdate, store: SupervisorRedisStore = Depends(get_supervisor_store)) -> ManagedPlanMutationResponse:
    """저장된 작업 계획을 수정한다."""
    try:
        res = await store.update_plan(plan_id, body)
    except StoreUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"계획을 찾을 수 없습니다: {plan_id}")
    return res


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_managed_plan(plan_id: str, store: SupervisorRedisStore = Depends(get_supervisor_store)) -> None:
    """저장된 작업 계획을 삭제한다."""
    try:
        deleted = await store.delete_plan(plan_id)
    except StoreUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"계획을 찾을 수 없습니다: {plan_id}")
