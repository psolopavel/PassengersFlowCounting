"""Автозаполнение разрывов GPS-трека по маршрутам — порт TrackGapFiller.php.

Идея (кратко):
  1. В треке ищем разрывы: пара соседних точек с расстоянием > MIN_GAP_METERS.
     Разрыв расширяется в прошлое по «зависшим» дублям координаты перед прыжком.
  2. Для каждой реальной точки считаем, на каких маршрутах она лежит (коридор)
     и её положение `along` вдоль полилинии.
  3. По разрывам идём по времени: для каждого ищем маршрут, покрывающий обе
     границы с движением вперёд (along_after > along_before). Однозначный —
     применяем; неоднозначный — откладываем и решаем инерцией соседних.
  4. Разрыв заполняется интерполяцией по куску маршрута (along_before..along_after).
  5. Если границы разрыва не покрыты коридором либо кандидатов в коридоре нет
     вовсе (например, покрывающие маршруты идут назад), включается approach:
     фактические A и B проецируются на маршруты уже без коридора, а путь
     собирается как [прямая A→вход] + [кусок маршрута] + [прямая выход→B].
  6. Если у маршрутов в routes.additional заданы inverse_route_id и/или
     avg_route_pass_time, сначала пробуется план по контексту проездов до и
     после разрыва (gap_context_service, режим 2 TrackGapFiller.php): обороты
     через обратный маршрут, время по средней скорости маршрута, лишнее
     время — стоянка. Шаги 2–5 работают для разрывов, которые он не решил.

Работает с треком вида {'lat','lon','time','speed','interpolated'} (списки).
`routes` — список подготовленных маршрутов:
    {'id', 'points': [(lat,lon)...], 'segment_lengths': [...], 'total_len': float}
Возвращает (filled_track, diagnostics). Интерполированные точки помечаются
interpolated=1.
"""

from __future__ import annotations

import math

from app.services import gap_context_service
from app.utils.geo import (
    bearing,
    haversine,
    point_at_distance_along,
    project_point_on_polyline,
)

MIN_GAP_SECONDS = 60
MIN_GAP_METERS = 150
ROUTE_CORRIDOR_METERS = 500
INTERPOLATION_STEP_SECONDS = 20
ASSUMED_SPEED_KMH = 30
MIN_REAL_SPEED_KMH = 5

# Максимальная длина «подводящего плеча» — прямой от точки вне маршрута до
# входа на маршрут. Если A или B дальше, approach-методом разрыв не чиним.
MAX_APPROACH_METERS = 10000
# Если approach требует скорости выше этого порога (км/ч) — не чиним:
# лучше оставить разрыв, чем нарисовать телепорт.
MAX_FILL_SPEED_KMH = 150

_COORD_EPS = 1e-6


def _same_coord(lat1, lon1, lat2, lon2) -> bool:
    return abs(lat1 - lat2) < _COORD_EPS and abs(lon1 - lon2) < _COORD_EPS


# ── детект разрывов ─────────────────────────────────────────────────

def _first_stationary_index(track: dict, index_before: int) -> int:
    lat_ref = track["lat"][index_before]
    lon_ref = track["lon"][index_before]
    first = index_before
    for j in range(index_before - 1, -1, -1):
        if not _same_coord(track["lat"][j], track["lon"][j], lat_ref, lon_ref):
            break
        first = j
    return first


def _make_gap(track: dict, start_idx: int, end_idx: int, dd: float | None = None) -> dict:
    lat_b, lon_b = track["lat"][start_idx], track["lon"][start_idx]
    lat_a, lon_a = track["lat"][end_idx], track["lon"][end_idx]
    if dd is None:
        dd = haversine(lat_b, lon_b, lat_a, lon_a)
    time_b = int(track["time"][start_idx])
    time_a = int(track["time"][end_idx])
    return {
        "start_index": start_idx,
        "end_index": end_idx,
        "time_before": time_b,
        "time_after": time_a,
        "lat_before": lat_b,
        "lon_before": lon_b,
        "lat_after": lat_a,
        "lon_after": lon_a,
        "dt": time_a - time_b,
        "dd": dd,
        "remove_indices": list(range(start_idx + 1, end_idx)),
    }


