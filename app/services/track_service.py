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
        "gnss":  [...],   # статус GNSS точки (normalize_gnss) или None
        "nofix": [(t0, t1), ...],  # интервалы выброшенных точек без фикса
    }

GNSS пишут парсеры (IO 69 у Teltonika, у остальных протоколов выводится из
присланных координат). Точки «нет фикса» несут старые или подставленные
координаты, поэтому в трек не попадают: на их месте остаётся разрыв, а не
ложная стоянка. Списки, которые собирают трек заново (починка), gnss и nofix
не переносят — читать их только через fix_flags / track.get.
"""

from __future__ import annotations

import math
import re
from datetime import date, time, timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.utils.geo import haversine
from app.utils.local_tz import combine_local, unix_to_local

# Имя динамической таблицы приходит из БД, но подставляется в SQL как
# идентификатор — поэтому строго валидируем формат перед использованием.
_TABLE_RE = re.compile(r"^[A-Za-z0-9_]+$")
# Таблицы трека, в которых нет колонки GNSS (старые) — читаем без неё.
_TABLES_WITHOUT_GNSS: set[str] = set()

# Пробег «скорость × время» — те же пороги, что в servergps/revenue3.php,
# чтобы километраж совпадал с уже существующей страницей.
_SPEED_GPS_ERROR_KMH = 190   # точка со скоростью >= — ошибка GPS, отбрасываем
_SPEED_MOVING_KMH = 2        # средняя скорость пары ниже — стоянка/шум
_SPEED_MAX_GAP_SEC = 300     # пара точек дальше 5 мин — разрыв, не считаем

# Статус GNSS (Teltonika IO 69). 0 неоднозначен: так же пишется «парсер не
# передал» (дефолт ParserEngine), поэтому считается неизвестным.
GNSS_OFF = 0
GNSS_FIX = 1
GNSS_NO_FIX = 2
GNSS_SLEEP = 3


def normalize_gnss(raw) -> int | None:
    """Значение колонки GNSS (varchar) -> 0..3 или None, если не передано/мусор.

    Parser20301 до исправления читал однобайтовый IO 69 двумя байтами, захватывая
    следующий байт пакета: статус — в старшем байте (257 -> 1, 513 -> 2).
    """
    if raw is None:
        return None
    try:
        v = int(float(str(raw).strip()))
    except ValueError:
        return None
    if 0 <= v <= 3:
        return v
    if 256 <= v <= 1023:
        return v >> 8
    return None


def fix_flags(track: dict) -> list[bool]:
    """Для каждой точки: координаты подтверждены приёмником (фикс или сон на месте)."""
    n = len(track.get("lat") or [])
    gnss = track.get("gnss")
    if not gnss or len(gnss) != n:
        return [False] * n
    return [g in (GNSS_FIX, GNSS_SLEEP) for g in gnss]


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

    params = {"tid": tracker_id, "s": int(start_unix), "e": int(end_unix)}

    def select(gnss_col: str):
        return gps_db.execute(text(
            f"SELECT `latitude`, `longtitude`, `speed`, `epochtime`, {gnss_col} "  # noqa: S608 — имя валидировано
            f"FROM `{tracker_table}` "
            f"WHERE `imei` = :tid AND `epochtime` BETWEEN :s AND :e "
            f"ORDER BY `epochtime` ASC"
        ), params).all()

    if tracker_table in _TABLES_WITHOUT_GNSS:
        rows = select("NULL")
    else:
        try:
            rows = select("`GNSS`")
        except DBAPIError as exc:
            if "Unknown column" not in str(exc):
                raise
            _TABLES_WITHOUT_GNSS.add(tracker_table)
            rows = select("NULL")

    lat: list[float] = []
    lon: list[float] = []
    tim: list[int] = []
    spd: list[float] = []
    gns: list[int | None] = []
    nofix: list[tuple[int, int]] = []
    run: list[int] | None = None  # текущая серия точек без фикса [t0, t1]
    for r in rows:
        try:
            la_raw = float(r[0])
            lo_raw = float(r[1])
            ep = int(r[3])
        except (TypeError, ValueError):
            continue
        g = normalize_gnss(r[4])
        if g == GNSS_NO_FIX:
            if run is None:
                run = [ep, ep]
            else:
                run[1] = ep
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
        if run is not None:
            nofix.append((run[0], run[1]))
            run = None
        lat.append(la)
        lon.append(lo)
        spd.append(float(r[2]) if r[2] is not None else 0.0)
        tim.append(ep)
        gns.append(g)
    if run is not None:
        nofix.append((run[0], run[1]))

    return {
        "lat": lat,
        "lon": lon,
        "time": tim,
        "speed": spd,
        "interpolated": [0] * len(lat),
        "gnss": gns,
        "nofix": nofix,
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
        return {"lat": [], "lon": [], "time": [], "speed": [], "interpolated": [],
                "gnss": [], "nofix": []}, None
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


def speed_distance_km(times: list[int], speeds: list[float]) -> float:
    """Пробег по скорости × время между соседними точками (calculateDailyKm из revenue3.php)."""
    total = 0.0
    for i in range(1, len(times)):
        dt = times[i] - times[i - 1]
        if dt <= 0 or dt > _SPEED_MAX_GAP_SEC:
            continue
        avg_speed = (speeds[i - 1] + speeds[i]) / 2.0
        if avg_speed < _SPEED_MOVING_KMH:
            continue
        total += avg_speed * (dt / 3600.0)
    return round(total, 1)


def daily_speed_km(gps_db: Session, imei: str, days: list[date]) -> dict[date, float] | None:
    """Пробег машины за каждые календарные сутки (местное время, 00:00–23:59:59).

    Один запрос на весь диапазон дней, дальше раскладка по суткам.
    None — трекер по IMEI не найден.
    """
    if not days:
        return {}
    tracker = get_tracker(gps_db, imei)
    if tracker is None:
        return None
    tracker_id, table = tracker

    start = int(combine_local(min(days), time(0, 0)).timestamp())
    end = int(combine_local(max(days) + timedelta(days=1), time(0, 0)).timestamp()) - 1
    rows = gps_db.execute(
        text(
            f"SELECT `speed`, `epochtime` FROM `{table}` "  # noqa: S608 — имя валидировано
            f"WHERE `imei` = :tid AND `epochtime` BETWEEN :s AND :e "
            f"ORDER BY `epochtime` ASC"
        ),
        {"tid": tracker_id, "s": start, "e": end},
    ).all()

    points: dict[date, tuple[list[int], list[float]]] = {d: ([], []) for d in days}
    for raw_speed, raw_time in rows:
        try:
            speed = float(raw_speed or 0)
            ep = int(raw_time)
        except (TypeError, ValueError):
            continue
        if speed >= _SPEED_GPS_ERROR_KMH:
            continue
        bucket = points.get(unix_to_local(ep).date())
        if bucket is None:
            continue
        bucket[0].append(ep)
        bucket[1].append(speed)

    return {d: speed_distance_km(t, s) for d, (t, s) in points.items()}


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
