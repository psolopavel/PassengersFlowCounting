"""Гібридний підрахунок міжміста (cars.count_optimization = hybrid).

Де є трек, ходки й відрізки визначаються автоматично (як у режимі auto, але без
дорисовки). Ділянки, де трек відповіді не дає, стають вікнами ручної розмітки —
рахувальник розмічає їх за відео ходками ручної хронології.

Вікно — це:
  * розрив треку: між сусідніми точками ≥ GAP_MIN_SEC (на стоянці трекер шле
    точки з нульовою швидкістю, тож тиша — це збій) або ≥ NOFIX_GAP_MIN_SEC,
    якщо між ними трекер слав точки без фіксу GNSS (track["nofix"]: де була
    машина — невідомо), зокрема на початку й у кінці дня;
  * проскочена зупинка: усередині проїзду лінія «перестрибнула» зупинку, у якої
    за проїзд немає жодного візиту;
  * з'їзд з маршруту: між проїздами машина від'їжджала ≥ OFF_ROUTE_MIN_M від
    місць, де закінчився попередній і почався наступний проїзд (крім чистого
    обороту «кінцева → початок маршруту»).

Краї вікна — якорі з треку: остання зупинка перед вікном (left) і перша після
нього (right) з часом, а також крайні точки треку на лінії (edge_utc, edge_km).
  * Перша ходка вікна продовжує проїзд перед ним (from_left), якщо той
    закінчився не на кінцевій: маршрут і початкова зупинка — з якоря, перша
    відмітка — сам якір (anchor), кількість після нього вводить рахувальник.
    Рахувальник може позначити ended_on_track: рейс закінчився на треку і в
    розрив не продовжувався. Така ходка лишається лише позначкою, щоб її не
    створили знову; у список відрізків і у звіт вона не потрапляє.
  * Незавершена остання ходка вікна переходить у проїзд після вікна, якщо
    маршрут той самий і зупинка після вікна не раніше за маршрутом.
  * Проміжні зупинки ходки розподіляються лінійно за часом між найближчими
    відомими точками (якорі, краї треку, початок і кінець від рахувальника).
Нічого не введено — нічого й не перевіряється: проїзд перед вікном закінчується,
після вікна починається (як у режимі auto).

У data сесії:
    mode            — "hybrid";
    routes_catalog  — як у ручній хронології (зупинки з km);
    data_end_utc    — далі цього часу даних немає (кінець дня або момент збору);
    track_start_utc, track_end_utc — межі треку дня;
    auto_blocks     — [{uid, route_id, route_name, start_utc, end_utc,
                        first_stop_id, last_stop_id, fresh, complete}];
    auto_segments   — відрізки з треку (count вводить рахувальник) з block_uid;
                      assumed_start — ведучий відрізок, відкидається, коли в
                      блок переходить ходка вікна;
    windows         — [{uid, start_utc, end_utc, reason, left, right}];
                      left / right: {block_uid, route_id, stop_id, utc, km,
                      edge_utc, edge_km, open};
    chronology      — ходки вікон: [{uid, window_uid, from_left, ended_on_track,
                        route_id, route_name, closed, start_stop_id, end_stop_id,
                        marks: [{uid, utc, count_after, anchor?}]}];
    segments, route_periods — зведення авто- і ручних відрізків у форматі режиму
                      auto (source: "track" | "manual"), тому FIFO, зведення,
                      звіти та API працюють без змін.

Розрахунок ходок вікон дублюється в static/js/count_intercity_hybrid.js
(resolveTrip, continuesIntoRight) — правила мають збігатися.
"""

from __future__ import annotations

import secrets

from app.services import manual_chronology_service as manual_svc
from app.services.route_match_service import (
    _order_predecessor,
    detect_route_periods_in_ranges,
    period_visits,
)
from app.utils.geo import haversine
from app.utils.local_tz import unix_to_local

MODE_HYBRID = "hybrid"

GAP_MIN_SEC = 15 * 60
NOFIX_GAP_MIN_SEC = 5 * 60
OFF_ROUTE_MIN_M = 500
EDGE_TOL_SEC = 60

REASON_GAP = "gap"
REASON_SKIP = "skip"
REASON_OFF_ROUTE = "off_route"
REASON_NO_TRACK = "no_track"
REASON_NO_ROUTE = "no_route"


def is_hybrid(data: dict | None) -> bool:
    return bool(data) and data.get("mode") == MODE_HYBRID


def _hms(utc: int) -> str:
    return unix_to_local(int(utc)).strftime("%H:%M:%S")


def _hm(utc: int) -> str:
    return unix_to_local(int(utc)).strftime("%H:%M")


# ── трек → блоки й вікна ─────────────────────────────────────────────

def _is_gap(t0: int, t1: int, nofix: list[tuple[int, int]]) -> bool:
    """Розрив між моментами t0 < t1: довга тиша або пропуск без фіксу GNSS."""
    dt = t1 - t0
    if dt >= GAP_MIN_SEC:
        return True
    return dt >= NOFIX_GAP_MIN_SEC and any(t0 <= a and b <= t1 for a, b in nofix)


