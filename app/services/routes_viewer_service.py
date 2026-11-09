"""Підписане посилання на перегляд маршрутів і треку машини в RoutesAndAdvert.

Токен: base64url(json payload) + "." + base64url(HMAC-SHA256(secret, payload_b64)).
Payload: {"v": 1, "imei": ..., "exp": unix, "sub": "pfc:<user_id>"}.
Перевірка — RoutesAndAdvert/includes/viewer_token.php; формат має збігатися.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import date
from urllib.parse import urlencode

from app.config import get_settings


def is_enabled() -> bool:
    s = get_settings()
    return bool(s.routes_viewer_url.strip() and s.routes_viewer_secret.strip())


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_token(imei: str, user_id: int) -> str:
    s = get_settings()
    payload = {
        "v": 1,
        "imei": str(imei).strip(),
        "exp": int(time.time()) + max(1, int(s.routes_viewer_ttl_min)) * 60,
        "sub": f"pfc:{int(user_id)}",
    }
    payload_b64 = _b64url(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    sig = hmac.new(
        s.routes_viewer_secret.strip().encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_b64}.{_b64url(sig)}"


def viewer_url(imei: str, user_id: int, day: date | None = None) -> str:
    s = get_settings()
    params = {"t": make_token(imei, user_id)}
    if day is not None:
        params["date"] = day.isoformat()
    base = s.routes_viewer_url.strip()
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"
