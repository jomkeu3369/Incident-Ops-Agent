from __future__ import annotations

import hmac
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from common.config import INTERNAL_TOKEN_HEADER, OPERATOR_ID_HEADER, Settings
from common.di import get_settings

_internal_token_scheme = APIKeyHeader(name=INTERNAL_TOKEN_HEADER, auto_error=False)


def verify_internal_token(provided: str | None = Depends(_internal_token_scheme), settings: Settings = Depends(get_settings)) -> str:
    """Spring InternalTokenFilter와 동일한 fail-closed 상수시간 내부 토큰 검증."""
    expected = settings.internal_service_token.get_secret_value()
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 내부 서비스 토큰입니다.")
    return provided


def get_operator_id(request: Request, settings: Settings = Depends(get_settings)) -> str:
    """운영자 신원을 서버 측 설정 또는 신뢰 프록시 헤더에서만 결정한다."""
    if settings.trust_operator_header:
        header_val = (request.headers.get(OPERATOR_ID_HEADER) or "").strip()
        if header_val:
            return header_val
    return settings.default_operator_id