def _gap_flags(times: list[int], nofix: list[tuple[int, int]]) -> list[bool]:
    """gaps[k] — розрив між точками k і k+1."""
    return [_is_gap(times[k], times[k + 1], nofix) for k in range(len(times) - 1)]


def _ranges(n: int, gaps: list[bool]) -> list[tuple[int, int]]:
    """Шматки треку між розривами (індекси включно)."""
    if n == 0:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    for i in range(1, n):
        if gaps[i - 1]:
            out.append((start, i - 1))
            start = i
    out.append((start, n - 1))
    return out


def _has_gap(gaps: list[bool], i: int, j: int) -> bool:
    return any(gaps[k] for k in range(max(0, i), min(j, len(gaps))))


def _excursion(track: dict, i: int, j: int, refs: list[int]) -> float:
    """Найбільша відстань точки [i..j] від найближчої з точок refs (м)."""
    lat, lon = track["lat"], track["lon"]
    best = 0.0
    for k in range(max(0, i), min(j, len(lat) - 1) + 1):
        d = min(haversine(lat[k], lon[k], lat[r], lon[r]) for r in refs)
        best = max(best, d)
    return best


def _jump_edges(
    p_along: list[float], p_time: list[int], t_from: int, t_to: int,
) -> tuple[tuple[int, float], tuple[int, float]] | None:
    """Найбільший стрибок лінії між сусідніми точками в [t_from, t_to]."""
    best = None
    for i in range(len(p_time) - 1):
        if p_time[i] < t_from or p_time[i + 1] > t_to:
            continue
        jump = p_along[i + 1] - p_along[i]
        if best is None or jump > best[0]:
            best = (jump, i)
    if best is None:
        return None
    i = best[1]
    return (p_time[i], p_along[i]), (p_time[i + 1], p_along[i + 1])


def _split_on_skips(route: dict, events: list[dict]) -> list[list[dict]]:
    """Ділить візити проїзду там, де лінія перескочила зупинку без візиту."""
    visited = {ev["stop"]["id"] for ev in events}
    parts = [[events[0]]]
    for a, b in zip(events, events[1:]):
        lo, hi = a["stop"]["along"], b["stop"]["along"]
        skipped = hi > lo and any(
            lo < s["along"] < hi and s["id"] not in visited for s in route["stops"]
        )
        if skipped:
            parts.append([b])
        else:
            parts[-1].append(b)
    return parts


def _track_km(track: dict, start: int, end: int) -> float:
    """Пробіг за [start, end] без стрибків через розрив: пара точок, що виходить
    за межі відрізка більше ніж на EDGE_TOL_SEC, не враховується."""
    lat, lon, times = track["lat"], track["lon"], track["time"]
    total = 0.0
    for i in range(1, len(times)):
        t0, t1 = int(times[i - 1]), int(times[i])
        if t1 < start or t0 > end or t0 < start - EDGE_TOL_SEC or t1 > end + EDGE_TOL_SEC:
            continue
        total += haversine(lat[i - 1], lon[i - 1], lat[i], lon[i])
    return round(total / 1000.0, 2)


def _stop_ref(stop: dict, arrival: int, departure: int) -> dict:
    return {
        "id": stop["id"],
        "name": stop["name"],
        "order": stop["order"],
        "arrival_utc": arrival,
        "departure_utc": departure,
    }


def _auto_segment(route: dict, block_uid: str, a: dict, b: dict, start: int, end: int,
                  track: dict, assumed: bool = False) -> dict:
    seg = {
        "seg_key": f"a:{route['id']}:{a['id']}:{b['id']}:{start}",
        "route_id": route["id"],
        "route_name": route["name"],
        "from_stop": a,
        "to_stop": b,
        "period_start_utc": start,
        "period_end_utc": end,
        "count": None,
        "distance_km": _track_km(track, start, end),
        "source": "track",
        "block_uid": block_uid,
    }
    if assumed:
        seg["assumed_start"] = True
    return seg


def _block_segments(route: dict, block: dict, events: list[dict], track: dict,
                    period_start: int | None) -> list[dict]:
    """Відрізки блоку між сусідніми візитами; period_start — для ведучого відрізка."""
    segs: list[dict] = []
    first = events[0]
    if period_start is not None:
        prev_stop = _order_predecessor(route["stops"], first["stop"])
        if prev_stop is not None and first["arrival_utc"] > period_start:
            segs.append(_auto_segment(
                route, block["uid"],
                _stop_ref(prev_stop, period_start, period_start),
                _stop_ref(first["stop"], first["arrival_utc"], first["departure_utc"]),
                period_start, first["arrival_utc"], track, assumed=True,
            ))
    for a, b in zip(events, events[1:]):
        start, end = a["departure_utc"], b["arrival_utc"]
        if end <= start:
            start = a["arrival_utc"]
        if end <= start:
            continue
        segs.append(_auto_segment(
            route, block["uid"],
            _stop_ref(a["stop"], a["arrival_utc"], a["departure_utc"]),
            _stop_ref(b["stop"], b["arrival_utc"], b["departure_utc"]),
            start, end, track,
        ))
    return segs


