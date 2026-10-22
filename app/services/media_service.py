"""Доступ к медиа-сервису камер (видео/фото салона) — vcpdata.

Эндпоинты (base = settings.media_base):
  * videoListPoster.php POST  car, timestart, timestop (PHP читает только POST!)
  * photo_list.php      POST  car, timestart, timestop (YYYY-MM-DD HH:MM:SS)

Ответ video: { "CAM": { videoURLs[], posterURLs?, unixTimestarts?, ... } }
Ответ photo: { "CAM": { photoURLs[], unixTimestarts[], unixTimestops[] } }
  (иногда photo — плоский объект без ключа камеры — поддерживаем оба варианта).

unixTimestarts для видео могут отсутствовать — тогда парсим из имени файла
в URL: .../20260818_1787001432_1787002627.mp4

Отладка: from app.utils.debug_log import debug_log
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from app.config import get_settings
from app.utils.debug_log import debug_log

settings = get_settings()

_LOCAL_OFFSET = timedelta(hours=3)
_COVERAGE_OK = 0.95
_VIDEO_TS_RE = re.compile(r"_(\d{10})_(\d{10})\.mp4")


def _abs_url(rel: str) -> str:
    return settings.media_base + rel.lstrip("/")


def _ssl_context() -> ssl.SSLContext:
    if settings.media_ssl_verify:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _urlopen(req_or_url, *, timeout: int):
    return urllib.request.urlopen(req_or_url, timeout=timeout, context=_ssl_context())


def _post_form(path: str, fields: dict[str, str]) -> tuple[dict | list | None, str, int]:
    """POST application/x-www-form-urlencoded. Возвращает (json, raw_body, status)."""
    url = _abs_url(path)
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with _urlopen(req, timeout=settings.media_timeout_sec) as resp:
        status = resp.getcode()
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        payload = None
    return payload, raw, status


def _day_bounds_local(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, 0, 0, 0)
    stop = datetime(day.year, day.month, day.day, 23, 59, 59)
    return start, stop


def _camera_block(payload, camera: str) -> tuple[dict | None, str | None]:
    """Блок данных камеры из ответа API или текст ошибки."""
    if not isinstance(payload, dict):
        return None, "ответ не JSON-объект"
    if payload.get("error"):
        return None, str(payload["error"])
    block = payload.get(camera)
    if isinstance(block, dict):
        return block, None
    # Плоский ответ (legacy photo)
    if "photoURLs" in payload or "videoURLs" in payload:
        return payload, None
    keys = list(payload.keys())[:8]
    return None, f"нет блока для {camera!r}, keys={keys}"


def _video_times(rel_url: str, starts: list, stops: list, index: int) -> tuple[int | None, int | None]:
    if index < len(starts) and index < len(stops):
        try:
            return int(starts[index]), int(stops[index])
        except (TypeError, ValueError):
            pass
    m = _VIDEO_TS_RE.search(rel_url)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


# ── сетевые запросы ─────────────────────────────────────────────────

def fetch_day_videos(camera: str, day: date) -> tuple[list[dict], str | None]:
    """Список видео камеры за день. Возвращает (items, error)."""
    start, stop = _day_bounds_local(day)
    fields = {
        "car": camera,
        "timestart": start.strftime("%Y-%m-%d %H:%M:%S"),
        "timestop": stop.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        payload, raw, status = _post_form("videoListPoster.php", fields)
    except Exception as exc:  # noqa: BLE001
        return [], f"video API: {exc}"

    block, block_err = _camera_block(payload, camera)
    if block is None:
        err = f"video API: {block_err}" if block_err else f"video API: пустой ответ для {camera}"
        return [], err

    urls = block.get("videoURLs") or []
    posters = block.get("posterURLs") or []
    starts = block.get("unixTimestarts") or []
    stops = block.get("unixTimestops") or []
    items: list[dict] = []
    for i, rel in enumerate(urls):
        s, e = _video_times(rel, starts, stops, i)
        if s is None or e is None:
            continue
        items.append({
            "kind": "video",
            "camera": camera,
            "url": _abs_url(rel),
            "poster": _abs_url(posters[i]) if i < len(posters) and posters[i] else None,
            "chunk_start_utc": s,
            "chunk_stop_utc": e,
        })
    items.sort(key=lambda x: x["chunk_start_utc"])
    return items, None


def fetch_day_photos(camera: str, day: date) -> tuple[list[dict], str | None]:
    """Список фото камеры за день. Возвращает (items, error)."""
    start, stop = _day_bounds_local(day)
    fields = {
        "car": camera,
        "timestart": start.strftime("%Y-%m-%d %H:%M:%S"),
        "timestop": stop.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        payload, raw, status = _post_form("photo_list.php", fields)
    except Exception as exc:  # noqa: BLE001
        return [], f"photo API: {exc}"

    block, block_err = _camera_block(payload, camera)
    if block is None:
        err = f"photo API: {block_err}" if block_err else f"photo API: пустой ответ для {camera}"
        return [], err

    urls = block.get("photoURLs") or []
    starts = block.get("unixTimestarts") or []
    items: list[dict] = []
    for i, rel in enumerate(urls):
        try:
            ts = int(starts[i])
        except (IndexError, TypeError, ValueError):
            continue
        items.append({
            "kind": "photo",
            "camera": camera,
            "url": _abs_url(rel),
            "utc": ts,
        })
    items.sort(key=lambda x: x["utc"])
    return items, None


# ── подбор под окно ─────────────────────────────────────────────────

def _videos_in_window(videos: list[dict], start: int, stop: int) -> list[dict]:
    out = []
    for v in videos:
        cs, ce = v["chunk_start_utc"], v["chunk_stop_utc"]
        if cs < stop and ce > start:
            item = dict(v)
            item["seek_from_sec"] = max(0, start - cs)
            item["seek_to_sec"] = min(ce - cs, stop - cs)
            out.append(item)
    return out


def _photos_in_window(photos: list[dict], start: int, stop: int) -> list[dict]:
    return [p for p in photos if start <= p["utc"] <= stop]


def _video_coverage(videos: list[dict], start: int, stop: int) -> float:
    window = max(1, stop - start)
    intervals = sorted(
        (max(start, v["chunk_start_utc"]), min(stop, v["chunk_stop_utc"]))
        for v in videos
    )
    covered = 0
    cur_end = start
    for s, e in intervals:
        if e <= s:
            continue
        s = max(s, cur_end)
        if e > s:
            covered += e - s
            cur_end = e
    return covered / window


def pick_for_window(
    videos: list[dict],
    photos: list[dict],
    start: int,
    stop: int,
    source: str,
) -> tuple[list[dict], str]:
    """Медиа под окно + статус (ok | partial | no_media)."""
    want_video = source in ("video", "photoAndVideo")
    want_photo = source in ("photo", "photoAndVideo")

    media: list[dict] = []
    coverage = 0.0

    if want_video:
        vids = _videos_in_window(videos, start, stop)
        media.extend(vids)
        coverage = _video_coverage(vids, start, stop)

    if want_photo and (source == "photo" or coverage < _COVERAGE_OK):
        media.extend(_photos_in_window(photos, start, stop))

    if not media:
        return [], "no_media"

    if source == "photo":
        return media, "ok"
    if coverage >= _COVERAGE_OK:
        return media, "ok"
    return media, "partial"
