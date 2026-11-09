"""Ручна хронологія міжміста (cars.count_optimization = manual_chronology).

Програма не визначає ходки за треком. Рахувальник за відео будує хронологію:
ходка = маршрут + впорядковані відмітки зупинок (час із плеєра) + кількість
людей у салоні після кожної зупинки, крім останньої.

У data сесії зберігаються:
    mode            — "manual_chronology";
    routes_catalog  — знімок прив'язаних маршрутів із зупинками (для вибору та перевірок);
    chronology      — [{uid, route_id, route_name, closed, marks: [{uid, stop_id,
                        stop_name, utc, count_after}]}];
    track.cum_km    — прорежений ряд накопиченого пробігу [[utc, km], ...];
    segments, route_periods — виводяться з chronology у тому ж форматі, що й
                        в автоматичному режимі, тому FIFO, зведення, звіти та API
                        працюють без змін.
"""

from __future__ import annotations

import json
import math
import re
import secrets
from bisect import bisect_left

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.services.route_match_service import _car_route_ids
from app.utils.geo import haversine

MODE_AUTO = "auto"
MODE_MANUAL = "manual_chronology"

MAX_TRIPS = 300
MAX_MARKS_PER_TRIP = 300
CUM_KM_MAX_POINTS = 1500

_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def is_manual(data: dict | None) -> bool:
    return bool(data) and data.get("mode") == MODE_MANUAL


# ── каталог маршрутів ────────────────────────────────────────────────

def _inverse_route_id(raw) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        extra = json.loads(raw)
        inv = int(float(extra.get("inverse_route_id")))
    except (ValueError, TypeError, AttributeError):
        return None
    return inv if inv > 0 else None