def _catalog_km(catalog_by_id: dict, route_id: int, stop_id: int, along: float) -> float:
    for s in (catalog_by_id.get(route_id) or {}).get("stops") or []:
        if s["id"] == stop_id and s.get("km") is not None:
            return float(s["km"])
    return round(along / 1000.0, 3)


def _anchor(block: dict, side: str, catalog_by_id: dict) -> dict:
    """Якір вікна: остання зупинка блоку (left) або перша (right) + край треку."""
    ev = block["_last"] if side == "left" else block["_first"]
    utc = ev["departure_utc"] if side == "left" else ev["arrival_utc"]
    edge_t, edge_along = block["_edge_end"] if side == "left" else block["_edge_start"]
    edge_ok = edge_t > utc if side == "left" else edge_t < utc
    return {
        "block_uid": block["uid"],
        "route_id": block["route_id"],
        "stop_id": ev["stop"]["id"],
        "utc": utc,
        "km": _catalog_km(catalog_by_id, block["route_id"], ev["stop"]["id"], ev["stop"]["along"]),
        "edge_utc": int(edge_t) if edge_ok else None,
        "edge_km": round(edge_along / 1000.0, 3) if edge_ok else None,
        "open": not block["complete"] if side == "left" else not block["fresh"],
    }


def _window(left: dict | None, right: dict | None, reason: str,
            day_start: int, data_end: int) -> dict | None:
    start = left["utc"] if left else day_start
    end = right["utc"] if right else data_end
    if end <= start:
        return None
    return {
        "uid": f"w{start}",
        "start_utc": start,
        "end_utc": end,
        "reason": reason,
        "left": left,
        "right": right,
    }


def build_parts(
    track: dict, routes: list[dict], catalog: list[dict],
    day_start: int, data_end: int,
) -> dict:
    """Блоки з треку, їхні відрізки і вікна ручної розмітки між ними.

    -> {"auto_blocks", "auto_segments", "windows"}.
    """
    times = [int(t) for t in track.get("time") or []]
    n = len(times)
    nofix = track.get("nofix") or []
    gaps = _gap_flags(times, nofix)
    catalog_by_id = {r["id"]: r for r in catalog}

    periods = []
    if routes and n >= 2:
        periods = detect_route_periods_in_ranges(track, routes, _ranges(n, gaps))
    periods.sort(key=lambda p: p["start_utc"])

    blocks: list[dict] = []
    segments: list[dict] = []
    splits: list[tuple[dict, dict]] = []
    for period in periods:
        route = routes[period["route_idx"]]
        if len(route["stops"]) < 2:
            continue
        pts = period["point_indices"]
        visits = period_visits(track, route, pts)
        if visits is None or not visits[0]:
            continue
        events, p_along, p_time = visits
        parts = _split_on_skips(route, events)
        edges = [((p_time[0], p_along[0]), None)]
        for prev, nxt in zip(parts, parts[1:]):
            jump = _jump_edges(p_along, p_time, prev[-1]["departure_utc"], nxt[0]["arrival_utc"])
            left_edge = jump[0] if jump else (prev[-1]["departure_utc"], prev[-1]["stop"]["along"])
            right_edge = jump[1] if jump else (nxt[0]["arrival_utc"], nxt[0]["stop"]["along"])
            edges[-1] = (edges[-1][0], left_edge)
            edges.append((right_edge, None))
        edges[-1] = (edges[-1][0], (p_time[-1], p_along[-1]))

        period_blocks = []
        for k, evs in enumerate(parts):
            first, last = evs[0], evs[-1]
            block = {
                "uid": f"b{first['arrival_utc']}",
                "route_id": route["id"],
                "route_name": route["name"],
                "start_utc": first["arrival_utc"],
                "end_utc": last["departure_utc"],
                "first_stop_id": first["stop"]["id"],
                "last_stop_id": last["stop"]["id"],
                "fresh": first["stop"]["id"] == route["stops"][0]["id"],
                "complete": last["stop"]["id"] == route["stops"][-1]["id"],
                "_first": first,
                "_last": last,
                "_edge_start": edges[k][0],
                "_edge_end": edges[k][1],
                "_start_idx": pts[0],
                "_end_idx": pts[-1],
            }
            segments.extend(_block_segments(
                route, block, evs, track, times[pts[0]] if k == 0 else None,
            ))
            period_blocks.append(block)
        for prev, nxt in zip(period_blocks, period_blocks[1:]):
            splits.append((prev, nxt))
        blocks.extend(period_blocks)

    blocks.sort(key=lambda b: b["start_utc"])
    split_pairs = {(a["uid"], b["uid"]) for a, b in splits}
    windows: list[dict] = []

    def add(left_block, right_block, reason):
        left = _anchor(left_block, "left", catalog_by_id) if left_block else None
        right = _anchor(right_block, "right", catalog_by_id) if right_block else None
        w = _window(left, right, reason, day_start, data_end)
        if w is not None:
            windows.append(w)

    if not blocks:
        add(None, None, REASON_NO_TRACK if n == 0 else REASON_NO_ROUTE)
    else:
        first = blocks[0]
        if _is_gap(day_start, times[0], nofix) or _has_gap(gaps, 0, first["_start_idx"]):
            add(None, first, REASON_GAP)
        elif not first["fresh"] and _excursion(
            track, 0, first["_start_idx"], [first["_start_idx"]]
        ) >= OFF_ROUTE_MIN_M:
            add(None, first, REASON_OFF_ROUTE)

        for p, q in zip(blocks, blocks[1:]):
            if (p["uid"], q["uid"]) in split_pairs:
                add(p, q, REASON_SKIP)
            elif _has_gap(gaps, p["_end_idx"], q["_start_idx"]):
                add(p, q, REASON_GAP)
            elif not (p["complete"] and q["fresh"]) and _excursion(
                track, p["_end_idx"], q["_start_idx"], [p["_end_idx"], q["_start_idx"]]
            ) >= OFF_ROUTE_MIN_M:
                add(p, q, REASON_OFF_ROUTE)

        last = blocks[-1]
        if _is_gap(times[-1], data_end, nofix) or _has_gap(gaps, last["_end_idx"], n - 1):
            add(last, None, REASON_GAP)
        elif not last["complete"] and _excursion(
            track, last["_end_idx"], n - 1, [last["_end_idx"]]
        ) >= OFF_ROUTE_MIN_M:
            add(last, None, REASON_OFF_ROUTE)

    windows.sort(key=lambda w: w["start_utc"])
    public_blocks = [{k: v for k, v in b.items() if not k.startswith("_")} for b in blocks]
    segments.sort(key=lambda s: s["period_start_utc"])
    return {"auto_blocks": public_blocks, "auto_segments": segments, "windows": windows}