def _detect_gaps(track: dict) -> list[dict]:
    lat, lon = track["lat"], track["lon"]
    n = len(lat)
    gaps: list[dict] = []
    if n < 2:
        return gaps
    i = 1
    while i < n:
        prev = i - 1
        dd = haversine(lat[prev], lon[prev], lat[i], lon[i])
        if dd <= MIN_GAP_METERS:
            i += 1
            continue
        start_idx = _first_stationary_index(track, prev)
        end_idx = i
        gaps.append(_make_gap(track, start_idx, end_idx, dd))
        i = end_idx + 1
    return gaps


# ── проекция точек на маршруты ──────────────────────────────────────

def _precompute_point_on_routes(track: dict, routes: list[dict]) -> list[list[dict]]:
    n = len(track["lat"])
    point_routes: list[list[dict]] = [[] for _ in range(n)]
    interp = track.get("interpolated") or [0] * n
    for i in range(n):
        if interp[i] == 1:
            continue
        lat, lon = track["lat"][i], track["lon"][i]
        for r_idx, route in enumerate(routes):
            proj = project_point_on_polyline(
                lat, lon, route["points"], route["segment_lengths"]
            )
            if proj is None or proj["distance"] > ROUTE_CORRIDOR_METERS:
                continue
            point_routes[i].append({
                "route_idx": r_idx,
                "route_id": route["id"],
                "along": proj["along"],
                "distance": proj["distance"],
            })
    return point_routes


# ── stateful-разрешение разрывов ────────────────────────────────────

def _find_covered_anchor(track, point_routes, start_idx, step) -> int | None:
    n = len(track["lat"])
    interp = track.get("interpolated") or [0] * n
    i = start_idx
    while 0 <= i < n:
        if interp[i] != 1 and point_routes[i]:
            return i
        i += step
    return None


def _build_candidates(before_on, after_on, routes) -> list[dict]:
    after_by_idx = {a["route_idx"]: a for a in after_on}
    candidates = []
    for b in before_on:
        a = after_by_idx.get(b["route_idx"])
        if a is None or a["along"] <= b["along"]:
            continue
        candidates.append({
            "type": "single",
            "route": routes[b["route_idx"]],
            "route_idx": b["route_idx"],
            "along_before": b["along"],
            "along_after": a["along"],
        })
    return candidates


def _pick_by_state(candidates, state):
    if state["route_idx"] is None:
        return None
    for c in candidates:
        if c["route_idx"] == state["route_idx"]:
            return c
    return None


def _resolve_pending(pending, resolutions, picked):
    picked_start = picked["route_idx"]
    for p in pending:
        if resolutions[p["gap_idx"]] is not None:
            continue
        for c in p["candidates"]:
            if c["route_idx"] == picked_start:
                resolutions[p["gap_idx"]] = c
                break


def _resolve_gaps(track, gaps, routes, point_routes) -> list[dict | None]:
    resolutions: list[dict | None] = [None] * len(gaps)
    pending: list[dict] = []
    state = {"route_idx": None}

    for gap_idx, gap in enumerate(gaps):
        before_idx = _find_covered_anchor(track, point_routes, gap["start_index"], -1)
        after_idx = _find_covered_anchor(track, point_routes, gap["end_index"], +1)
        if before_idx is None or after_idx is None:
            continue

        candidates = _build_candidates(
            point_routes[before_idx], point_routes[after_idx], routes
        )
        if not candidates:
            continue
        if len(candidates) == 1:
            resolutions[gap_idx] = candidates[0]
            _resolve_pending(pending, resolutions, candidates[0])
            state = {"route_idx": candidates[0]["route_idx"]}
            continue

        picked = _pick_by_state(candidates, state)
        if picked is not None:
            resolutions[gap_idx] = picked
            _resolve_pending(pending, resolutions, picked)
            state = {"route_idx": picked["route_idx"]}
            continue

        pending.append({"gap_idx": gap_idx, "candidates": candidates})

    for p in pending:
        if resolutions[p["gap_idx"]] is None:
            resolutions[p["gap_idx"]] = p["candidates"][0]
    return resolutions


# ── approach: подтягивание точек вне маршрута ───────────────────────

