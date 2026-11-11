"""Ручна хронологія міжміста (cars.count_optimization = manual_chronology).

Програма не визначає ходки за треком. Рахувальник за відео відмічає початок
ходки (час + початкова зупинка), кінець (час + кінцева зупинка) і будь-яку
кількість зупинок між ними — лише час та кількість людей у салоні після неї.

Реальні зупинки проміжних відміток програма обчислює сама: частка часу ходки
переводиться в позицію на маршруті між початковою і кінцевою зупинкою (за
довжиною лінії маршруту, без треку), відмітка прив'язується до найближчої
зупинки, не повертаючись назад.

У data сесії зберігаються:
    mode            — "manual_chronology";
    routes_catalog  — знімок прив'язаних маршрутів: [{id, name, inverse_route_id,
                        length_km, stops: [{id, name, order, km}]}], зупинки за km;
    chronology      — ввід рахувальника: [{uid, route_id, route_name, closed,
                        start_stop_id, end_stop_id, marks: [{uid, utc, count_after}]}];
    segments, route_periods — виводяться з chronology (лише завершені ходки) у
                        тому ж форматі, що й в автоматичному режимі, тому FIFO,
                        зведення, звіти та API працюють без змін.

Розрахунок дублюється в static/js/count_intercity_manual.js (resolveTrip) —
правила мають збігатися.
"""

from __future__ import annotations

import json
import math
import re
import secrets

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.services.route_match_service import _car_route_ids, _load_routes_geo, _load_stops
from app.utils.local_tz import unix_to_local

MODE_AUTO = "auto"
MODE_MANUAL = "manual_chronology"

MAX_TRIPS = 300
MAX_MARKS_PER_TRIP = 300

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
    """Прив'язані до машини активні маршрути із зупинками в порядку проходження.

    km зупинки — позиція її проекції на лінію маршруту. Маршрут без геометрії
    отримує зупинки за order_index і km = None (тоді позицією слугує номер).
    """
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
    geo = _load_routes_geo(routes_db, ids)

    plain_stops: dict[int, list[dict]] = {}
    for s in stop_rows:
        plain_stops.setdefault(int(s.route_id), []).append({
            "id": int(s.id),
            "name": s.name or f"Зупинка #{s.id}",
            "order": int(s.order_index) if s.order_index is not None else 0,
            "km": None,
        })

    by_id: dict[int, dict] = {}
    for r in route_rows:
        rid = int(r.id)
        route = {
            "id": rid,
            "name": r.name or f"Маршрут #{rid}",
            "inverse_route_id": _inverse_route_id(r.additional),
            "length_km": None,
            "stops": plain_stops.get(rid, []),
        }
        g = geo.get(rid)
        if g is not None:
            _load_stops(routes_db, g)
            if len(g["stops"]) >= 2:
                route["length_km"] = round(g["total_len"] / 1000.0, 3)
                route["stops"] = [
                    {
                        "id": s["id"],
                        "name": s["name"],
                        "order": s["order"],
                        "km": round(s["along"] / 1000.0, 3),
                    }
                    for s in g["stops"]
                ]
        by_id[rid] = route
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


CATALOG_WARNING_PREFIXES = ("Немає прив'язаних маршрутів", "Маршрут «")


def catalog_warnings(catalog: list[dict]) -> list[str]:
    """Попередження про маршрути знімка (усі починаються з CATALOG_WARNING_PREFIXES)."""
    if not catalog:
        return ["Немає прив'язаних маршрутів (all_car_routes)"]
    out = []
    for route in catalog:
        if route.get("unbound"):
            continue
        if len(route.get("stops") or []) < 2:
            out.append(f"Маршрут «{route['name']}»: менше двох зупинок — ходку по ньому не завершити")
        elif route.get("length_km") is None:
            out.append(
                f"Маршрут «{route['name']}» без лінії на карті — зупинки розподіляються "
                "рівномірно за порядком, кілометри не рахуються"
            )
    return out


def _lacks_positions(route: dict) -> bool:
    stops = route.get("stops") or []
    return not stops or any(s.get("km") is None for s in stops)


