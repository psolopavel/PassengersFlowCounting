"""Определение маршрутов и отрезков между остановками по GPS-треку (межгород).

Шаги:
  1. load_car_routes — по imei тянем привязанные маршруты (all_car_routes.routes_id),
     геометрию, остановки (along) и service-зоны (zones.type='service').
  2. detect_route_periods — ведём трек по маршрутам «оконным курсором».
     Ориентир — САМ маршрут (точки его полилинии), не остановки. На старте
     проезда по окну ~1000 м выбираем маршрут (приоритет: число зон → близость
     трека к линии → покрытие), затем ведём позицию вперёд, проецируя каждую
     точку лишь на участок линии вокруг курсора. Слом проезда — реальный
     разворот (откат курсора от пика ≥150 м) или выход из коридора; на этой
     точке выбираем маршрут заново. Так самопересечение (спур «туда-обратно»)
     остаётся ОДНИМ проездом, а встречный маршрут отсекается сам (курсор по
     нему не растёт).
  3. build_segments — внутри периода визиты остановок по близости (спур даёт
     два визита — туда и обратно) и отрезки A→B по факту трека.

Геометрию читаем сырым SQL через ST_AsText — колонки GEOMETRY не мапятся ORM.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.utils.debug_log import debug_log
from app.utils.geo import (
    cumulative_along,
    haversine,
    parse_linestring,
    parse_point,
    project_point_on_polyline,
    project_point_on_polyline_window,
    segment_lengths,
)

settings = get_settings()

# ── следование по маршруту через «оконный курсор» ───────────────────
# Держим позицию машины на полилинии (курсор по длине дуги `along`) и
# проецируем каждую следующую точку трека только на УЧАСТОК линии вокруг
# курсора. Так `along` остаётся монотонным на самопересечении (спур
# «туда-обратно» по одной дороге) и корректно ловится реальный разворот.

# Окно проекции вперёд по линии от курсора (м). Должно перекрывать
# максимальный реальный скачок между соседними точками трека (в т.ч.
# интерполированными gap-fill), но быть меньше длины любой петли-разворота.
PROJECTION_WINDOW_FWD_M = 800
# Окно назад по линии (м) — «линейка» для измерения отката (разворота).
PROJECTION_WINDOW_BACK_M = 250
# Откат курсора от достигнутого пика больше этого (м) — реальный разворот
# (машина поехала обратно по маршруту) => слом проезда.
REVERSAL_BREAK_M = 150
# Меньшие откаты — дребезг GPS на грани сегмента, не считаем разворотом.
REVERSAL_JITTER_M = 30
# Стартовое окно для выбора маршрута нового проезда (м пути по треку).
BOOTSTRAP_WINDOW_M = 1000
# Доля точек bootstrap-окна в коридоре, ниже которой маршрут не кандидат.
MIN_BOOTSTRAP_COVERAGE = 0.5

# Повторный заход в радиус остановки ближе этого по времени (сек) — тот же
# визит (дребезг GPS на границе радиуса), а не два отдельных проезда.
STOP_REVISIT_MIN_SEC = 120
# Если у остановки нет radius_m — считаем её зоной этого радиуса (м).
DEFAULT_STOP_RADIUS_M = 80
# В along-фолбэке (когда реальных GPS-точек у остановки нет) не «пробиваем»
# остановку, если между соседними точками along прыгнул больше этого (м):
# такой скачок — разрыв GPS, а не проезд, иначе времена остановок слипаются.
MAX_FALLBACK_ALONG_JUMP_M = 1000


# ── помощники для отладочных логов ──────────────────────────────────

def _fmt_utc(utc) -> str:
    """utc → 'HH:MM:SS' (UTC) для читаемости логов; при ошибке — '?'."""
    try:
        return datetime.fromtimestamp(int(utc), tz=timezone.utc).strftime("%H:%M:%S")
    except (ValueError, OSError, OverflowError, TypeError):
        return "?"


# ── загрузка маршрутов и остановок ──────────────────────────────────

def _car_route_ids(routes_db: Session, imei: str) -> list[int]:
    rows = routes_db.execute(
        text("SELECT routes_id FROM all_car_routes WHERE imei = :imei"),
        {"imei": str(imei).strip()},
    ).all()
    ids: list[int] = []
    seen: set[int] = set()
    for (raw,) in rows:
        try:
            arr = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            arr = []
        for rid in arr if isinstance(arr, list) else []:
            try:
                rid = int(rid)
            except (ValueError, TypeError):
                continue
            if rid not in seen:
                seen.add(rid)
                ids.append(rid)
    return ids


def _load_routes_geo(routes_db: Session, route_ids: list[int]) -> dict[int, dict]:
    if not route_ids:
        return {}
    stmt = text(
        "SELECT id, name, additional, ST_AsText(geom) AS wkt FROM routes "
        "WHERE id IN :ids AND active = 1"
    ).bindparams(bindparam("ids", expanding=True))
    rows = routes_db.execute(stmt, {"ids": route_ids}).all()
    result: dict[int, dict] = {}
    for r in rows:
        points = parse_linestring(r.wkt)
        if len(points) < 2:
            continue
        seg_lens = segment_lengths(points)
        total_len = sum(seg_lens)
        extra = parse_route_additional(r.additional, int(r.id), total_len)
        result[int(r.id)] = {
            "id": int(r.id),
            "name": r.name or f"Маршрут #{r.id}",
            "points": points,
            "segment_lengths": seg_lens,
            "cum_along": cumulative_along(seg_lens),
            "total_len": total_len,
            "stops": [],
            "service_zones": [],
            "pass_time_s": extra["pass_time_s"],
            "inverse_route_id": extra["inverse_route_id"],
            "inverse_idx": None,
            "inverse_entry_along": None,
        }
    return result


# ── routes.additional: время рейса и обратный маршрут ───────────────
# {"color":"#33fff1","inverse_route_id":565,"avg_route_pass_time":40}
# avg_route_pass_time — минуты. Используются починкой трека по контексту
# (gap_context_service); без них починка работает по-старому.

# Время рейса, дающее среднюю скорость вне этих границ (км/ч), игнорируется.
MIN_ROUTE_SPEED_KMH = 5
MAX_ROUTE_SPEED_KMH = 90
# Конец маршрута дальше этого от обратного маршрута — предупреждение в лог.
INVERSE_ENTRY_WARN_M = 3000


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            float(v)
        except ValueError:
            return False
        return True
    return False


def parse_route_additional(raw, route_id: int, total_len: float) -> dict:
    """Разбор routes.additional -> {'pass_time_s', 'inverse_route_id'}.

    Некорректные значения игнорируются (с записью в лог).
    """
    out: dict = {"pass_time_s": None, "inverse_route_id": None}
    if raw is None or str(raw).strip() == "":
        return out
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        debug_log("additional is not valid json", tag="gap_fill", data={"route_id": route_id})
        return out

    inv = data.get("inverse_route_id")
    if inv is not None:
        if _is_number(inv) and int(float(inv)) > 0:
            out["inverse_route_id"] = int(float(inv))
        else:
            debug_log(
                "invalid inverse_route_id, ignored",
                tag="gap_fill", data={"route_id": route_id, "value": inv},
            )

    v = data.get("avg_route_pass_time")
    if v is not None:
        if _is_number(v) and float(v) > 0:
            sec = float(v) * 60.0
            kmh = (total_len / sec) * 3.6
            if kmh < MIN_ROUTE_SPEED_KMH or kmh > MAX_ROUTE_SPEED_KMH:
                debug_log(
                    "avg_route_pass_time gives unrealistic speed, ignored",
                    tag="gap_fill",
                    data={
                        "route_id": route_id, "minutes": v,
                        "total_len_m": round(total_len), "speed_kmh": round(kmh, 1),
                    },
                )
            else:
                out["pass_time_s"] = sec
        else:
            debug_log(
                "invalid avg_route_pass_time, ignored",
                tag="gap_fill", data={"route_id": route_id, "value": v},
            )
    return out


def link_inverse_routes(routes: list[dict]) -> None:
    """Проставляет inverse_idx и inverse_entry_along (вход на обратный маршрут).

    inverse_route_id, которого нет среди маршрутов машины (не привязан или
    неактивен), игнорируется. Если у B обратный не указан, а у A указан B —
    связь B → A достраивается. Точка входа — проекция конца маршрута на первую
    половину обратного.
    """
    idx_by_id = {r["id"]: i for i, r in enumerate(routes)}
    for r in routes:
        r.setdefault("inverse_idx", None)
        r.setdefault("inverse_entry_along", None)

    for i, r in enumerate(routes):
        inv_id = r.get("inverse_route_id")
        if inv_id is None:
            continue
        j = idx_by_id.get(inv_id)
        if j is None:
            debug_log(
                "inverse route not assigned to car or inactive, ignored",
                tag="gap_fill", data={"route_id": r["id"], "inverse_route_id": inv_id},
            )
            continue
        if j == i:
            debug_log("route is inverse of itself, ignored", tag="gap_fill", data={"route_id": r["id"]})
            continue
        r["inverse_idx"] = j

    for i, r in enumerate(routes):
        j = r["inverse_idx"]
        if j is None:
            continue
        back = routes[j]["inverse_idx"]
        if back is None:
            routes[j]["inverse_idx"] = i
            debug_log(
                "inverse link completed",
                tag="gap_fill", data={"route_id": routes[j]["id"], "inverse_route_id": r["id"]},
            )
        elif back != i:
            debug_log(
                "inverse links are asymmetric",
                tag="gap_fill",
                data={
                    "route_id": r["id"], "inverse_route_id": routes[j]["id"],
                    "inverse_of_inverse": routes[back]["id"],
                },
            )

    for r in routes:
        j = r["inverse_idx"]
        if j is None:
            continue
        end_pt = r["points"][-1]
        inv = routes[j]
        proj = project_point_on_polyline_window(
            end_pt[0], end_pt[1],
            inv["points"], inv["segment_lengths"], inv["cum_along"],
            0.0, inv["total_len"] / 2.0,
        )
        if proj is None:
            r["inverse_idx"] = None
            continue
        r["inverse_entry_along"] = proj["along"]
        debug_log(
            "inverse linked" + (" (far from route end)" if proj["distance"] > INVERSE_ENTRY_WARN_M else ""),
            tag="gap_fill",
            data={
                "route_id": r["id"], "inverse_route_id": inv["id"],
                "entry_along_m": round(proj["along"]), "end_to_entry_m": round(proj["distance"]),
            },
        )


def _load_stops(routes_db: Session, route: dict) -> None:
    """Добавляет к route['stops'] список остановок с позицией along."""
    rows = routes_db.execute(
        text(
            "SELECT id, name, order_index, radius_m, ST_AsText(geom) AS wkt FROM stops "
            "WHERE route_id = :rid AND active = 1 ORDER BY order_index ASC"
        ),
        {"rid": route["id"]},
    ).all()
    stops = []
    for r in rows:
        pt = parse_point(r.wkt)
        if pt is None:
            continue
        proj = project_point_on_polyline(
            pt[0], pt[1], route["points"], route["segment_lengths"]
        )
        if proj is None:
            continue
        radius = int(r.radius_m) if r.radius_m and int(r.radius_m) > 0 else DEFAULT_STOP_RADIUS_M
        stops.append({
            "id": int(r.id),
            "name": r.name or f"Зупинка #{r.id}",
            "order": int(r.order_index) if r.order_index is not None else 0,
            "along": proj["along"],
            "lat": pt[0],
            "lon": pt[1],
            "radius_m": radius,
        })
    stops.sort(key=lambda s: s["along"])
    route["stops"] = stops


def _load_service_zones(routes_db: Session, routes: list[dict]) -> None:
    """Service-зоны (конечная/парк) по route_id. Advertising не загружаем."""
    ids = [r["id"] for r in routes]
    for r in routes:
        r["service_zones"] = []
    if not ids:
        return
    stmt = text(
        "SELECT id, name, route_id, radius_m, ST_AsText(geom) AS wkt FROM zones "
        "WHERE type = 'service' AND active = 1 AND route_id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    try:
        rows = routes_db.execute(stmt, {"ids": ids}).all()
    except Exception:
        debug_log("load_service_zones failed", tag="intercity", data={"route_ids": ids})
        return
    by_rid: dict[int, list[dict]] = {}
    for r in rows:
        pt = parse_point(r.wkt)
        if pt is None:
            continue
        radius = int(r.radius_m) if r.radius_m and int(r.radius_m) > 0 else 0
        if radius <= 0:
            continue
        rid = int(r.route_id)
        by_rid.setdefault(rid, []).append({
            "id": int(r.id),
            "name": r.name or f"Зона #{r.id}",
            "lat": pt[0],
            "lon": pt[1],
            "radius_m": radius,
        })
    for route in routes:
        route["service_zones"] = by_rid.get(route["id"], [])


def load_car_routes(routes_db: Session, imei: str) -> list[dict]:
    """Список маршрутов машины с геометрией, остановками и service-зонами."""
    ids = _car_route_ids(routes_db, imei)
    routes_map = _load_routes_geo(routes_db, ids)
    routes = [routes_map[rid] for rid in ids if rid in routes_map]
    for route in routes:
        _load_stops(routes_db, route)
    _load_service_zones(routes_db, routes)
    link_inverse_routes(routes)
    debug_log(
        "load_car_routes",
        tag="intercity",
        data={
            "imei": str(imei).strip(),
            "route_ids_in_db": ids,
            "routes_loaded": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "points": len(r["points"]),
                    "total_len_m": round(r["total_len"], 1),
                    "stops": len(r["stops"]),
                    "service_zones": len(r.get("service_zones") or []),
                    "pass_time_min": (
                        round(r["pass_time_s"] / 60.0, 1) if r.get("pass_time_s") else None
                    ),
                    "inverse_route_id": r.get("inverse_route_id"),
                }
                for r in routes
            ],
        },
    )
    return routes


# ── визиты остановок / зоны ─────────────────────────────────────────

def _stop_events(
    track: dict,
    point_indices: list[int],
    stop: dict,
    p_along: list[float],
    p_time: list[int],
) -> list[dict]:
    """Все визиты остановки внутри периода как отдельные события.

    Каждый непрерывный заход в радиус — отдельный визит {arrival, departure}.
    Проезд одной остановки дважды (туда и обратно) даёт ДВА события, чтобы
    сегменты честно шли по факту трека. Близкие по времени куски (дребезг GPS
    на границе радиуса) склеиваем. Если ни одна точка не попала в радиус
    (редкий GPS) — фолбэк на ВСЕ пересечения along остановки (arrival =
    departure = момент проезда).
    """
    times = track["time"]
    radius = stop.get("radius_m") or DEFAULT_STOP_RADIUS_M
    lat, lon = stop.get("lat"), stop.get("lon")

    raw: list[tuple[int, int]] = []
    if lat is not None and lon is not None:
        cluster: list[int] = []
        for idx in point_indices:
            if haversine(track["lat"][idx], track["lon"][idx], lat, lon) <= radius:
                cluster.append(idx)
            elif cluster:
                raw.append((int(times[cluster[0]]), int(times[cluster[-1]])))
                cluster = []
        if cluster:
            raw.append((int(times[cluster[0]]), int(times[cluster[-1]])))

    if raw:
        merged: list[list[int]] = []
        for arr, dep in raw:
            if merged and arr - merged[-1][1] <= STOP_REVISIT_MIN_SEC:
                merged[-1][1] = max(merged[-1][1], dep)
            else:
                merged.append([arr, dep])
        return [{"arrival_utc": a, "departure_utc": d} for a, d in merged]

    events: list[dict] = []
    target = stop["along"]
    for i in range(1, len(p_along)):
        a0, a1 = p_along[i - 1], p_along[i]
        # Огромный скачок along между соседними точками — это разрыв GPS, а не
        # реальный проезд. Не «пробиваем» остановку по нему, иначе несколько
        # остановок слипаются в один момент (время 1-2 сек между километрами).
        if abs(a1 - a0) > MAX_FALLBACK_ALONG_JUMP_M:
            continue
        if (a0 <= target <= a1) or (a1 <= target <= a0):
            if a1 == a0:
                t = int(p_time[i])
            else:
                frac = (target - a0) / (a1 - a0)
                t = int(round(p_time[i - 1] + (p_time[i] - p_time[i - 1]) * frac))
            if events and t - events[-1]["departure_utc"] <= STOP_REVISIT_MIN_SEC:
                continue
            events.append({"arrival_utc": t, "departure_utc": t})
    return events


def _zone_count(track: dict, point_indices: list[int], zones: list[dict]) -> int:
    """Число service-зон маршрута, в радиус которых попала хоть одна точка."""
    if not zones:
        return 0
    hit = 0
    for zone in zones:
        lat, lon, radius = zone["lat"], zone["lon"], zone["radius_m"]
        for idx in point_indices:
            if haversine(track["lat"][idx], track["lon"][idx], lat, lon) <= radius:
                hit += 1
                break
    return hit


# ── следование по маршруту: bootstrap + оконный курсор ──────────────

def _project_window(route: dict, lat: float, lon: float, cursor: float) -> dict | None:
    """Проекция точки на участок линии [cursor-back, cursor+fwd] по длине дуги.

    prefer_from=cursor — на ничьей по боковому расстоянию (возврат по той же
    дороге на спуре) предпочитаем копию впереди курсора, чтобы along рос.
    """
    return project_point_on_polyline_window(
        lat, lon,
        route["points"], route["segment_lengths"], route["cum_along"],
        cursor - PROJECTION_WINDOW_BACK_M, cursor + PROJECTION_WINDOW_FWD_M,
        prefer_from=cursor,
    )


def _bootstrap_window_indices(
    track: dict, start_i: int, end_i: int | None = None
) -> list[int]:
    """Индексы точек, покрывающие ~BOOTSTRAP_WINDOW_M пути трека от start_i
    (не дальше end_i включительно)."""
    lat, lon = track["lat"], track["lon"]
    last = len(lat) - 1 if end_i is None else end_i
    idxs = [start_i]
    dist = 0.0
    j = start_i + 1
    while j <= last and dist < BOOTSTRAP_WINDOW_M:
        dist += haversine(lat[j - 1], lon[j - 1], lat[j], lon[j])
        idxs.append(j)
        j += 1
    return idxs


def _eval_route_on_window(track: dict, route: dict, window_idx: list[int]) -> dict | None:
    """Оценивает, как трек-окно ложится на маршрут (для выбора маршрута).

    Ведём оконный курсор от глобальной проекции первой точки. Возвращает
    {'seed_along','coverage','mean_lateral','zone_count','net'} или None, если
    маршрут не кандидат: мало точек в коридоре или едем встречно (net<0).
    """
    lat, lon = track["lat"], track["lon"]
    corridor = settings.route_corridor_m
    seed = project_point_on_polyline(
        lat[window_idx[0]], lon[window_idx[0]],
        route["points"], route["segment_lengths"],
    )
    if seed is None:
        return {"ok": False, "reject": "no_seed"}
    cursor = seed["along"]
    laterals: list[float] = []
    first_along: float | None = None
    last_along: float | None = None
    in_corridor = 0
    for idx in window_idx:
        proj = _project_window(route, lat[idx], lon[idx], cursor)
        if proj is None or proj["distance"] > corridor:
            continue
        in_corridor += 1
        laterals.append(proj["distance"])
        if first_along is None:
            first_along = proj["along"]
        last_along = proj["along"]
        cursor = proj["along"]
    if in_corridor == 0 or first_along is None or last_along is None:
        return {"ok": False, "reject": "no_corridor_pts"}
    coverage = in_corridor / max(len(window_idx), 1)
    net = last_along - first_along
    info = {
        "seed_along": seed["along"],
        "coverage": coverage,
        "mean_lateral": sum(laterals) / len(laterals),
        "zone_count": _zone_count(track, window_idx, route.get("service_zones") or []),
        "net": net,
    }
    if coverage < MIN_BOOTSTRAP_COVERAGE:
        return {"ok": False, "reject": "low_coverage", **info}
    # Отсекаем только ВСТРЕЧНЫЕ (along реально убывает). Малое положительное
    # продвижение (старт с городской петлёй) не режем — пусть решает
    # follow-length: неверный маршрут всё равно сломается рано.
    if net <= 0:
        return {"ok": False, "reject": "not_forward", **info}
    return {"ok": True, **info}


def _bootstrap(
    track: dict, routes: list[dict], start_i: int, end_i: int | None = None
) -> tuple[int | None, float, dict]:
    """Выбирает маршрут нового проезда с точки start_i.

    Приоритет: число зон → длина следования → близость. Когда на старте
    подходит НЕСКОЛЬКО маршрутов (общий участок до развилки, например
    «Одеса-Дачне» и «АС Старосінна-Усатове» до Клеверного мосту), различить их
    по близости на старте нельзя — путь идентичен. Поэтому «прослеживаем»
    каждый кандидат до слома и берём тот, по которому машина реально едет
    ДАЛЬШЕ: неверный маршрут ломается на развилке, а верный идёт до конца.

    Кандидатом становится любой маршрут с покрытием ≥ порога и движением
    ВПЕРЁД (net>0); малый net на старте (городская петля) не отсекаем — пусть
    решает follow-length. В diag пишем и отклонённых, с причиной и метриками.
    Возвращает (route_idx | None, seed_along, diag).
    """
    window_idx = _bootstrap_window_indices(track, start_i, end_i)
    if len(window_idx) < 2:
        return None, 0.0, {"start_i": start_i, "candidates": []}
    evals: list[tuple[int, dict]] = [
        (r_idx, _eval_route_on_window(track, route, window_idx))
        for r_idx, route in enumerate(routes)
    ]
    viable = [(r_idx, ev) for r_idx, ev in evals if ev.get("ok")]
    # Длину следования считаем только при неоднозначности — иначе лишний проход.
    multi = len(viable) > 1
    best_idx: int | None = None
    best_key: tuple | None = None
    best_seed = 0.0
    follow_by_ridx: dict[int, int] = {}
    for r_idx, ev in viable:
        follow_len = 0
        if multi:
            pts, _ri, _br, _al = _follow_route(
                track, routes[r_idx], start_i, ev["seed_along"], end_i
            )
            follow_len = len(pts)
        follow_by_ridx[r_idx] = follow_len
        key = (ev["zone_count"], follow_len, -ev["mean_lateral"])
        if best_key is None or key > best_key:
            best_key = key
            best_idx = r_idx
            best_seed = ev["seed_along"]
    # Диагностика: и выбранные, и отклонённые — с причиной и метриками,
    # чтобы видеть, почему нужный маршрут не попал в кандидаты на развилке.
    cand_diag: list[dict] = []
    for r_idx, ev in evals:
        d: dict = {"route_id": routes[r_idx]["id"], "ok": bool(ev.get("ok"))}
        if not ev.get("ok"):
            d["reject"] = ev.get("reject")
        if "coverage" in ev:
            d["zones"] = ev["zone_count"]
            d["mean_lat"] = round(ev["mean_lateral"])
            d["coverage"] = round(ev["coverage"], 2)
            d["net_m"] = round(ev["net"])
        if r_idx in follow_by_ridx:
            d["follow_pts"] = follow_by_ridx[r_idx]
        cand_diag.append(d)
    return best_idx, best_seed, {"start_i": start_i, "candidates": cand_diag}


def _follow_route(
    track: dict, route: dict, start_i: int, seed_along: float,
    end_i: int | None = None,
) -> tuple[list[int], int, str, list[float]]:
    """Ведёт оконный курсор по маршруту от start_i до слома (не дальше end_i).

    Слом: откат курсора от достигнутого пика ≥ REVERSAL_BREAK_M (реальный
    разворот) либо выход из коридора. Разрыв трека сам по себе не слом — на нём
    расширяем поиск ВПЕРЁД по линии и переносим курсор (машина продолжила по
    маршруту). Возвращает (point_indices, resume_i, reason, alongs).
    """
    lat, lon = track["lat"], track["lon"]
    last = len(lat) - 1 if end_i is None else end_i
    corridor = settings.route_corridor_m
    cursor = seed_along
    peak = seed_along
    pts = [start_i]
    alongs = [seed_along]
    reason = "end_of_track" if end_i is None else "end_of_range"
    resume_i = last + 1
    i = start_i + 1
    while i <= last:
        proj = _project_window(route, lat[i], lon[i], cursor)
        if proj is None or proj["distance"] > corridor:
            # Возможен разрыв GPS: ищем, где трек снова лёг на маршрут ВПЕРЁД.
            fwd = project_point_on_polyline_window(
                lat[i], lon[i],
                route["points"], route["segment_lengths"], route["cum_along"],
                cursor - REVERSAL_JITTER_M, route["total_len"],
            )
            if fwd is not None and fwd["distance"] <= corridor and fwd["along"] > cursor:
                cursor = fwd["along"]
                peak = max(peak, cursor)
                pts.append(i)
                alongs.append(cursor)
                i += 1
                continue
            reason = "left_corridor"
            resume_i = i
            break
        new_along = proj["along"]
        if peak - new_along >= REVERSAL_BREAK_M:
            # Реальный разворот: текущий проезд заканчиваем на пике, обратный
            # ход отдаём следующему bootstrap (встречный маршрут / новый рейс).
            peak_pos = max(range(len(alongs)), key=lambda k: alongs[k])
            resume_i = pts[peak_pos] + 1
            pts = pts[:peak_pos + 1]
            alongs = alongs[:peak_pos + 1]
            reason = "reversal"
            break
        cursor = new_along
        if new_along > peak:
            peak = new_along
        pts.append(i)
        alongs.append(new_along)
        i += 1
    return pts, resume_i, reason, alongs


def _valid_pass(track: dict, pts: list[int]) -> bool:
    """Проезд достоверен, если достаточно точек и длительности (отсев шума)."""
    if len(pts) < settings.route_min_period_points:
        return False
    times = track["time"]
    return times[pts[-1]] - times[pts[0]] >= settings.route_min_period_sec


def detect_route_periods(track: dict, routes: list[dict]) -> list[dict]:
    """Периоды проездов: ведём трек по маршрутам оконным курсором.

    На каждом старте по окну ~1000 м выбираем наиболее подходящий маршрут
    (зоны → близость → покрытие), ведём курсор вперёд до слома (разворот
    ≥150 м или выход из коридора) и повторяем с точки слома. Проезды по
    построению не пересекаются во времени — отдельный разбор спорных окон
    не нужен. Самопересечение маршрута (спур «туда-обратно») штатно остаётся
    ОДНИМ проездом: along по оконному курсору монотонен и не считает возврат
    по той же дороге разворотом.
    """
    n = len(track.get("lat", []))
    times = track["time"]
    passes: list[dict] = []
    boot_diag: list[dict] = []
    i = 0
    while i < n:
        r_idx, seed, diag = _bootstrap(track, routes, i)
        if r_idx is None:
            i += 1
            continue
        pts, resume_i, reason, _alongs = _follow_route(track, routes[r_idx], i, seed)
        if _valid_pass(track, pts):
            passes.append({"route_idx": r_idx, "point_indices": pts})
            boot_diag.append({
                **diag,
                "picked_route_id": routes[r_idx]["id"],
                "pass_points": len(pts),
                "pass_utc": [int(times[pts[0]]), int(times[pts[-1]])],
                "pass_hh": [_fmt_utc(times[pts[0]]), _fmt_utc(times[pts[-1]])],
                "break": reason,
            })
        if resume_i <= i:
            resume_i = i + 1
        i = resume_i

    occ: dict[int, int] = {}
    result: list[dict] = []
    for p in passes:
        route = routes[p["route_idx"]]
        occ[route["id"]] = occ.get(route["id"], 0) + 1
        pts = p["point_indices"]
        result.append({
            "route_idx": p["route_idx"],
            "route_id": route["id"],
            "route_name": route["name"],
            "start_utc": int(times[pts[0]]),
            "end_utc": int(times[pts[-1]]),
            "occurrence": occ[route["id"]],
            "point_indices": pts,
        })

    debug_log(
        "detect_route_periods",
        tag="intercity",
        data={
            "track_points": n,
            "corridor_m": settings.route_corridor_m,
            "window_fwd_m": PROJECTION_WINDOW_FWD_M,
            "window_back_m": PROJECTION_WINDOW_BACK_M,
            "reversal_break_m": REVERSAL_BREAK_M,
            "bootstrap_window_m": BOOTSTRAP_WINDOW_M,
            "periods_kept": len(result),
            "periods": [
                {
                    "route_id": p["route_id"],
                    "occurrence": p["occurrence"],
                    "points": len(p["point_indices"]),
                    "start_utc": p["start_utc"],
                    "end_utc": p["end_utc"],
                    "start_hh": _fmt_utc(p["start_utc"]),
                    "end_hh": _fmt_utc(p["end_utc"]),
                }
                for p in result
            ],
            "bootstrap": boot_diag,
        },
    )
    return result


def detect_route_periods_in_ranges(
    track: dict, routes: list[dict], ranges: list[tuple[int, int]]
) -> list[dict]:
    """Проезды только ВНУТРИ кусков трека [from, to] (включительно).

    Для починки трека по контексту (gap_context_service): проезд не может
    перескочить через разрыв, иначе стоянка + скачок склеиваются в один
    «проезд вперёд» и контекст до/после разрыва теряется.
    Возвращает [{'route_idx','route_id','start_index','end_index','start_utc',
    'end_utc','along_start','along_end','points','break'}].
    """
    times = track["time"]
    periods: list[dict] = []
    for range_from, range_to in ranges:
        i = int(range_from)
        end = int(range_to)
        while i <= end:
            r_idx, seed, _diag = _bootstrap(track, routes, i, end)
            if r_idx is None:
                i += 1
                continue
            route = routes[r_idx]
            pts, resume_i, reason, alongs = _follow_route(track, route, i, seed, end)
            if _valid_pass(track, pts):
                periods.append({
                    "route_idx": r_idx,
                    "route_id": route["id"],
                    "start_index": pts[0],
                    "end_index": pts[-1],
                    "start_utc": int(times[pts[0]]),
                    "end_utc": int(times[pts[-1]]),
                    "along_start": alongs[0],
                    "along_end": alongs[-1],
                    "points": len(pts),
                    "break": reason,
                })
            if resume_i <= i:
                resume_i = i + 1
            i = resume_i

    debug_log(
        "detect_route_periods_in_ranges",
        tag="gap_fill",
        data={
            "ranges": len(ranges),
            "periods": [
                {
                    "route_id": p["route_id"],
                    "idx": f"{p['start_index']}->{p['end_index']}",
                    "along": f"{round(p['along_start'])}->{round(p['along_end'])}",
                    "start_hh": _fmt_utc(p["start_utc"]),
                    "end_hh": _fmt_utc(p["end_utc"]),
                    "points": p["points"],
                    "break": p["break"],
                }
                for p in periods
            ],
        },
    )
    return periods


# ── отрезки между остановками ───────────────────────────────────────

def _order_predecessor(stops: list[dict], target: dict) -> dict | None:
    """Остановка с наибольшим order, меньшим order цели (соседняя «до» неё)."""
    order = target.get("order", 0)
    best: dict | None = None
    for s in stops:
        if s["id"] == target["id"]:
            continue
        o = s.get("order", 0)
        if o < order and (best is None or o > best.get("order", 0)):
            best = s
    return best


def _mk_segment(
    route: dict, occurrence: int, seq: int, *,
    from_stop: dict, to_stop: dict,
    from_arr: int, from_dep: int, to_arr: int, to_dep: int,
    start_utc: int, end_utc: int, assumed_start: bool = False,
) -> dict:
    seg = {
        "seg_key": f"{route['id']}:{from_stop['id']}:{to_stop['id']}:{occurrence}:{seq}",
        "route_id": route["id"],
        "route_name": route["name"],
        "occurrence": occurrence,
        "from_stop": {
            "id": from_stop["id"],
            "name": from_stop["name"],
            "order": from_stop["order"],
            "arrival_utc": from_arr,
            "departure_utc": from_dep,
        },
        "to_stop": {
            "id": to_stop["id"],
            "name": to_stop["name"],
            "order": to_stop["order"],
            "arrival_utc": to_arr,
            "departure_utc": to_dep,
        },
        "period_start_utc": start_utc,
        "period_end_utc": end_utc,
    }
    if assumed_start:
        seg["assumed_start"] = True
    return seg


def build_segments(track: dict, routes: list[dict], periods: list[dict]) -> list[dict]:
    """Отрезки между СОСЕДНИМИ ВО ВРЕМЕНИ визитами остановок внутри периода.

    Ведём строго по факту трека: события-визиты сортируем по времени и делаем
    отрезок на каждую соседнюю пару. Так корректно попадают развороты и
    встречные пары (A→B, B→C, … D→C, C→B, B→C, … E→F) и повторные проезды.
    Окно отрезка = отправление с A → прибытие на B.

    Если первая по времени посещённая остановка — не первая по порядку зоны,
    добавляем ведущий отрезок <предыдущая по order> → <первая посещённая> с
    окном «начало периода → прибытие», чтобы посчитать уже едущих в салоне.
    """
    times = track["time"]
    segments: list[dict] = []
    period_diag: list[dict] = []

    for period in periods:
        route = routes[period["route_idx"]]
        stops = route["stops"]
        if len(stops) < 2:
            period_diag.append({
                "route_id": route["id"], "occurrence": period["occurrence"],
                "skip": "stops<2", "stops": len(stops),
            })
            continue

        pts = period["point_indices"]
        p_along = []
        p_time = []
        for idx in pts:
            proj = project_point_on_polyline(
                track["lat"][idx], track["lon"][idx],
                route["points"], route["segment_lengths"],
            )
            if proj is None:
                continue
            p_along.append(proj["along"])
            p_time.append(int(times[idx]))
        if len(p_along) < 2:
            period_diag.append({
                "route_id": route["id"], "occurrence": period["occurrence"],
                "skip": "p_along<2", "p_along": len(p_along),
            })
            continue

        events = []
        for stop in stops:
            for ev in _stop_events(track, pts, stop, p_along, p_time):
                events.append({"stop": stop, **ev})
        events.sort(key=lambda v: (v["arrival_utc"], v["departure_utc"]))

        # Склеиваем ПОДРЯД идущие визиты ОДНОЙ зоны (стоянка/манёвр у конечной,
        # многократный вход-выход из радиуса) в один визит — иначе плодятся
        # бессмысленные отрезки «A → A» с временем касания края радиуса.
        # Повторный визит зоны, когда между ними была ДРУГАЯ зона (разворот
        # D→C→B→C), сохраняется — склеиваем только соседние одинаковые.
        collapsed: list[dict] = []
        for ev in events:
            if collapsed and collapsed[-1]["stop"]["id"] == ev["stop"]["id"]:
                collapsed[-1]["departure_utc"] = max(
                    collapsed[-1]["departure_utc"], ev["departure_utc"]
                )
            else:
                collapsed.append(ev)
        events = collapsed

        occurrence = period["occurrence"]
        period_start = int(times[pts[0]])
        before = len(segments)
        seq = 0

        # Какие визиты остановок реально нашлись в этом периоде и с каким
        # временем. По ним строятся отрезки, поэтому если тут не хватает
        # остановок (например, обратного хода) — видно сразу.
        debug_log(
            "build_segments: визиты периода",
            tag="intercity",
            data={
                "route_id": route["id"],
                "route_name": route["name"],
                "occurrence": occurrence,
                "period_utc": [period_start, int(times[pts[-1]])],
                "period_hh": [_fmt_utc(period_start), _fmt_utc(times[pts[-1]])],
                "along_range_m": [round(min(p_along)), round(max(p_along))],
                "visits": [
                    {
                        "stop": ev["stop"]["name"],
                        "order": ev["stop"]["order"],
                        "arr_utc": ev["arrival_utc"],
                        "arr_hh": _fmt_utc(ev["arrival_utc"]),
                        "dep_hh": _fmt_utc(ev["departure_utc"]),
                    }
                    for ev in events
                ],
            },
        )

        if events:
            first = events[0]
            prev_stop = _order_predecessor(stops, first["stop"])
            if prev_stop is not None and first["arrival_utc"] > period_start:
                segments.append(_mk_segment(
                    route, occurrence, seq,
                    from_stop=prev_stop, to_stop=first["stop"],
                    from_arr=period_start, from_dep=period_start,
                    to_arr=first["arrival_utc"], to_dep=first["departure_utc"],
                    start_utc=period_start, end_utc=first["arrival_utc"],
                    assumed_start=True,
                ))
                seq += 1

        for i in range(1, len(events)):
            a = events[i - 1]
            b = events[i]
            start_utc = a["departure_utc"]
            end_utc = b["arrival_utc"]
            if end_utc <= start_utc:
                start_utc = a["arrival_utc"]
            if end_utc <= start_utc:
                continue
            segments.append(_mk_segment(
                route, occurrence, seq,
                from_stop=a["stop"], to_stop=b["stop"],
                from_arr=a["arrival_utc"], from_dep=a["departure_utc"],
                to_arr=b["arrival_utc"], to_dep=b["departure_utc"],
                start_utc=start_utc, end_utc=end_utc,
            ))
            seq += 1

        period_diag.append({
            "route_id": route["id"], "occurrence": occurrence,
            "stops": len(stops), "events": len(events),
            "segments": len(segments) - before,
        })

    segments.sort(key=lambda s: s["period_start_utc"])
    debug_log(
        "build_segments",
        tag="intercity",
        data={"periods": len(periods), "segments": len(segments), "per_period": period_diag},
    )
    return segments