def _resolve_gap_with_approach(gap: dict, routes: list[dict]) -> dict | None:
    """Резолвер для разрывов, которые коридорный проход не закрыл корректно.

    Покрывает случаи, когда A и/или B не лежат в коридоре, а также когда обе
    точки в коридоре, но все покрывающие маршруты идут назад. Проецирует
    фактические координаты границ на все маршруты уже без ограничения коридора
    и оставляет те, где движение вперёд (along_B > along_A) и оба плеча
    не длиннее MAX_APPROACH_METERS. Выбирается минимальная сумма плеч.
    """
    lat_a, lon_a = gap["lat_before"], gap["lon_before"]
    lat_b, lon_b = gap["lat_after"], gap["lon_after"]

    best: dict | None = None
    for r_idx, route in enumerate(routes):
        proj_a = project_point_on_polyline(
            lat_a, lon_a, route["points"], route["segment_lengths"]
        )
        proj_b = project_point_on_polyline(
            lat_b, lon_b, route["points"], route["segment_lengths"]
        )
        if proj_a is None or proj_b is None:
            continue
        if proj_b["along"] <= proj_a["along"]:
            continue
        if (proj_a["distance"] > MAX_APPROACH_METERS
                or proj_b["distance"] > MAX_APPROACH_METERS):
            continue
        score = proj_a["distance"] + proj_b["distance"]
        if best is None or score < best["score"]:
            best = {
                "score": score,
                "route": route,
                "route_idx": r_idx,
                "along_before": proj_a["along"],
                "along_after": proj_b["along"],
                "dist_a": proj_a["distance"],
                "dist_b": proj_b["distance"],
            }

    if best is None:
        return None

    return {
        "type": "approach",
        "route": best["route"],
        "route_idx": best["route_idx"],
        "along_before": best["along_before"],
        "along_after": best["along_after"],
        "approach_from": {"lat": lat_a, "lon": lon_a},
        "approach_to": {"lat": lat_b, "lon": lon_b},
        "dist_a": best["dist_a"],
        "dist_b": best["dist_b"],
    }


# ── интерполяция ────────────────────────────────────────────────────

def _build_resolution_segments(res: dict) -> list[dict]:
    """Геометрия резолюции как цепочка сегментов.

    Обычная резолюция — один кусок маршрута. Approach — прямая A→вход,
    кусок маршрута и прямая выход→B.
    """
    route = res["route"]
    route_leg = {
        "type": "route",
        "route": route,
        "along_from": res["along_before"],
        "along_to": res["along_after"],
    }
    if res.get("type") != "approach":
        return [route_leg]

    entry = point_at_distance_along(
        route["points"], route["segment_lengths"], res["along_before"]
    )
    exit_pt = point_at_distance_along(
        route["points"], route["segment_lengths"], res["along_after"]
    )

    segments: list[dict] = []
    if entry is not None and res.get("approach_from") is not None:
        segments.append({
            "type": "line",
            "from": (res["approach_from"]["lat"], res["approach_from"]["lon"]),
            "to": (entry["lat"], entry["lon"]),
        })
    segments.append(route_leg)
    if exit_pt is not None and res.get("approach_to") is not None:
        segments.append({
            "type": "line",
            "from": (exit_pt["lat"], exit_pt["lon"]),
            "to": (res["approach_to"]["lat"], res["approach_to"]["lon"]),
        })
    return segments


def _segment_length(seg: dict) -> float:
    if seg["type"] == "line":
        return haversine(seg["from"][0], seg["from"][1], seg["to"][0], seg["to"][1])
    d = seg["along_to"] - seg["along_from"]
    return d if d > 0 else 0.0


def _route_heading(route: dict, segment_index: int) -> int:
    points = route["points"]
    if segment_index + 1 >= len(points):
        return 0
    return int(round(bearing(
        points[segment_index][0], points[segment_index][1],
        points[segment_index + 1][0], points[segment_index + 1][1],
    )))


def _locate_along_segments(segments: list[dict], target: float) -> dict | None:
    """Точка на цепочке сегментов по дистанции от её начала."""
    acc = 0.0
    last = len(segments) - 1
    for idx, seg in enumerate(segments):
        seg_len = _segment_length(seg)
        if target <= acc + seg_len or idx == last:
            local = min(max(target - acc, 0.0), seg_len)

            if seg["type"] == "line":
                t = local / seg_len if seg_len > 0 else 0.0
                return {
                    "lat": seg["from"][0] + (seg["to"][0] - seg["from"][0]) * t,
                    "lon": seg["from"][1] + (seg["to"][1] - seg["from"][1]) * t,
                    "direction": int(round(bearing(
                        seg["from"][0], seg["from"][1],
                        seg["to"][0], seg["to"][1],
                    ))),
                }

            route = seg["route"]
            local_target = min(seg["along_from"] + local, seg["along_to"])
            pt = point_at_distance_along(
                route["points"], route["segment_lengths"], local_target
            )
            if pt is None:
                return None
            return {
                "lat": pt["lat"],
                "lon": pt["lon"],
                "direction": _route_heading(route, pt["segment_index"]),
            }
        acc += seg_len
    return None

