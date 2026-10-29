"""Доступ к медиа-сервису камер (видео/фото салона) — vcpdata.

Эндпоинты (base = settings.media_base):
  * videoListPoster.php POST  car, timestart, timestop (PHP читает только POST!)
  * photo_list.php      POST  car, timestart, timestop (YYYY-MM-DD HH:MM:SS)

Ответ video: { "CAM": { videoURLs[], posterURLs?, unixTimestarts?, ... } }
Ответ photo: { "CAM": { photoURLs[], unixTimestarts[], unixTimestops[] } }
  (иногда photo — плоский объект без ключа камеры — поддерживаем оба варианта).

unixTimestarts для видео могут отсутствовать — тогда парсим из имени файла
в URL: .../20260818_1787001432_1787002627.mp4

Время видео — часы сервера (nginx-rtmp), оно верное. Время фото — часы камеры
из имени файла (2026_10_26_19_40_24.jpg), а камеры живут на фиксированном
UTC+3 без перехода на зимнее время. photo_list.php же читает имя как
Europe/Kyiv, поэтому зимой его unixTimestarts на час впереди. Реальный сдвиг
часов камеры определяем по Last-Modified (момент записи файла сервером).

Отладка: from app.utils.debug_log import debug_log
"""

from __future__ import annotations

import calendar
import email.utils
import json
import re
import ssl
import urllib.parse
import urllib.request
from datetime import date, datetime

from app.config import get_settings
from app.utils.debug_log import debug_log

settings = get_settings()

_COVERAGE_OK = 0.95
_VIDEO_TS_RE = re.compile(r"_(\d{10})_(\d{10})\.mp4")
_PHOTO_NAME_RE = re.compile(r"(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})\.jpg")

# Часы камер по умолчанию — UTC+3, если сдвиг по Last-Modified не определился.
_CAMERA_CLOCK_DEFAULT_SEC = 3 * 3600
_CLOCK_SAMPLES = 5
# Фото попадает на сервер через секунды после снимка; если даже лучший образец
# дальше от целого часа, значит файлы перезаливали — Last-Modified не верим.
_CLOCK_MAX_RESIDUAL_SEC = 10 * 60
_CLOCK_HEAD_TIMEOUT_SEC = 10
_clock_offset_cache: dict[tuple[str, date], int] = {}


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


def _photo_wall_epoch(rel_url: str) -> int | None:
    """Показания часов камеры из имени фото, как если бы это был UTC."""
    m = _PHOTO_NAME_RE.search(urllib.parse.unquote(rel_url))
    if not m:
        return None
    try:
        dt = datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None
    return calendar.timegm(dt.timetuple())


def _last_modified(rel_url: str) -> int | None:
    req = urllib.request.Request(_abs_url(rel_url), method="HEAD")
    try:
        with _urlopen(req, timeout=_CLOCK_HEAD_TIMEOUT_SEC) as resp:
            raw = resp.headers.get("Last-Modified")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return int(email.utils.parsedate_to_datetime(raw).timestamp())
    except (TypeError, ValueError):
        return None


def _detect_clock_offset(camera: str, samples: list[tuple[str, int]]) -> int:
    """На сколько секунд часы камеры впереди UTC (кратно часу)."""
    if not samples:
        return _CAMERA_CLOCK_DEFAULT_SEC
    n = len(samples)
    picks = sorted({round(i * (n - 1) / max(1, _CLOCK_SAMPLES - 1)) for i in range(_CLOCK_SAMPLES)})
    diffs = []
    for i in picks:
        rel, wall = samples[i]
        mtime = _last_modified(rel)
        if mtime is not None:
            diffs.append(wall - mtime)
    if not diffs:
        debug_log(f"[media] {camera}: Last-Modified недоступен, часы камеры = UTC+3")
        return _CAMERA_CLOCK_DEFAULT_SEC
    # Задержка загрузки только уменьшает разницу — берём образец с минимальной.
    best = max(diffs)
    offset = round(best / 3600) * 3600
    if abs(best - offset) > _CLOCK_MAX_RESIDUAL_SEC:
        debug_log(f"[media] {camera}: сдвиг часов не определён ({best}s), часы камеры = UTC+3")
        return _CAMERA_CLOCK_DEFAULT_SEC
    return offset


def _camera_clock_offset(camera: str, day: date, samples: list[tuple[str, int]]) -> int:
    key = (camera, day)
    cached = _clock_offset_cache.get(key)
    if cached is not None:
        return cached
    offset = _detect_clock_offset(camera, samples)
    if len(_clock_offset_cache) > 5000:
        _clock_offset_cache.clear()
    _clock_offset_cache[key] = offset
    return offset


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
    walls = [_photo_wall_epoch(rel) for rel in urls]
    samples = [(rel, w) for rel, w in zip(urls, walls) if w is not None]
    offset = _camera_clock_offset(camera, day, samples) if samples else 0
    items: list[dict] = []
    for i, rel in enumerate(urls):
        if walls[i] is not None:
            ts = walls[i] - offset
        else:
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