# ── ходки вікон ──────────────────────────────────────────────────────

def _new_uid(seen: set[str]) -> str:
    uid = secrets.token_hex(6)
    while uid in seen:
        uid = secrets.token_hex(6)
    seen.add(uid)
    return uid


def _default_left_trip(window: dict, routes: dict[int, dict], seen: set[str]) -> dict:
    left = window["left"]
    return {
        "uid": _new_uid(seen),
        "window_uid": window["uid"],
        "from_left": True,
        "route_id": left["route_id"],
        "route_name": (routes.get(left["route_id"]) or {}).get("name") or "",
        "closed": False,
        "start_stop_id": left["stop_id"],
        "end_stop_id": None,
        "marks": [{"uid": _new_uid(seen), "utc": left["utc"], "count_after": None, "anchor": True}],
    }


def _wants_left_trip(window: dict) -> bool:
    left = window.get("left")
    return bool(left and left.get("open"))


def sanitize_chronology(raw, windows: list[dict], catalog: list[dict]) -> list[dict]:
    """Ходки вікон з браузера → канонічний вигляд; у кожному вікні з відкритим
    лівим якорем рівно одна ходка from_left (з якорем першою відміткою).

    ended_on_track на цій ходці означає, що рейс закінчився на треку: якір
    лишається, щоб продовження не створилось знову, зупинки розриву зникають.
    """
    windows_by_uid = {w["uid"]: w for w in windows}
    routes = {r["id"]: r for r in catalog}
    seen: set[str] = set()
    trips: list[dict] = []
    for t in raw if isinstance(raw, list) else []:
        if not isinstance(t, dict):
            continue
        window = windows_by_uid.get(str(t.get("window_uid") or ""))
        if window is None:
            continue
        base = manual_svc.sanitize_chronology([t])
        if not base:
            continue
        trip = base[0]
        for uid in [trip["uid"], *(m["uid"] for m in trip["marks"])]:
            seen.add(uid)
        raw_marks = [m for m in (t.get("marks") or []) if isinstance(m, dict)]
        raw_anchor = next((m for m in raw_marks if m.get("anchor")), None)
        anchor = None
        if raw_anchor is not None:
            anchor = next((m for m in trip["marks"] if m["uid"] == str(raw_anchor.get("uid"))), None)
        rest = [m for m in trip["marks"] if m is not anchor]
        from_left = bool(t.get("from_left")) and _wants_left_trip(window)
        ended_on_track = from_left and bool(t.get("ended_on_track"))
        if from_left:
            left = window["left"]
            anchor_mark = {
                "uid": anchor["uid"] if anchor else _new_uid(seen),
                "utc": left["utc"],
                "count_after": None if ended_on_track else (anchor["count_after"] if anchor else None),
                "anchor": True,
            }
            if ended_on_track:
                trip["marks"] = [anchor_mark]
                trip["closed"] = False
                trip["end_stop_id"] = None
            else:
                trip["marks"] = [anchor_mark] + [m for m in rest if m["utc"] > left["utc"]]
            trip["route_id"] = left["route_id"]
            trip["start_stop_id"] = left["stop_id"]
        else:
            trip["marks"] = rest
        if not trip["marks"]:
            continue
        trip["window_uid"] = window["uid"]
        trip["from_left"] = from_left
        trip["ended_on_track"] = ended_on_track
        trip["route_name"] = (routes.get(trip["route_id"]) or {}).get("name") or trip["route_name"]
        trips.append(trip)

    out: list[dict] = []
    for window in windows:
        own = sorted(
            (t for t in trips if t["window_uid"] == window["uid"]),
            key=lambda t: (not t["from_left"], t["marks"][0]["utc"]),
        )
        lefts = [t for t in own if t["from_left"]]
        for extra in lefts[1:]:
            extra["from_left"] = False
            extra["marks"] = [m for m in extra["marks"] if not m.get("anchor")]
        own = [t for t in own if t["marks"]]
        if _wants_left_trip(window) and not lefts:
            own.insert(0, _default_left_trip(window, routes, seen))
        own.sort(key=lambda t: (not t["from_left"], t["marks"][0]["utc"]))
        out.extend(own)
    return out