def _interpolate_resolution(gap: dict, res: dict) -> list[dict]:
    dt = int(gap["dt"])
    if dt < MIN_GAP_SECONDS:
        return []

    segments = _build_resolution_segments(res)
    total_len = sum(_segment_length(s) for s in segments)
    if total_len <= 0:
        return []

    raw_speed_kmh = (total_len / max(dt, 1)) * 3.6
    time_before = int(gap["time_before"])
    time_after = int(gap["time_after"])
    time_window = time_after - time_before
    if time_window <= 0:
        time_window = dt

    if raw_speed_kmh < MIN_REAL_SPEED_KMH:
        speed_kmh = ASSUMED_SPEED_KMH
        travel_time = int(round(total_len / (ASSUMED_SPEED_KMH / 3.6)))
        travel_time = max(1, min(travel_time, time_window))
        travel_start = time_after - travel_time
    else:
        speed_kmh = int(round(raw_speed_kmh))
        travel_time = time_window
        travel_start = time_before

    # Предохранитель от телепортов: approach с нереально высокой скоростью
    # (длинное плечо на коротком окне) не применяем.
    if res.get("type") == "approach" and speed_kmh > MAX_FILL_SPEED_KMH:
        return []

    meters_per_step = max(1.0, (speed_kmh / 3.6) * INTERPOLATION_STEP_SECONDS)
    count = int(round(total_len / meters_per_step)) - 1
    if count < 1:
        return []

    points: list[dict] = []
    for k in range(1, count + 1):
        fraction = k / (count + 1)
        pt = _locate_along_segments(segments, total_len * fraction)
        if pt is None:
            continue
        points.append({
            "lat": pt["lat"],
            "lon": pt["lon"],
            "speed": speed_kmh,
            "time": travel_start + int(round(travel_time * fraction)),
            "direction": pt["direction"],
        })
    return points


def _half_up(x: float) -> int:
    """Округление как floor(x + 0.5) в TrackGapFiller.php (round() в Python — банковское)."""
    return int(math.floor(x + 0.5))


def _interpolate_plan(gap: dict, plan: dict) -> list[dict]:
    """Точки для плана gap_context_service: цепочка участков (подъезд прямой,
    куски маршрутов, подъезд прямой), каждый со своей скоростью.

    Стоянка (idle_s) — в начале разрыва, без точек: машина стояла там, где
    «замёрзла» координата, затем поехала и приехала в конец разрыва. Если
    вариант чуть длиннее разрыва, все участки ускоряются в speed_factor раз.
    Точки ставятся раз в ~INTERPOLATION_STEP_SECONDS секунд движения.
    """
    dt = int(gap["dt"])
    if dt < MIN_GAP_SECONDS:
        return []
    legs = plan["legs"]
    factor = plan["speed_factor"] if plan["speed_factor"] > 0 else 1.0

    durations = [t / factor for t in plan["leg_times"]]
    lengths = [_segment_length(leg) for leg in legs]
    move_total = min(sum(durations), float(dt))
    if move_total <= 0:
        return []

    travel_start = int(gap["time_after"]) - move_total
    count = _half_up(move_total / INTERPOLATION_STEP_SECONDS) - 1
    if count < 1:
        return []

    points: list[dict] = []
    leg_count = len(legs)
    for k in range(1, count + 1):
        t_offset = move_total * k / (count + 1)
        acc = 0.0
        leg_idx = leg_count - 1
        for j in range(leg_count):
            if t_offset <= acc + durations[j] or j == leg_count - 1:
                leg_idx = j
                break
            acc += durations[j]
        leg_dur = durations[leg_idx]
        frac = (t_offset - acc) / leg_dur if leg_dur > 0 else 0.0
        frac = min(max(frac, 0.0), 1.0)
        pt = _locate_along_segments([legs[leg_idx]], lengths[leg_idx] * frac)
        if pt is None:
            continue
        speed_kmh = _half_up((lengths[leg_idx] / leg_dur) * 3.6) if leg_dur > 0 else 0
        points.append({
            "lat": pt["lat"],
            "lon": pt["lon"],
            "speed": speed_kmh,
            "time": _half_up(travel_start + t_offset),
            "direction": pt["direction"],
        })
    return points


