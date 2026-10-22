"""Простой debug-лог в `app/logs/` — можно вызывать из любого места проекта.

Пример:
    from app.utils.debug_log import debug_log

    debug_log("ответ videoListPoster", tag="media", data=payload)
    debug_log("строка 68 media_service", location="media_service.fetch_day_videos")

Файлы: app/logs/debug-YYYY-MM-DD.log (один файл на день).
Включение/выключение: DEBUG_LOG_ENABLED в .env (по умолчанию true при DEBUG=true).
"""

from __future__ import annotations

import inspect
import json
import traceback
from datetime import datetime
from pathlib import Path
from threading import Lock

_lock = Lock()
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_MAX_DATA_LEN = 8000


def _enabled() -> bool:
    try:
        from app.config import get_settings

        return get_settings().debug_log_enabled
    except Exception:  # noqa: BLE001
        return True


def _format_data(data) -> str:
    if data is None:
        return ""
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(data)
    if len(text) > _MAX_DATA_LEN:
        text = text[:_MAX_DATA_LEN] + f"... (+{len(text) - _MAX_DATA_LEN} chars)"
    return text


def debug_log(
    message: str,
    *,
    tag: str = "app",
    data=None,
    location: str = "",
) -> None:
    """Записать строку в app/logs/debug-YYYY-MM-DD.log."""
    if not _enabled():
        return

    if not location:
        frame = inspect.currentframe()
        if frame and frame.f_back:
            info = frame.f_back
            location = f"{info.f_code.co_filename}:{info.f_lineno}"

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{tag}] {message}"
    if location:
        line += f" @ {location}"
    extra = _format_data(data)
    if extra:
        line += f" | {extra}"

    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _LOG_DIR / f"debug-{datetime.now():%Y-%m-%d}.log"
        with _lock:
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:
        # Не роняем приложение из-за лога
        print(f"debug_log failed: {exc}")  # noqa: T201


def debug_exc(message: str, *, tag: str = "app", exc: BaseException | None = None) -> None:
    """Записать сообщение + traceback."""
    if exc is None:
        data = traceback.format_exc()
    else:
        data = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    debug_log(message, tag=tag, data={"traceback": data})