def sync_route_catalog(data: dict, fresh: list[dict]) -> dict:
    """Звіряє знімок маршрутів сесії з поточними прив'язками машини.

    Нові маршрути додаються, відв'язані без ходок прибираються, відв'язані з
    ходками лишаються з unbound=True (ходки рахуються, але завершити підрахунок
    не можна, доки маршрут ходки не змінять). Зупинки вже знятих маршрутів не
    оновлюються (стабільність розрахунку) — крім знімків без позицій зупинок.
    -> {"changed", "added": [назви], "removed": [назви], "unbound": [назви]}.
    """
    old = data.get("routes_catalog") or []
    old_by_id = {r["id"]: r for r in old}
    fresh_ids = {r["id"] for r in fresh}
    used = {t.get("route_id") for t in data.get("chronology") or []}

    merged: list[dict] = []
    added: list[str] = []
    for r in fresh:
        prev = old_by_id.get(r["id"])
        if prev is None:
            added.append(r["name"])
            merged.append(r)
        elif _lacks_positions(prev) and not _lacks_positions(r):
            merged.append(r)
        else:
            kept = {k: v for k, v in prev.items() if k != "unbound"}
            kept["name"] = r["name"]
            kept["inverse_route_id"] = r["inverse_route_id"]
            merged.append(kept)
    removed: list[str] = []
    unbound: list[str] = []
    for r in old:
        if r["id"] in fresh_ids:
            continue
        if r["id"] in used:
            unbound.append(r["name"])
            merged.append({**r, "unbound": True, "inverse_route_id": None})
        else:
            removed.append(r["name"])

    changed = json.dumps(merged, sort_keys=True) != json.dumps(old, sort_keys=True)
    if changed:
        data["routes_catalog"] = merged
        data["warnings"] = [
            w for w in data.get("warnings") or []
            if not str(w).startswith(CATALOG_WARNING_PREFIXES)
        ] + catalog_warnings(merged)
    return {"changed": changed, "added": added, "removed": removed, "unbound": unbound}