def _operator_marks(trip: dict) -> list[dict]:
    return [m for m in trip["marks"] if not m.get("anchor")]


def _is_bare_left(trip: dict) -> bool:
    return trip.get("from_left") and not trip.get("closed") and not _operator_marks(trip)


def continues_into_right(trip: dict, window: dict, route: dict | None, is_last: bool) -> bool:
    """Незавершена остання ходка вікна переходить у проїзд після вікна."""
    right = window.get("right")
    if not is_last or trip.get("closed") or not right or right["route_id"] != trip["route_id"]:
        return False
    stops = {s["id"]: s for s in manual_svc.route_stop_positions(route)}
    start, end = stops.get(trip.get("start_stop_id")), stops.get(right["stop_id"])
    if start is None or end is None or end["pos"] < start["pos"]:
        return False
    return all(m["utc"] < right["utc"] for m in trip["marks"])


def _interp(knots: list[tuple[int, float]], utc: int) -> float:
    if utc <= knots[0][0]:
        return knots[0][1]
    for (t0, p0), (t1, p1) in zip(knots, knots[1:]):
        if utc <= t1:
            return p0 if t1 == t0 else p0 + (utc - t0) / (t1 - t0) * (p1 - p0)
    return knots[-1][1]


def resolve_trip(trip: dict, window: dict, route: dict | None, is_last: bool) -> dict | None:
    """Зупинки відміток ходки вікна: {"points": [{stop, pos, estimated, mark}], "continues"}.

    Ходка, що переходить у проїзд після вікна, закінчується віртуальною відміткою
    на першій зупинці після вікна. Проміжна відмітка: позиція — лінійно за часом
    між найближчими відомими точками (knots), далі найближча зупинка між
    початковою і кінцевою, не раніше попередньої (на рівній відстані — раніша).
    """
    marks = trip.get("marks") or []
    stops = manual_svc.route_stop_positions(route)
    if not marks or not stops:
        return None
    index = {s["id"]: i for i, s in enumerate(stops)}
    ia = index.get(trip.get("start_stop_id"))
    if ia is None:
        return None
    continues = continues_into_right(trip, window, route, is_last)
    if trip.get("closed"):
        ib = index.get(trip.get("end_stop_id"))
        if ib is None or len(marks) < 2 or stops[ib]["pos"] <= stops[ia]["pos"]:
            return None
        inner, end_mark = marks[1:-1], marks[-1]
    elif continues:
        right = window["right"]
        ib = index[right["stop_id"]]
        inner = marks[1:]
        end_mark = {"uid": f"R{window['uid']}", "utc": right["utc"], "count_after": None}
    else:
        return None
    t0, t1 = marks[0]["utc"], end_mark["utc"]
    if t1 <= t0:
        return None
    pos_a, pos_b = stops[ia]["pos"], stops[ib]["pos"]

    knots = [(t0, pos_a)]
    if manual_svc._distance_scale(route):
        left, right = window.get("left"), window.get("right")
        if trip.get("from_left") and left and left.get("edge_utc") and t0 < left["edge_utc"] < t1:
            knots.append((left["edge_utc"], float(left["edge_km"])))
        if continues and right.get("edge_utc") and knots[-1][0] < right["edge_utc"] < t1:
            knots.append((right["edge_utc"], float(right["edge_km"])))
    knots.append((t1, pos_b))
    level = pos_a
    clamped = []
    for t, p in knots:
        level = min(pos_b, max(level, p))
        clamped.append((t, level))
    knots = clamped

    points = [{"stop": stops[ia], "pos": pos_a, "estimated": False, "mark": marks[0]}]
    lo = ia
    for mark in inner:
        pos = _interp(knots, mark["utc"])
        best = lo
        for j in range(lo + 1, ib + 1):
            if abs(stops[j]["pos"] - pos) < abs(stops[best]["pos"] - pos):
                best = j
        lo = best
        points.append({"stop": stops[best], "pos": pos, "estimated": True, "mark": mark})
    points.append({"stop": stops[ib], "pos": pos_b, "estimated": False, "mark": end_mark})
    return {"points": points, "continues": continues}


