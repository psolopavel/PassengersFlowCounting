"""Геометрия для разбора GPS-трека и маршрутов (порт из TrackGapFiller.php).

Все координаты — (lat, lon) в градусах. Расстояния — в метрах.
Маршрут представляется как polyline: список точек [(lat, lon), ...] и
предрасчитанные длины сегментов (segment_lengths).
"""

from __future__ import annotations

import bisect
import math
import re

EARTH_RADIUS_M = 6371000.0

_LINESTRING_RE = re.compile(r"LINESTRING\s*\((.+)\)", re.IGNORECASE)
_POINT_RE = re.compile(r"POINT\s*\(([^)]+)\)", re.IGNORECASE)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние по большой окружности между двумя точками (метры)."""
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_M * c


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Азимут (0..360) движения из точки 1 в точку 2."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    theta = math.atan2(y, x)
    return math.degrees(theta) % 360.0


def parse_linestring(wkt: str | None) -> list[tuple[float, float]]:
    """WKT 'LINESTRING(lon lat, ...)' -> [(lat, lon), ...]."""
    points: list[tuple[float, float]] = []
    if not wkt or not isinstance(wkt, str):
        return points
    m = _LINESTRING_RE.search(wkt)
    if not m:
        return points
    for pair in m.group(1).split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = re.split(r"\s+", pair)
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        points.append((lat, lon))
    return points


def parse_point(wkt: str | None) -> tuple[float, float] | None:
    """WKT 'POINT(lon lat)' -> (lat, lon)."""
    if not wkt or not isinstance(wkt, str):
        return None
    m = _POINT_RE.search(wkt)
    if not m:
        return None
    parts = re.split(r"\s+", m.group(1).strip())
    if len(parts) < 2:
        return None
    try:
        lon = float(parts[0])
        lat = float(parts[1])
    except ValueError:
        return None
    return (lat, lon)


def segment_lengths(points: list[tuple[float, float]]) -> list[float]:
    """Длины сегментов полилинии (метры)."""
    lens: list[float] = []
    for i in range(1, len(points)):
        lens.append(
            haversine(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
        )
    return lens


def _project_on_segment(
    lat: float, lon: float,
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> tuple[float, float]:
    """Проекция точки на сегмент. Возвращает (distance_m, t) где t∈[0,1]."""
    lat_ref = (lat1 + lat2) / 2.0
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat_ref))

    x = (lon - lon1) * m_per_deg_lon
    y = (lat - lat1) * m_per_deg_lat
    dx = (lon2 - lon1) * m_per_deg_lon
    dy = (lat2 - lat1) * m_per_deg_lat

    len_sq = dx * dx + dy * dy
    if len_sq <= 0:
        return math.hypot(x, y), 0.0
    t = (x * dx + y * dy) / len_sq
    t = max(0.0, min(1.0, t))
    ex = x - dx * t
    ey = y - dy * t
    return math.hypot(ex, ey), t


def project_point_on_polyline(
    lat: float, lon: float,
    points: list[tuple[float, float]],
    seg_lens: list[float],
) -> dict | None:
    """Ближайшая проекция точки на полилинию.

    Возвращает {'distance', 'along', 'segment_index', 't'} или None.
    `along` — дистанция вдоль полилинии от начала до проекции (метры).
    """
    n = len(points)
    if n < 2:
        return None
    best_dist: float | None = None
    best_seg = 0
    best_t = 0.0
    for i in range(n - 1):
        dist, t = _project_on_segment(
            lat, lon,
            points[i][0], points[i][1],
            points[i + 1][0], points[i + 1][1],
        )
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_seg = i
            best_t = t
    along = sum(seg_lens[:best_seg]) + seg_lens[best_seg] * best_t
    return {
        "distance": best_dist,
        "along": along,
        "segment_index": best_seg,
        "t": best_t,
    }


def cumulative_along(seg_lens: list[float]) -> list[float]:
    """Накопленная дистанция до КАЖДОЙ вершины полилинии (метры).

    Длина результата = len(points) = len(seg_lens) + 1.
    cum[i] — путь от начала до points[i]; cum[0] = 0.
    """
    cum: list[float] = [0.0]
    acc = 0.0
    for length in seg_lens:
        acc += length
        cum.append(acc)
    return cum


def project_point_on_polyline_window(
    lat: float, lon: float,
    points: list[tuple[float, float]],
    seg_lens: list[float],
    cum_along: list[float],
    along_lo: float,
    along_hi: float,
    prefer_from: float | None = None,
    tie_m: float = 30.0,
) -> dict | None:
    """Ближайшая проекция точки на УЧАСТОК полилинии по длине дуги.

    Рассматриваются только сегменты, чей диапазон along пересекает
    [along_lo, along_hi]. Это удерживает `along` монотонным на
    самопересекающейся геометрии (спур «туда-обратно» по одной дороге):
    вместо глобально ближайшего сегмента берём ближайший В ОКНЕ вокруг
    текущего курсора.

    Если задан ``prefer_from`` (текущий курсор), то при почти равных боковых
    расстояниях (в пределах ``tie_m``) предпочитается копия дороги ВПЕРЁДИ
    курсора. Это разрешает неоднозначность в точке разворота спура: возврат по
    той же линии продолжает along вперёд (штатный проезд), а настоящий разворот
    (где копии впереди нет) честно уводит along назад → слом.

    Возвращает {'distance','along','segment_index','t'} или None, если в окне
    нет ни одного сегмента.
    """
    n = len(points)
    if n < 2:
        return None
    # cum_along отсортирован по возрастанию — сегмент i занимает
    # [cum_along[i], cum_along[i+1]]. Бинарным поиском сужаем перебор до
    # сегментов, пересекающих окно [along_lo, along_hi].
    lo_i = max(bisect.bisect_right(cum_along, along_lo) - 1, 0)
    hi_i = min(bisect.bisect_left(cum_along, along_hi), n - 1)
    cands: list[tuple[float, int, float, float]] = []
    best_dist: float | None = None
    for i in range(lo_i, hi_i):
        dist, t = _project_on_segment(
            lat, lon,
            points[i][0], points[i][1],
            points[i + 1][0], points[i + 1][1],
        )
        along_i = cum_along[i] + seg_lens[i] * t
        cands.append((dist, i, t, along_i))
        if best_dist is None or dist < best_dist:
            best_dist = dist
    if best_dist is None:
        return None

    if prefer_from is None:
        dist, seg, t, along = min(cands, key=lambda c: (c[0], c[1]))
    else:
        # Ничья по боковому расстоянию → берём копию впереди курсора (ближайшую
        # из тех, что не позади). Если впереди копий нет — ближайшую по along.
        tie = [c for c in cands if c[0] <= best_dist + tie_m]
        ahead = [c for c in tie if c[3] >= prefer_from - 1e-6]
        pool = ahead if ahead else tie
        dist, seg, t, along = min(pool, key=lambda c: abs(c[3] - prefer_from))

    return {
        "distance": dist,
        "along": along,
        "segment_index": seg,
        "t": t,
    }


def point_at_distance_along(
    points: list[tuple[float, float]],
    seg_lens: list[float],
    target_dist: float,
) -> dict | None:
    """Точка на полилинии на дистанции target_dist от начала.

    Возвращает {'lat', 'lon', 'segment_index'} или None.
    """
    if target_dist < 0:
        target_dist = 0.0
    acc = 0.0
    n = len(seg_lens)
    for i in range(n):
        nxt = acc + seg_lens[i]
        if target_dist <= nxt or i == n - 1:
            seg_len = seg_lens[i]
            if seg_len <= 0:
                return {
                    "lat": points[i + 1][0],
                    "lon": points[i + 1][1],
                    "segment_index": i,
                }
            t = (target_dist - acc) / seg_len
            t = max(0.0, min(1.0, t))
            lat = points[i][0] + (points[i + 1][0] - points[i][0]) * t
            lon = points[i][1] + (points[i + 1][1] - points[i][1]) * t
            return {"lat": lat, "lon": lon, "segment_index": i}
        acc = nxt
    return None
