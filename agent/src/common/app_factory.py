from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from common.config import Settings
from common.di import build_container

logger = logging.getLogger(__name__)


def create_app(service_name: str, *, settings: Settings | None = None, enable_redis: bool = False) -> FastAPI:
    """DI 컨테이너, CORS, 보안 헤더, 안전한 예외 처리를 갖춘 FastAPI 앱을 생성한다."""
    cfg = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = build_container(cfg, enable_redis=enable_redis)
        try:
            yield
        finally:
            await app.state.container.aclose()

    app = FastAPI(title=f"Incident-Ops-Agent ({service_name})", version="0.1.0", lifespan=lifespan)

    # 허용된 출처만 열어 브라우저 요청을 제한한다
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", "X-Internal-Token", "X-Operator-Id"],
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # 프론트엔드 API 명세(§4)에 맞춰 요청 스키마 검증 오류를 HTTP 400으로 통일한다
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"status": 400, "error": "Bad Request", "message": "요청 형식이 올바르지 않습니다."})

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"status": exc.status_code, "error": "HTTP Error", "message": str(exc.detail)})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        # 내부 스택트레이스·토큰 노출을 막기 위해 서버 로그에만 기록한다
        logger.exception("서비스 처리 중 예기치 않은 오류 발생: %s", service_name)
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"status": 500, "error": "Internal Server Error", "message": "요청 처리 중 내부 오류가 발생했습니다."})

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        """헬스체크용 공개 엔드포인트."""
        return {"status": "ok", "service": service_name}

    return app