def _manual_segments(trip: dict, window: dict, route: dict | None, resolved: dict) -> list[dict]:
    points = resolved["points"]
    if resolved["continues"] and len(points) == 2 and points[0]["stop"]["id"] == points[1]["stop"]["id"]:
        return []
    scale = manual_svc._distance_scale(route)
    route_name = trip.get("route_name") or f"Маршрут {trip['route_id']}"

    def stop_info(p: dict) -> dict:
        utc = p["mark"]["utc"]
        return {
            "id": p["stop"]["id"],
            "name": p["stop"]["name"],
            "order": p["stop"].get("order", 0),
            "arrival_utc": utc,
            "departure_utc": utc,
            "estimated": p["estimated"],
        }

    segs = []
    for a, b in zip(points, points[1:]):
        segs.append({
            "seg_key": f"m:{a['mark']['uid']}:{b['mark']['uid']}",
            "route_id": trip["route_id"],
            "route_name": route_name,
            "from_stop": stop_info(a),
            "to_stop": stop_info(b),
            "period_start_utc": a["mark"]["utc"],
            "period_end_utc": b["mark"]["utc"],
            "count": a["mark"].get("count_after"),
            "distance_km": round(max(0.0, (b["pos"] - a["pos"]) * scale), 2),
            "source": "manual",
            "window_uid": window["uid"],
        })
    return segs


