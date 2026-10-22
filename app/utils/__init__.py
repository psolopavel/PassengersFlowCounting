"""Утилиты приложения.

Исторически это был модуль app/utils.py; теперь это пакет (добавился app/utils/geo.py),
поэтому общие хелперы (загрузка файлов, парсинг) живут здесь, в __init__, чтобы
`from app.utils import clamp_rating, parse_optional_int, save_upload` продолжал работать.
"""

import uuid
from pathlib import Path

from fastapi import UploadFile

# __init__.py лежит в app/utils/, поэтому каталог приложения — на уровень выше.
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def parse_optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def clamp_rating(value: int | None) -> int | None:
    if value is None:
        return None
    return max(1, min(5, value))


def save_upload(file: UploadFile | None) -> str | None:
    """Сохраняет картинку в static/uploads и возвращает публичный URL или None."""
    if file is None or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXT:
        return None
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / name
    with dest.open("wb") as out:
        out.write(file.file.read())
    return f"/static/uploads/{name}"
