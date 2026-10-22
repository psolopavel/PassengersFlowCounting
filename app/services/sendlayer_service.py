"""Тонкий клиент SendLayer API (POST /email).

Знает только формат API: https://developers.sendlayer.com/api-reference/endpoint/email
Ключ — SENDLAYER_API_KEY в .env, адрес отправителя должен быть на
верифицированном в SendLayer домене (mail.vcpserver.net.ua).
Лимит SendLayer: 10 МБ на письмо вместе с вложениями.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache

import certifi

from app.config import get_settings

_MAX_ERROR_BODY = 1000


@lru_cache
def _ssl_context() -> ssl.SSLContext:
    # Корневые сертификаты из certifi: у Python в venv на сервере системного
    # хранилища может не быть (CERTIFICATE_VERIFY_FAILED: unable to get local issuer).
    return ssl.create_default_context(cafile=certifi.where())


class SendLayerError(Exception):
    """Ошибка отправки. retryable=False — повтор не поможет (4xx, кроме 429)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime_type: str


def _addresses(emails: list[str]) -> list[dict]:
    return [{"email": e} for e in emails]


def send_email(
    *,
    to: list[str],
    subject: str,
    html: str,
    text: str = "",
    attachments: list[Attachment] | None = None,
    bcc: list[str] | None = None,
    reply_to: str = "",
    tags: list[str] | None = None,
) -> str:
    """Отправляет письмо, возвращает MessageID SendLayer."""
    settings = get_settings()
    if not settings.sendlayer_api_key:
        raise SendLayerError("SENDLAYER_API_KEY не задано", retryable=False)
    if not to:
        raise SendLayerError("Порожній список отримувачів", retryable=False)

    payload: dict = {
        "From": {
            "name": settings.report_from_name,
            "email": settings.report_from_email,
        },
        "To": _addresses(to),
        "Subject": subject,
        "ContentType": "HTML",
        "HTMLContent": html,
    }
    if text:
        payload["PlainContent"] = text
    if bcc:
        payload["BCC"] = _addresses(bcc)
    if reply_to:
        payload["ReplyTo"] = _addresses([reply_to])
    if tags:
        payload["Tags"] = tags
    if attachments:
        payload["Attachments"] = [
            {
                "Filename": a.filename,
                "Content": base64.b64encode(a.content).decode("ascii"),
                "Type": a.mime_type,
                "Disposition": "attachment",
            }
            for a in attachments
        ]

    request = urllib.request.Request(
        settings.sendlayer_api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.sendlayer_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=settings.sendlayer_timeout_sec, context=_ssl_context(),
        ) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:_MAX_ERROR_BODY]
        retryable = exc.code == 429 or exc.code >= 500
        raise SendLayerError(
            f"SendLayer HTTP {exc.code}: {detail}", status=exc.code, retryable=retryable,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SendLayerError(f"SendLayer недоступний: {exc}") from exc

    try:
        message_id = (json.loads(body) or {}).get("MessageID")
    except (json.JSONDecodeError, AttributeError):
        message_id = None
    return str(message_id or "")