def _apply_insertions(track: dict, insertions: list[dict]) -> dict:
    """Пересобирает трек: удаляет remove_indices и вставляет новые точки."""
    remove_set: set[int] = set()
    for ins in insertions:
        remove_set.update(int(x) for x in ins.get("remove_indices", []))

    ins_by_after: dict[int, list[dict]] = {}
    for ins in insertions:
        ins_by_after.setdefault(int(ins["after_index"]), []).extend(ins["points"])

    n = len(track["lat"])
    out = {"lat": [], "lon": [], "time": [], "speed": [], "interpolated": []}
    interp = track.get("interpolated") or [0] * n

    for i in range(n):
        if i in remove_set:
            continue
        out["lat"].append(track["lat"][i])
        out["lon"].append(track["lon"][i])
        out["time"].append(track["time"][i])
        out["speed"].append(track["speed"][i])
        out["interpolated"].append(interp[i])

        for pt in ins_by_after.get(i, []):
            out["lat"].append(pt["lat"])
            out["lon"].append(pt["lon"])
            out["time"].append(pt["time"])
            out["speed"].append(pt["speed"])
            out["interpolated"].append(1)

    return out


# ── главный метод ───────────────────────────────────────────────────

def fill_track(track: dict, routes: list[dict]) -> tuple[dict, list[dict]]:
    """Чинит трек по маршрутам. Возвращает (filled_track, diagnostics)."""
    diagnostics: list[dict] = []
    if len(track.get("lat", [])) < 2 or not routes:
        return track, diagnostics

    gaps = _detect_gaps(track)
    if not gaps:
        return track, diagnostics

    point_routes = _precompute_point_on_routes(track, routes)
    resolutions = _resolve_gaps(track, gaps, routes, point_routes)

    # План по контексту маршрутов до/после разрыва (нужны inverse_route_id
    # и/или avg_route_pass_time в routes.additional). Что он не решил —
    # чинится старой логикой ниже.
    context_plans: dict[int, dict] = {}
    if any(r.get("inverse_idx") is not None or r.get("pass_time_s") is not None for r in routes):
        context_plans = gap_context_service.resolve_gaps_by_context(track, gaps, routes)

    insertions: list[dict] = []
    for gap_idx, gap in enumerate(gaps):
        plan = context_plans.get(gap_idx)
        if plan is not None:
            pts = _interpolate_plan(gap, plan)
            if pts:
                insertions.append({
                    "after_index": gap["start_index"],
                    "points": pts,
                    "remove_indices": gap.get("remove_indices", []),
                })
                diagnostics.append({
                    "gap": gap_idx,
                    "matched_route_id": plan["route"]["id"] if plan["route"] else None,
                    "resolution_type": "context",
                    "route_seq": plan["route_seq"],
                    "turns": plan["turns"],
                    "idle_s": _half_up(plan["idle_s"]),
                    "interpolated_count": len(pts),
                })
                continue

        res = resolutions[gap_idx]

        # Коридорная резолюция достоверна, только когда якорями стали сами
        # границы разрыва: если _find_covered_anchor ушёл наружу за покрытой
        # точкой, найденный кусок маршрута относится уже не к этому разрыву.
        # Второй случай для approach — кандидатов в коридоре не нашлось вовсе
        # (все покрывающие маршруты идут назад).
        start_covered = bool(point_routes[gap["start_index"]])
        end_covered = bool(point_routes[gap["end_index"]])
        if res is None or not start_covered or not end_covered:
            approach = _resolve_gap_with_approach(gap, routes)
            if approach is not None:
                res = approach

        if res is None:
            diagnostics.append({"gap": gap_idx, "matched_route_id": None})
            continue
        pts = _interpolate_resolution(gap, res)
        if pts:
            insertions.append({
                "after_index": gap["start_index"],
                "points": pts,
                "remove_indices": gap.get("remove_indices", []),
            })
        diagnostics.append({
            "gap": gap_idx,
            "matched_route_id": res["route"]["id"],
            "resolution_type": res["type"],
            "interpolated_count": len(pts),
        })

    if insertions:
        track = _apply_insertions(track, insertions)
    return track, diagnostics