def route_stop_positions(route: dict | None) -> list[dict]:
    """Зупинки маршруту з позицією pos (км; без геометрії — номер), за pos."""
    stops = list((route or {}).get("stops") or [])
    if not stops:
        return []
    if any(s.get("km") is None for s in stops):
        return [{**s, "pos": float(i)} for i, s in enumerate(stops)]
    out = [{**s, "pos": float(s["km"])} for s in stops]
    out.sort(key=lambda s: (s["pos"], s.get("order", 0), s["id"]))
    return out


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
    """Приводить хронологію з браузера до канонічного вигляду; биті записи відкидає.

    Старий формат (stop_id у кожної відмітки) читається так: початкова зупинка —
    з першої відмітки, кінцева — з останньої завершеної ходки, решта обчислюється.
    """
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
        raw_marks = [m for m in (t.get("marks") or [])[:MAX_MARKS_PER_TRIP] if isinstance(m, dict)]
        marks: list[dict] = []
        legacy_stops: list[int | None] = []
        for m in raw_marks:
            utc = _int(m.get("utc"))
            if utc is None:
                continue
            marks.append({
                "uid": _uid(m.get("uid"), seen),
                "utc": utc,
                "count_after": parse_count(m.get("count_after")),
            })
            legacy_stops.append(_int(m.get("stop_id")))
        closed = bool(t.get("closed"))
        start_stop_id = _int(t.get("start_stop_id"))
        if start_stop_id is None and legacy_stops:
            start_stop_id = legacy_stops[0]
        end_stop_id = _int(t.get("end_stop_id")) if closed else None
        if closed and end_stop_id is None and len(legacy_stops) >= 2:
            end_stop_id = legacy_stops[-1]
        trips.append({
            "uid": _uid(t.get("uid"), seen),
            "route_id": route_id,
            "route_name": str(t.get("route_name") or "")[:255],
            "closed": closed,
            "start_stop_id": start_stop_id,
            "end_stop_id": end_stop_id,
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


# ── розподіл відміток по зупинках ────────────────────────────────────

def resolve_trip(trip: dict, route: dict | None) -> list[dict] | None:
    """Зупинка кожної відмітки завершеної ходки: [{stop, pos, estimated}] або None.

    Перша і остання відмітки — вказані рахувальником початкова і кінцева зупинки.
    Проміжна: pos = pos_початку + частка_часу × (pos_кінця − pos_початку), далі
    найближча зупинка між початковою і кінцевою, не раніше попередньої відмітки
    (на рівній відстані — раніша).
    """
    marks = trip.get("marks") or []
    if not trip.get("closed") or len(marks) < 2:
        return None
    stops = route_stop_positions(route)
    idx_a = next((i for i, s in enumerate(stops) if s["id"] == trip.get("start_stop_id")), None)
    idx_b = next((i for i, s in enumerate(stops) if s["id"] == trip.get("end_stop_id")), None)
    if idx_a is None or idx_b is None or stops[idx_b]["pos"] <= stops[idx_a]["pos"]:
        return None
    pos_a = stops[idx_a]["pos"]
    pos_b = stops[idx_b]["pos"]
    t0 = marks[0]["utc"]
    t1 = marks[-1]["utc"]
    if t1 <= t0:
        return None

    out = [{"stop": stops[idx_a], "pos": pos_a, "estimated": False}]
    lo = idx_a
    for mark in marks[1:-1]:
        frac = min(1.0, max(0.0, (mark["utc"] - t0) / (t1 - t0)))
        pos = pos_a + frac * (pos_b - pos_a)
        best = lo
        for j in range(lo + 1, idx_b + 1):
            if abs(stops[j]["pos"] - pos) < abs(stops[best]["pos"] - pos):
                best = j
        lo = best
        out.append({"stop": stops[best], "pos": pos, "estimated": True})
    out.append({"stop": stops[idx_b], "pos": pos_b, "estimated": False})
    return out


def _trip_start(trip: dict) -> int:
    marks = trip.get("marks") or []
    return marks[0]["utc"] if marks else 2**62


def _routes_by_id(data: dict) -> dict[int, dict]:
    return {r["id"]: r for r in data.get("routes_catalog") or []}


def _distance_scale(route: dict | None) -> float:
    """Множник pos → км: 1 для маршрутів із геометрією, 0 — без неї (км невідомі)."""
    stops = (route or {}).get("stops") or []
    return 0.0 if not stops or any(s.get("km") is None for s in stops) else 1.0


def apply_chronology(data: dict, chronology: list[dict]) -> dict:
    """Записує chronology в data і перебудовує segments / route_periods."""
    routes = _routes_by_id(data)
    for trip in chronology:
        route = routes.get(trip["route_id"])
        if route is not None:
            trip["route_name"] = route["name"]

    occurrences: dict[int, int] = {}
    segments: list[dict] = []
    periods: list[dict] = []
    for trip in sorted(chronology, key=_trip_start):
        rid = trip["route_id"]
        route = routes.get(rid)
        resolved = resolve_trip(trip, route)
        if resolved is None:
            continue
        marks = trip["marks"]
        occurrence = occurrences.get(rid, 0) + 1
        occurrences[rid] = occurrence
        route_name = trip["route_name"] or f"Маршрут {rid}"
        scale = _distance_scale(route)
        periods.append({
            "route_id": rid,
            "route_name": route_name,
            "start_utc": marks[0]["utc"],
            "end_utc": marks[-1]["utc"],
            "occurrence": occurrence,
            "trip_uid": trip["uid"],
        })

        def stop_info(mark: dict, res: dict) -> dict:
            return {
                "id": res["stop"]["id"],
                "name": res["stop"]["name"],
                "order": res["stop"].get("order", 0),
                "arrival_utc": mark["utc"],
                "departure_utc": mark["utc"],
                "estimated": res["estimated"],
            }

        for i in range(len(marks) - 1):
            a, b = marks[i], marks[i + 1]
            ra, rb = resolved[i], resolved[i + 1]
            segments.append({
                "seg_key": f"m:{a['uid']}:{b['uid']}",
                "route_id": rid,
                "route_name": route_name,
                "occurrence": occurrence,
                "from_stop": stop_info(a, ra),
                "to_stop": stop_info(b, rb),
                "period_start_utc": a["utc"],
                "period_end_utc": b["utc"],
                "count": a["count_after"],
                "distance_km": round(max(0.0, (rb["pos"] - ra["pos"]) * scale), 2),
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
    routes = _routes_by_id(data)
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
        route = routes.get(trip["route_id"])
        if route is None or route.get("unbound"):
            err(f"Маршрут «{name}» більше не прив'язаний до машини — змініть маршрут ходки "
                "або видаліть її.", trip, marks[0] if marks else None)
            continue
        if len(marks) < 2:
            err(f"Ходка «{name}»: потрібні щонайменше початок і кінець.", trip)
            continue
        if not trip.get("closed"):
            err(f"Ходка «{name}» не завершена.", trip, marks[-1])
            continue
        stops = {s["id"]: s for s in route_stop_positions(route)}
        start = stops.get(trip.get("start_stop_id"))
        end = stops.get(trip.get("end_stop_id"))
        if start is None:
            err(f"Ходка «{name}»: вкажіть початкову зупинку маршруту.", trip, marks[0])
        if end is None:
            err(f"Ходка «{name}»: вкажіть кінцеву зупинку маршруту.", trip, marks[-1])
        if start is not None and end is not None and end["pos"] <= start["pos"]:
            err(f"Ходка «{name}»: кінцева зупинка «{end['name']}» має бути далі за маршрутом, "
                f"ніж початкова «{start['name']}».", trip, marks[-1])
        for i, mark in enumerate(marks):
            if i > 0 and mark["utc"] <= marks[i - 1]["utc"]:
                err(f"Ходка «{name}»: відмітка о {_hms(mark['utc'])} має бути пізніше "
                    "попередньої.", trip, mark)
            if i < len(marks) - 1:
                count = mark.get("count_after")
                if count is None:
                    err(f"Ходка «{name}»: вкажіть кількість людей після відмітки "
                        f"о {_hms(mark['utc'])}.", trip, mark)
                elif count > max_people:
                    err(f"Ходка «{name}»: після відмітки о {_hms(mark['utc'])} більше людей, "
                        f"ніж максимум ({max_people}).", trip, mark)

    ordered = sorted((t for t in chronology if t.get("marks")), key=_trip_start)
    for prev, trip in zip(ordered, ordered[1:]):
        if trip["marks"][0]["utc"] < prev["marks"][-1]["utc"]:
            name = trip.get("route_name") or f"Маршрут {trip['route_id']}"
            err(f"Ходка «{name}» починається раніше, ніж закінчилась попередня.", trip)
    return errors


def _hms(utc: int) -> str:
    return unix_to_local(int(utc)).strftime("%H:%M:%S")