def _trips_by_window(chronology: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for trip in chronology:
        out.setdefault(trip["window_uid"], []).append(trip)
    for trips in out.values():
        trips.sort(key=lambda t: (not t.get("from_left"), t["marks"][0]["utc"]))
    return out


def apply_chronology(data: dict, chronology: list[dict]) -> dict:
    """Записує ходки вікон у data і перебудовує segments / route_periods."""
    routes = {r["id"]: r for r in data.get("routes_catalog") or []}
    for trip in chronology:
        route = routes.get(trip["route_id"])
        if route is not None:
            trip["route_name"] = route["name"]
    by_window = _trips_by_window(chronology)

    items = [(b["start_utc"], 1, "block", b) for b in data.get("auto_blocks") or []]
    items += [(w["start_utc"], 0, "window", w) for w in data.get("windows") or []]
    items.sort(key=lambda x: (x[0], x[1]))
    autos_by_block: dict[str, list[dict]] = {}
    for seg in data.get("auto_segments") or []:
        autos_by_block.setdefault(seg["block_uid"], []).append(seg)

    trip_routes: list[tuple[int, str]] = []
    trip_segs: list[list[dict]] = []
    block_trip: dict[str, int] = {}
    carry: tuple[int, str] | None = None

    def new_trip(route_id: int, route_name: str) -> int:
        trip_routes.append((route_id, route_name))
        trip_segs.append([])
        return len(trip_routes) - 1

    for _start, _order, kind, item in items:
        if kind == "block":
            continued = carry is not None and carry[1] == item["uid"]
            tid = carry[0] if continued else new_trip(item["route_id"], item["route_name"])
            carry = None
            block_trip[item["uid"]] = tid
            for seg in autos_by_block.get(item["uid"], []):
                if continued and seg.get("assumed_start"):
                    continue
                trip_segs[tid].append(dict(seg))
            continue
        window = item
        trips = [t for t in by_window.get(window["uid"], []) if not t.get("ended_on_track")]
        carry = None
        for i, trip in enumerate(trips):
            route = routes.get(trip["route_id"])
            resolved = resolve_trip(trip, window, route, i == len(trips) - 1)
            if resolved is None:
                continue
            left = window.get("left")
            if trip.get("from_left") and left and left["block_uid"] in block_trip:
                tid = block_trip[left["block_uid"]]
            else:
                tid = new_trip(trip["route_id"], trip.get("route_name") or "")
            trip_segs[tid].extend(_manual_segments(trip, window, route, resolved))
            if resolved["continues"]:
                carry = (tid, window["right"]["block_uid"])

    order = sorted(
        (tid for tid in range(len(trip_segs)) if trip_segs[tid]),
        key=lambda tid: min(s["period_start_utc"] for s in trip_segs[tid]),
    )
    occurrences: dict[int, int] = {}
    segments: list[dict] = []
    periods: list[dict] = []
    for number, tid in enumerate(order, start=1):
        route_id, route_name = trip_routes[tid]
        occurrence = occurrences.get(route_id, 0) + 1
        occurrences[route_id] = occurrence
        trip_uid = f"h{number}"
        segs = sorted(trip_segs[tid], key=lambda s: s["period_start_utc"])
        for seg in segs:
            seg["occurrence"] = occurrence
            seg["trip_uid"] = trip_uid
        segments.extend(segs)
        periods.append({
            "route_id": route_id,
            "route_name": route_name or segs[0].get("route_name") or f"Маршрут {route_id}",
            "start_utc": segs[0]["period_start_utc"],
            "end_utc": max(s["period_end_utc"] for s in segs),
            "occurrence": occurrence,
            "trip_uid": trip_uid,
        })

    segments.sort(key=lambda s: s["period_start_utc"])
    data["chronology"] = chronology
    data["segments"] = segments
    data["route_periods"] = periods
    return data


def apply_counts(data: dict, counts) -> None:
    """count авто-відрізків з браузера: {seg_key: int|null}."""
    if not isinstance(counts, dict):
        return
    by_key = {s["seg_key"]: s for s in data.get("auto_segments") or []}
    for key, raw in counts.items():
        seg = by_key.get(str(key))
        if seg is not None:
            seg["count"] = manual_svc.parse_count(raw)


def counts_exceed_max(counts, chronology, max_people: int) -> bool:
    if isinstance(counts, dict):
        for raw in counts.values():
            n = manual_svc.parse_count(raw)
            if n is not None and n > max_people:
                return True
    if isinstance(chronology, list):
        for trip in chronology:
            for m in (trip.get("marks") or []) if isinstance(trip, dict) else []:
                n = manual_svc.parse_count(m.get("count_after")) if isinstance(m, dict) else None
                if n is not None and n > max_people:
                    return True
    return False


# ── перевірка перед завершенням ──────────────────────────────────────

def _stop_name(route: dict | None, stop_id) -> str:
    for s in (route or {}).get("stops") or []:
        if s["id"] == stop_id:
            return s["name"]
    return f"Зупинка #{stop_id}"


def right_mismatch_message(trip: dict, window: dict, route: dict | None, routes: dict) -> str:
    """Чому незавершена ходка не переходить у проїзд після вікна."""
    right = window["right"]
    right_route = routes.get(right["route_id"])
    name = (route or {}).get("name") or trip.get("route_name") or f"Маршрут {trip['route_id']}"
    after = (f"після ділянки машина їде маршрутом «{(right_route or {}).get('name') or right['route_id']}» "
             f"від зупинки «{_stop_name(right_route, right['stop_id'])}» о {_hm(right['utc'])}")
    if right["route_id"] != trip["route_id"]:
        return (f"Ходка «{name}» не завершена, а {after}. Позначте кінець ходки "
                "(і початок наступної, якщо вона була).")
    if not right["open"]:
        return (f"Ходка «{name}» не завершена, а {after} — це початок маршруту. "
                "Позначте кінець ходки.")
    return (f"Ходка «{name}» не завершена, а {after} — це раніше за маршрутом, ніж початок ходки. "
            "Позначте кінець ходки і розворот.")


def validate_for_ready(data: dict | None, max_people: int) -> list[dict]:
    """Помилки, що не дають завершити підрахунок:
    [{message, seg_key?, window_uid?, trip_uid?, mark_uid?}]."""
    data = data or {}
    routes = {r["id"]: r for r in data.get("routes_catalog") or []}
    errors: list[dict] = []

    def err(message: str, *, seg=None, window=None, trip=None, mark=None) -> None:
        errors.append({
            "message": message,
            "seg_key": seg["seg_key"] if seg else None,
            "window_uid": window["uid"] if window else None,
            "trip_uid": trip["uid"] if trip else None,
            "mark_uid": mark["uid"] if mark else None,
        })

    by_window = _trips_by_window(data.get("chronology") or [])
    for window in data.get("windows") or []:
        trips = [t for t in by_window.get(window["uid"], []) if not t.get("ended_on_track")]
        span = f"{_hm(window['start_utc'])}–{_hm(window['end_utc'])}"
        engaged = any(not _is_bare_left(t) for t in trips)
        for i, trip in enumerate(trips):
            route = routes.get(trip["route_id"])
            name = trip.get("route_name") or f"Маршрут {trip['route_id']}"
            marks = trip["marks"]
            is_last = i == len(trips) - 1
            if route is None or route.get("unbound"):
                err(f"Ділянка {span}: маршрут «{name}» більше не прив'язаний до машини — "
                    "змініть маршрут ходки або видаліть її.", window=window, trip=trip, mark=marks[0])
                continue
            for k, mark in enumerate(marks):
                if mark.get("anchor"):
                    continue
                if not window["start_utc"] < mark["utc"] < window["end_utc"]:
                    err(f"Ділянка {span}: відмітка о {_hms(mark['utc'])} поза ділянкою ручної розмітки.",
                        window=window, trip=trip, mark=mark)
                elif k > 0 and mark["utc"] <= marks[k - 1]["utc"]:
                    err(f"Ходка «{name}»: відмітка о {_hms(mark['utc'])} має бути пізніше попередньої.",
                        window=window, trip=trip, mark=mark)
            stops = {s["id"]: s for s in manual_svc.route_stop_positions(route)}
            start = stops.get(trip.get("start_stop_id"))
            if start is None:
                err(f"Ходка «{name}»: вкажіть початкову зупинку маршруту.", window=window, trip=trip, mark=marks[0])
            if trip.get("closed"):
                end = stops.get(trip.get("end_stop_id"))
                if end is None:
                    err(f"Ходка «{name}»: вкажіть кінцеву зупинку маршруту.", window=window, trip=trip, mark=marks[-1])
                elif start is not None and end["pos"] <= start["pos"]:
                    err(f"Ходка «{name}»: кінцева зупинка «{end['name']}» має бути далі за маршрутом, "
                        f"ніж початкова «{start['name']}».", window=window, trip=trip, mark=marks[-1])
                if len(marks) < 2:
                    err(f"Ходка «{name}»: потрібні щонайменше початок і кінець.", window=window, trip=trip)
                continue
            if continues_into_right(trip, window, route, is_last):
                continue
            if _is_bare_left(trip) and not (window.get("right") and engaged):
                continue
            elif not is_last:
                err(f"Ходка «{name}» не завершена — позначте її кінець перед наступною ходкою.",
                    window=window, trip=trip, mark=marks[-1])
            elif window.get("right"):
                err(right_mismatch_message(trip, window, route, routes), window=window, trip=trip, mark=marks[-1])
            else:
                err(f"Ходка «{name}» не завершена: після ділянки {span} треку немає — "
                    "вкажіть кінцеву зупинку.", window=window, trip=trip, mark=marks[-1])
        right = window.get("right")
        if engaged and right and right["open"] and (not trips or trips[-1].get("closed")):
            right_route = routes.get(right["route_id"])
            err(f"Ділянка {span}: після неї машина вже їде маршрутом «{(right_route or {}).get('name') or right['route_id']}» "
                f"(зупинка «{_stop_name(right_route, right['stop_id'])}» о {_hm(right['utc'])}). "
                "Останню ходку ділянки почніть цим маршрутом і не завершуйте — вона продовжиться за треком.",
                window=window, trip=trips[-1] if trips else None)
        for prev, trip in zip(trips, trips[1:]):
            if trip["marks"][0]["utc"] < prev["marks"][-1]["utc"]:
                err(f"Ходка «{trip.get('route_name')}» починається раніше, ніж закінчилась попередня.",
                    window=window, trip=trip)

    for seg in data.get("segments") or []:
        count = seg.get("count")
        label = (f"«{(seg.get('from_stop') or {}).get('name')} – {(seg.get('to_stop') or {}).get('name')}» "
                 f"о {_hm(seg['period_start_utc'])}")
        if count is None:
            err(f"Відрізок {label}: вкажіть кількість людей.", seg=seg)
        elif count > max_people:
            err(f"Відрізок {label}: більше людей, ніж максимум ({max_people}).", seg=seg)
    return errors


# ── пересбор ─────────────────────────────────────────────────────────

def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def merge_rebuild(old: dict | None, new: dict, time_tol: int) -> dict:
    """Переносить введене зі старої data в нову; ручні відмітки, на місце яких
    прийшов трек, видаляються (перелік — у warnings)."""
    if not is_hybrid(old):
        return apply_chronology(new, sanitize_chronology([], new["windows"], new["routes_catalog"]))

    old_auto = {s["seg_key"]: s for s in old.get("auto_segments") or []}
    for seg in new["auto_segments"]:
        prev = old_auto.get(seg["seg_key"])
        if (prev and prev.get("count") is not None
                and abs(int(prev["period_start_utc"]) - seg["period_start_utc"]) <= time_tol
                and abs(int(prev["period_end_utc"]) - seg["period_end_utc"]) <= time_tol):
            seg["count"] = prev["count"]

    old_windows = {w["uid"]: w for w in old.get("windows") or []}
    new_windows = {w["uid"]: w for w in new["windows"]}
    removed: list[dict] = []
    kept: list[dict] = []
    for trip in old.get("chronology") or []:
        target = new_windows.get(trip.get("window_uid"))
        old_w = old_windows.get(trip.get("window_uid"))
        if target is None and old_w is not None:
            best = max(
                new["windows"],
                key=lambda w: _overlap(w["start_utc"], w["end_utc"], old_w["start_utc"], old_w["end_utc"]),
                default=None,
            )
            if best and _overlap(best["start_utc"], best["end_utc"], old_w["start_utc"], old_w["end_utc"]):
                target = best
        marks = trip.get("marks") or []
        if target is None:
            removed.extend(m for m in marks if not m.get("anchor"))
            continue
        if trip.get("from_left"):
            old_left = (old_w or {}).get("left") or {}
            new_left = target.get("left") or {}
            if (not _wants_left_trip(target) or old_left.get("stop_id") != new_left.get("stop_id")
                    or old_left.get("utc") != new_left.get("utc")):
                removed.extend(m for m in marks if not m.get("anchor"))
                continue
        inside = [m for m in marks if m.get("anchor") or target["start_utc"] < m["utc"] < target["end_utc"]]
        removed.extend(m for m in marks if m not in inside)
        if not [m for m in inside if not m.get("anchor")] and not trip.get("from_left"):
            continue
        trip = {**trip, "window_uid": target["uid"], "marks": inside}
        if trip.get("closed") and marks and marks[-1] not in inside:
            trip["closed"] = False
            trip["end_stop_id"] = None
        kept.append(trip)

    chronology = sanitize_chronology(kept, new["windows"], new["routes_catalog"])
    if removed:
        removed.sort(key=lambda m: m["utc"])
        listed = ", ".join(
            f"{_hm(m['utc'])} ({m['count_after'] if m.get('count_after') is not None else '—'})"
            for m in removed
        )
        new.setdefault("warnings", []).append(
            f"Трек з'явився там, де були ручні відмітки, — їх видалено (час і кількість людей): {listed}"
        )
    return apply_chronology(new, chronology)