def load_route_catalog(routes_db: Session, imei: str) -> list[dict]:
    """Прив'язані до машини активні маршрути із зупинками в порядку order_index."""
    ids = _car_route_ids(routes_db, imei)
    if not ids:
        return []
    route_rows = routes_db.execute(
        text(
            "SELECT id, name, additional FROM routes WHERE id IN :ids AND active = 1"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    stop_rows = routes_db.execute(
        text(
            "SELECT id, route_id, name, order_index FROM stops "
            "WHERE route_id IN :ids AND active = 1 ORDER BY route_id, order_index, id"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()

    stops_by_route: dict[int, list[dict]] = {}
    for s in stop_rows:
        stops_by_route.setdefault(int(s.route_id), []).append({
            "id": int(s.id),
            "name": s.name or f"Зупинка #{s.id}",
            "order": int(s.order_index) if s.order_index is not None else 0,
        })

    by_id = {
        int(r.id): {
            "id": int(r.id),
            "name": r.name or f"Маршрут #{r.id}",
            "inverse_route_id": _inverse_route_id(r.additional),
            "stops": stops_by_route.get(int(r.id), []),
        }
        for r in route_rows
    }
    catalog = [by_id[rid] for rid in ids if rid in by_id]

    for route in catalog:
        inv = route["inverse_route_id"]
        if inv is not None and (inv == route["id"] or inv not in by_id):
            route["inverse_route_id"] = None
    for route in catalog:
        inv = route["inverse_route_id"]
        if inv is not None and by_id[inv]["inverse_route_id"] is None:
            by_id[inv]["inverse_route_id"] = route["id"]
    return catalog


# ── пробіг ───────────────────────────────────────────────────────────

def cumulative_km_series(track: dict) -> list[list]:
    """[[utc, km_від_початку_дня], ...], прорежений до CUM_KM_MAX_POINTS точок."""
    lats = track.get("lat", [])
    lons = track.get("lon", [])
    times = track.get("time", [])
    n = len(lats)
    if n < 2 or len(lons) != n or len(times) != n:
        return []
    step = max(1, math.ceil(n / CUM_KM_MAX_POINTS))
    total_m = 0.0
    series = [[int(times[0]), 0.0]]
    for i in range(1, n):
        total_m += haversine(lats[i - 1], lons[i - 1], lats[i], lons[i])
        if i % step == 0 or i == n - 1:
            series.append([int(times[i]), round(total_m / 1000.0, 3)])
    return series


def _km_at(series: list[list], times: list[int], utc: int) -> float:
    if not series:
        return 0.0
    i = bisect_left(times, utc)
    if i <= 0:
        return float(series[0][1])
    if i >= len(series):
        return float(series[-1][1])
    t0, k0 = series[i - 1]
    t1, k1 = series[i]
    if t1 <= t0:
        return float(k1)
    return float(k0) + (float(k1) - float(k0)) * (utc - t0) / (t1 - t0)


# ── очищення вхідних даних ───────────────────────────────────────────

def _int(raw) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_count(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(n) or n < 0:
        return None
    return int(n) if n == int(n) else n


def _uid(raw, seen: set[str]) -> str:
    uid = str(raw or "")
    if not _UID_RE.match(uid) or uid in seen:
        uid = secrets.token_hex(6)
        while uid in seen:
            uid = secrets.token_hex(6)
    seen.add(uid)
    return uid


def sanitize_chronology(raw) -> list[dict]:
    """Приводить хронологію з браузера до канонічного вигляду; биті записи відкидає."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    trips: list[dict] = []
    for t in raw[:MAX_TRIPS]:
        if not isinstance(t, dict):
            continue
        route_id = _int(t.get("route_id"))
        if route_id is None:
            continue
        marks: list[dict] = []
        for m in (t.get("marks") or [])[:MAX_MARKS_PER_TRIP]:
            if not isinstance(m, dict):
                continue
            stop_id = _int(m.get("stop_id"))
            utc = _int(m.get("utc"))
            if stop_id is None or utc is None:
                continue
            marks.append({
                "uid": _uid(m.get("uid"), seen),
                "stop_id": stop_id,
                "stop_name": str(m.get("stop_name") or "")[:255],
                "utc": utc,
                "count_after": parse_count(m.get("count_after")),
            })
        trips.append({
            "uid": _uid(t.get("uid"), seen),
            "route_id": route_id,
            "route_name": str(t.get("route_name") or "")[:255],
            "closed": bool(t.get("closed")),
            "marks": marks,
        })
    return trips


def chronology_exceeds_max(chronology, max_people: int) -> bool:
    for trip in sanitize_chronology(chronology):
        for mark in trip["marks"]:
            c = mark["count_after"]
            if c is not None and c > max_people:
                return True
    return False


# ── chronology -> segments ───────────────────────────────────────────

def _trip_start(trip: dict) -> int:
    marks = trip.get("marks") or []
    return marks[0]["utc"] if marks else 2**62


def _catalog_maps(data: dict) -> tuple[dict, dict]:
    routes = {r["id"]: r for r in data.get("routes_catalog") or []}
    stops = {
        rid: {s["id"]: s for s in r.get("stops") or []}
        for rid, r in routes.items()
    }
    return routes, stops


def apply_chronology(data: dict, chronology: list[dict]) -> dict:
    """Записує chronology в data і перебудовує segments / route_periods."""
    routes, stops = _catalog_maps(data)
    series = (data.get("track") or {}).get("cum_km") or []
    series_times = [int(p[0]) for p in series]

    for trip in chronology:
        route = routes.get(trip["route_id"])
        if route is not None:
            trip["route_name"] = route["name"]
        route_stops = stops.get(trip["route_id"], {})
        for mark in trip["marks"]:
            stop = route_stops.get(mark["stop_id"])
            if stop is not None:
                mark["stop_name"] = stop["name"]

    occurrences: dict[int, int] = {}
    segments: list[dict] = []
    periods: list[dict] = []
    for trip in sorted(chronology, key=_trip_start):
        marks = trip["marks"]
        if len(marks) < 2:
            continue
        rid = trip["route_id"]
        occurrence = occurrences.get(rid, 0) + 1
        occurrences[rid] = occurrence
        route_name = trip["route_name"] or f"Маршрут {rid}"
        route_stops = stops.get(rid, {})
        periods.append({
            "route_id": rid,
            "route_name": route_name,
            "start_utc": marks[0]["utc"],
            "end_utc": marks[-1]["utc"],
            "occurrence": occurrence,
            "trip_uid": trip["uid"],
        })

        def stop_info(mark: dict) -> dict:
            stop = route_stops.get(mark["stop_id"]) or {}
            return {
                "id": mark["stop_id"],
                "name": mark["stop_name"] or stop.get("name") or f"Зупинка #{mark['stop_id']}",
                "order": stop.get("order", 0),
                "arrival_utc": mark["utc"],
                "departure_utc": mark["utc"],
            }

        for a, b in zip(marks, marks[1:]):
            km = _km_at(series, series_times, b["utc"]) - _km_at(series, series_times, a["utc"])
            segments.append({
                "seg_key": f"m:{a['uid']}:{b['uid']}",
                "route_id": rid,
                "route_name": route_name,
                "occurrence": occurrence,
                "from_stop": stop_info(a),
                "to_stop": stop_info(b),
                "period_start_utc": a["utc"],
                "period_end_utc": b["utc"],
                "count": a["count_after"],
                "distance_km": round(max(0.0, km), 2),
                "trip_uid": trip["uid"],
            })

    segments.sort(key=lambda s: s["period_start_utc"])
    data["chronology"] = chronology
    data["segments"] = segments
    data["route_periods"] = periods
    return data


# ── перевірка перед завершенням ──────────────────────────────────────

def validate_for_ready(data: dict | None, max_people: int) -> list[dict]:
    """Помилки, що не дають завершити підрахунок: [{message, trip_uid?, mark_uid?}]."""
    data = data or {}
    chronology = data.get("chronology") or []
    routes, stops = _catalog_maps(data)
    errors: list[dict] = []

    def err(message: str, trip: dict | None = None, mark: dict | None = None) -> None:
        errors.append({
            "message": message,
            "trip_uid": trip["uid"] if trip else None,
            "mark_uid": mark["uid"] if mark else None,
        })

    if not chronology:
        err("Додайте хоча б одну ходку.")
        return errors

    for trip in chronology:
        name = trip.get("route_name") or f"Маршрут {trip['route_id']}"
        marks = trip.get("marks") or []
        if trip["route_id"] not in routes:
            err(f"Маршрут «{name}» більше не прив'язаний до машини — видаліть ходку "
                "або оновіть маршрути.", trip)
            continue
        if len(marks) < 2:
            err(f"Ходка «{name}»: потрібно щонайменше дві зупинки (початок і кінець).", trip)
            continue
        if not trip.get("closed"):
            err(f"Ходка «{name}» не завершена.", trip)
        route_stops = stops.get(trip["route_id"], {})
        for i, mark in enumerate(marks):
            stop_name = mark.get("stop_name") or f"#{mark['stop_id']}"
            if mark["stop_id"] not in route_stops:
                err(f"Ходка «{name}»: зупинка «{stop_name}» не належить маршруту.", trip, mark)
            if i > 0:
                prev = marks[i - 1]
                if mark["utc"] <= prev["utc"]:
                    err(f"Ходка «{name}»: час зупинки «{stop_name}» має бути пізніше "
                        "попередньої.", trip, mark)
                if mark["stop_id"] == prev["stop_id"]:
                    err(f"Ходка «{name}»: зупинка «{stop_name}» відмічена двічі поспіль.",
                        trip, mark)
            if i < len(marks) - 1:
                count = mark.get("count_after")
                if count is None:
                    err(f"Ходка «{name}»: вкажіть кількість людей після зупинки "
                        f"«{stop_name}».", trip, mark)
                elif count > max_people:
                    err(f"Ходка «{name}»: після зупинки «{stop_name}» більше людей, "
                        f"ніж максимум ({max_people}).", trip, mark)

    ordered = sorted((t for t in chronology if t.get("marks")), key=_trip_start)
    for prev, trip in zip(ordered, ordered[1:]):
        if trip["marks"][0]["utc"] < prev["marks"][-1]["utc"]:
            name = trip.get("route_name") or f"Маршрут {trip['route_id']}"
            err(f"Ходка «{name}» починається раніше, ніж закінчилась попередня.", trip)
    return errors
