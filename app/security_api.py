"""Аутентификация внешнего API выручки — статический ключ (X-API-Key).

Ключ задаётся настройкой `revenue_api_key` (из .env). Пустой ключ означает,
что API выключено (503), чтобы случайно не открыть доступ без пароля.
Вызовы предполагаются сервер-сервер, поэтому CORS не настраивается.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from app.config import get_settings


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """FastAPI-dependency: пропускает запрос только с верным X-API-Key."""
    expected = get_settings().revenue_api_key or ""
    if not expected:
        raise HTTPException(status_code=503, detail="API disabled")
    # Сравнение в постоянном времени, чтобы не утекала длина/содержимое ключа.
    if not hmac.compare_digest(str(x_api_key or ""), expected):
        raise HTTPException(status_code=401, detail="Invalid API key")
