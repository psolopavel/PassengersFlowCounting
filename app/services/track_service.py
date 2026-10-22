"""Чтение GPS-трека машины за день из БД gps (read-only).

Поток:
  1. gps.trackers по реальному IMEI -> (tracker_id, tracker_table).
     ВАЖНО: в динамической таблице трека колонка `imei` хранит tracker_id
     (маленькое число), а НЕ реальный IMEI — так устроена gps-БД.
  2. gps.{tracker_table} (например `_car_918`) — точки за окно дня:
       WHERE imei = :tracker_id AND epochtime BETWEEN :start AND :end.

Трек возвращаем в формате, который ждёт gap_fill_service / route_match_service:
    {
        "lat":   [...],   # широта
        "lon":   [...],   # долгота
        "time":  [...],   # unix (сек)
        "speed": [...],   # км/ч
        "interpolated": [0, ...],  # 1 у точек, добавленных починкой
    }
"""

from __future__ import annotations

import math
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.utils.geo import haversine

# Имя динамической таблицы приходит из БД, но подставляется в SQL как
# идентификатор — поэтому строго валидируем формат перед использованием.
_TABLE_RE = re.compile(r"^[A-Za-z0-9_]+$")


def convert_coord(lat_raw: float, lon_raw: float) -> tuple[float, float] | None:
    """NMEA ddmm.mmmm -> десятичные градусы (порт convertcoord из functions.php).

    В gps-БД координаты хранятся как градусы+минуты слитно (напр. 4643.1861 =
    46°43.1861′). Возвращает (lat, lon) в десятичных градусах или None, если
    после конвертации значения выходят за допустимый диапазон.
    """
    n_lat = lat_raw < 0
    n_lon = lon_raw < 0
    lat = abs(lat_raw) / 100.0
    lon = abs(lon_raw) / 100.0

    lat_hi = math.floor(lat)
    lat = round(lat_hi + (lat - lat_hi) / 0.6, 6)
    lon_hi = math.floor(lon)
    lon = round(lon_hi + (lon - lon_hi) / 0.6, 6)

    lat_ok = (0 <= lat <= 90) or n_lat
    lon_ok = (0 <= lon <= 180) or n_lon
    if not (lat_ok and lon_ok):
        return None

    if n_lat:
        lat = -lat
    if n_lon:
        lon = -lon
    return lat, lon


def get_tracker(gps_db: Session, imei: str) -> tuple[int, str] | None:
    """(tracker_id, tracker_table) по реальному IMEI или None."""
    row = gps_db.execute(
        text(
            "SELECT `id`, `tracker_table` FROM `trackers` "
            "WHERE `imei` = :imei LIMIT 1"
        ),
        {"imei": str(imei).strip()},
    ).first()
    if row is None:
        return None
    tracker_id, table = int(row[0]), (row[1] or "").strip()
    if not table or not _TABLE_RE.match(table):
        return None
    return tracker_id, table


def fetch_track(
    gps_db: Session,
    tracker_id: int,
    tracker_table: str,
    start_unix: int,
    end_unix: int,
) -> dict:
    """Точки трека за [start_unix, end_unix]. Пустые массивы, если данных нет."""
    if not _TABLE_RE.match(tracker_table):
        raise ValueError(f"invalid tracker table: {tracker_table!r}")

    sql = text(
        f"SELECT `latitude`, `longtitude`, `speed`, `epochtime` "  # noqa: S608 — имя валидировано
        f"FROM `{tracker_table}` "
        f"WHERE `imei` = :tid AND `epochtime` BETWEEN :s AND :e "
        f"ORDER BY `epochtime` ASC"
    )
    rows = gps_db.execute(
        sql, {"tid": tracker_id, "s": int(start_unix), "e": int(end_unix)}
    ).all()

    lat: list[float] = []
    lon: list[float] = []
    tim: list[int] = []
    spd: list[float] = []
    for r in rows:
        try:
            la_raw = float(r[0])
            lo_raw = float(r[1])
            ep = int(r[3])
        except (TypeError, ValueError):
            continue
        if la_raw == 0 and lo_raw == 0:
            # нулевые координаты — нет фикса GPS, для трека бесполезны
            continue
        conv = convert_coord(la_raw, lo_raw)
        if conv is None:
            continue
        la, lo = conv
        if la == 0 and lo == 0:
            continue
        lat.append(la)
        lon.append(lo)
        spd.append(float(r[2]) if r[2] is not None else 0.0)
        tim.append(ep)

    return {
        "lat": lat,
        "lon": lon,
        "time": tim,
        "speed": spd,
        "interpolated": [0] * len(lat),
    }


def get_day_track(
    gps_db: Session,
    imei: str,
    start_unix: int,
    end_unix: int,
) -> tuple[dict, str | None]:
    """Трек машины за окно дня + источник-таблица (или причина отсутствия).

    Возвращает (track, source_table). source_table = None, если трекер не найден.
    """
    tracker = get_tracker(gps_db, imei)
    if tracker is None:
        return {"lat": [], "lon": [], "time": [], "speed": [], "interpolated": []}, None
    tracker_id, table = tracker
    track = fetch_track(gps_db, tracker_id, table, start_unix, end_unix)
    return track, table


def track_distance_km(
    track: dict,
    start_utc: int | None = None,
    end_utc: int | None = None,
) -> float:
    """Суммарная длина пути по GPS-точкам (км) за интервал времени.

    Считается как сумма расстояний между соседними точками трека (haversine).
    Если заданы ``start_utc`` / ``end_utc``, учитываются только «прыжки» между
    точками, чей временной интервал пересекается с окном.
    """
    lats = track.get("lat", [])
    lons = track.get("lon", [])
    times = track.get("time", [])
    n = len(lats)
    if n < 2 or len(lons) != n or len(times) != n:
        return 0.0

    total_m = 0.0
    for i in range(1, n):
        t_prev = int(times[i - 1])
        t_curr = int(times[i])
        if start_utc is not None and t_curr < start_utc:
            continue
        if end_utc is not None and t_prev > end_utc:
            continue
        total_m += haversine(lats[i - 1], lons[i - 1], lats[i], lons[i])
    return round(total_m / 1000.0, 2)


def _interpolated_time_ranges(track: dict) -> list[tuple[int, int]]:
    """Неперервні інтервали часу, покриті точками з interpolated=1."""
    times = track.get("time") or []
    interp = track.get("interpolated") or [0] * len(times)
    if len(interp) != len(times):
        interp = [0] * len(times)
    ranges: list[tuple[int, int]] = []
    i = 0
    n = len(times)
    while i < n:
        if interp[i] != 1:
            i += 1
            continue
        j = i
        while j < n and interp[j] == 1:
            j += 1
        t0 = int(times[i])
        t1 = int(times[j - 1]) if j > i else t0
        ranges.append((t0, t1))
        i = j
    return ranges


def segment_gap_generated(
    track: dict,
    start_utc: int,
    end_utc: int,
    min_overlap_fraction: float = 0.5,
) -> bool:
    """Чи відрізок побудований переважно по дорисованому (gap-fill) треку.

    True, якщо ≥ min_overlap_fraction тривалості [start_utc, end_utc]
    перетинається з інтервалами, де в треку лише згенеровані точки.
    """
    if end_utc <= start_utc:
        return False
    seg_len = end_utc - start_utc
    overlap = 0
    for r0, r1 in _interpolated_time_ranges(track):
        lo = max(start_utc, r0)
        hi = min(end_utc, r1)
        if hi > lo:
            overlap += hi - lo
    return overlap / seg_len >= min_overlap_fraction
