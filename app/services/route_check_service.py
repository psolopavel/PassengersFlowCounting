"""Перевірка маршруту ручної ходки за GPS-треком.

Точки треку за час ходки (від першої до кінцевої відмітки) порівнюються з лінією
вибраного рахувальником маршруту і з лініями інших прив'язаних маршрутів: яка
частка точок лежить поруч із лінією, наскільки близько і чи рухається машина по
маршруту вперед. Розриви треку не заважають — порівнюються наявні точки, а
переїзд через розрив теж показує напрям.

Підказка — лише коли інший маршрут явно кращий: поруч із ним значно більше точок
або вони значно ближче, або по вибраному маршруту машина їхала назад.
"""

from __future__ import annotations

import math
import statistics

from sqlalchemy.orm import Session

from app.services import track_service
from app.services.route_match_service import _load_routes_geo, _load_stops
from app.utils.geo import haversine

# Точка «на маршруті», якщо до його лінії не далі (м).
NEAR_M = 300
# Комірка сітки відрізків лінії (м); має бути більшою за NEAR_M + TIE_M.
GRID_M = 500
# Майже рівні відстані до двох ділянок лінії (м) — береться та, що попереду.
TIE_M = 30.0
# Сусідні точки ближче за це (стоянка, дрібний крок) не рахуються — інакше
# стоянка на кінцевій переважила б саму поїздку.
MIN_STEP_M = 150
MAX_POINTS = 400
# Менше точок у русі — висновків не робимо.
MIN_POINTS = 3
# Напрям визначається, якщо вздовж маршруту проїхали хоча б стільки (м).
MIN_MOVE_M = 300
# (вперед − назад) / весь рух уздовж маршруту: ≥ FORWARD — вперед, ≤ −FORWARD — назад.
FORWARD = 0.5
# Маршрут «підходить», якщо поруч із ним щонайменше така частка точок.
GOOD_SHARE = 0.8
# Вибраний маршрут «не схожий», якщо поруч із ним менше такої частки.
BAD_SHARE = 0.5
# Інший маршрут значно кращий: частка точок поруч більша на стільки…
SHARE_MARGIN = 0.25
# …або типова відстань до лінії в CLOSER_RATIO разів і на CLOSER_MIN_M менша.
CLOSER_RATIO = 3.0
CLOSER_MIN_M = 150
# Кандидати з частками в межах цього — рівноцінні (пропонуємо обидва).
TIE_SHARE = 0.05
MAX_SUGGESTIONS = 2


def _moving_points(track: dict) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for lat, lon in zip(track.get("lat") or [], track.get("lon") or []):
        if out and haversine(out[-1][0], out[-1][1], lat, lon) < MIN_STEP_M:
            continue
        out.append((lat, lon))
    if len(out) > MAX_POINTS:
        step = len(out) / MAX_POINTS
        out = [out[int(i * step)] for i in range(MAX_POINTS)]
    return out


def _segment_distance(x, y, x1, y1, x2, y2) -> tuple[float, float]:
    dx, dy = x2 - x1, y2 - y1
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 <= 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / seg2))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)), t


class _LineIndex:
    """Лінія маршруту в локальних метрах і сітка її відрізків для пошуку поруч із точкою."""

    def __init__(self, route: dict):
        pts = route["points"]
        lat0 = sum(p[0] for p in pts) / len(pts)
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 111320.0
        self.xy = [self._xy(lat, lon) for lat, lon in pts]
        self.lens = route["segment_lengths"]
        self.cum = route["cum_along"]
        self.grid: dict[tuple[int, int], list[int]] = {}
        for i in range(len(self.xy) - 1):
            (x1, y1), (x2, y2) = self.xy[i], self.xy[i + 1]
            for cx in range(math.floor(min(x1, x2) / GRID_M), math.floor(max(x1, x2) / GRID_M) + 1):
                for cy in range(math.floor(min(y1, y2) / GRID_M), math.floor(max(y1, y2) / GRID_M) + 1):
                    self.grid.setdefault((cx, cy), []).append(i)
        self.coarse = self.xy[::max(1, len(self.xy) // 200)] + [self.xy[-1]]

    def _xy(self, lat: float, lon: float) -> tuple[float, float]:
        return lon * self.kx, lat * self.ky

    def project(self, lat: float, lon: float, prefer_from: float | None) -> tuple[float, float | None]:
        """(відстань до лінії, along) — along лише якщо лінія ближче за GRID_M.

        Далі за GRID_M відстань наближена (до вершин лінії через одну-кілька).
        prefer_from — якщо найближча ділянка позаду, а та сама дорога є і
        попереду як інший прохід лінії («туди-назад»), береться копія попереду,
        щоб повернення тією ж дорогою не рахувалось рухом назад.
        """
        x, y = self._xy(lat, lon)
        cx, cy = math.floor(x / GRID_M), math.floor(y / GRID_M)
        seg_ids: set[int] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                seg_ids.update(self.grid.get((cx + dx, cy + dy), ()))
        cands = []
        for i in seg_ids:
            (x1, y1), (x2, y2) = self.xy[i], self.xy[i + 1]
            d, t = _segment_distance(x, y, x1, y1, x2, y2)
            cands.append((d, i, self.cum[i] + self.lens[i] * t))
        if cands:
            d, _i, along = min(cands, key=lambda c: (c[0], c[1]))
            if d <= GRID_M:
                if prefer_from is not None and along < prefer_from:
                    copies = [
                        c for c in cands
                        if c[0] <= d + TIE_M and c[2] >= prefer_from and c[2] - along > GRID_M
                    ]
                    if copies:
                        d, _i, along = min(copies, key=lambda c: c[2] - prefer_from)
                return d, along
        far = min(math.hypot(x - px, y - py) for px, py in self.coarse)
        return max(far, float(GRID_M)), None


def _metrics(route: dict, pts: list[tuple[float, float]]) -> dict:
    """{share, median_m, direction, first_along, last_along} точок треку щодо лінії маршруту."""
    line = _LineIndex(route)
    laterals: list[float] = []
    alongs: list[float] = []
    prev = None
    for lat, lon in pts:
        dist, along = line.project(lat, lon, prev)
        laterals.append(dist)
        if along is not None and dist <= NEAR_M:
            alongs.append(along)
            prev = along
    fwd = back = 0.0
    for a, b in zip(alongs, alongs[1:]):
        if b >= a:
            fwd += b - a
        else:
            back += a - b
    moved = fwd + back
    return {
        "share": len(alongs) / len(pts),
        "median_m": statistics.median(laterals) if laterals else None,
        "direction": (fwd - back) / moved if moved >= MIN_MOVE_M else None,
        "first_along": alongs[0] if alongs else None,
        "last_along": alongs[-1] if alongs else None,
    }


def _fits(m: dict) -> bool:
    return m["share"] >= GOOD_SHARE and (m["direction"] is None or m["direction"] >= FORWARD)


def _clearly_closer(m: dict, chosen: dict) -> bool:
    if m["share"] - chosen["share"] >= SHARE_MARGIN:
        return True
    cm, mm = chosen["median_m"], m["median_m"]
    return (
        cm is not None and mm is not None
        and cm - mm >= CLOSER_MIN_M and cm >= CLOSER_RATIO * max(mm, 1.0)
    )


def _suggested_stops(route: dict, m: dict) -> tuple[int | None, int | None]:
    """Зупинки маршруту, найближчі до першої і останньої точки треку на ньому."""
    stops = route.get("stops") or []
    a, b = m["first_along"], m["last_along"]
    if not stops or a is None or b is None or b <= a:
        return None, None
    start = min(stops, key=lambda s: abs(s["along"] - a))
    end = min(stops, key=lambda s: abs(s["along"] - b))
    if end["along"] <= start["along"]:
        return None, None
    return start["id"], end["id"]


def _brief(m: dict) -> dict:
    return {
        "share": round(m["share"], 2),
        "median_m": round(m["median_m"]) if m["median_m"] is not None else None,
        "direction": round(m["direction"], 2) if m["direction"] is not None else None,
    }


def _pct(share: float) -> str:
    return f"{round(share * 100)}%"


def _dist(m: dict) -> str:
    d = m["median_m"]
    if d is None:
        return ""
    text_d = f"{round(d)} м" if d < 1000 else f"{d / 1000:.1f} км".replace(".", ",")
    return f", типово ~{text_d} від лінії"


def check_trip_route(
    gps_db: Session,
    routes_db: Session,
    imei: str,
    catalog: list[dict],
    route_id: int,
    start_utc: int,
    end_utc: int,
) -> dict:
    """-> {status, message?, chosen?, suggestions: [{route_id, route_name, start_stop_id,
    end_stop_id, share, median_m, direction}], points}.

    status: ok — маршрут підтверджується або явних підстав сумніватися немає;
    suspect — інший прив'язаний маршрут явно кращий (suggestions не порожній);
    no_match — вибраний маршрут не схожий на трек, а кращого немає;
    no_data — у треку за час ходки замало точок у русі;
    unknown — вибраний маршрут без лінії на карті або не прив'язаний.
    """
    names = {int(r["id"]): r["name"] for r in catalog if not r.get("unbound")}
    if route_id not in names:
        return {"status": "unknown", "suggestions": []}
    geo = _load_routes_geo(routes_db, list(names))
    if route_id not in geo:
        return {"status": "unknown", "suggestions": []}

    track, _table = track_service.get_day_track(gps_db, imei, start_utc, end_utc)
    pts = _moving_points(track)
    if len(pts) < MIN_POINTS:
        return {"status": "no_data", "suggestions": [], "points": len(pts)}

    metrics = {rid: _metrics(route, pts) for rid, route in geo.items()}
    chosen = metrics[route_id]
    backward = chosen["direction"] is not None and chosen["direction"] <= -FORWARD
    better = [
        rid for rid, m in metrics.items()
        if rid != route_id and _fits(m) and (backward or _clearly_closer(m, chosen))
    ]
    better.sort(key=lambda rid: (-metrics[rid]["share"], metrics[rid]["median_m"] or 0.0))
    if better:
        top = metrics[better[0]]["share"]
        better = [rid for rid in better if top - metrics[rid]["share"] <= TIE_SHARE][:MAX_SUGGESTIONS]

    suggestions = []
    for rid in better:
        _load_stops(routes_db, geo[rid])
        start_stop_id, end_stop_id = _suggested_stops(geo[rid], metrics[rid])
        suggestions.append({
            "route_id": rid,
            "route_name": names[rid],
            "start_stop_id": start_stop_id,
            "end_stop_id": end_stop_id,
            **_brief(metrics[rid]),
        })

    result = {"chosen": _brief(chosen), "suggestions": suggestions, "points": len(pts)}
    if suggestions:
        alt = " або ".join(f"«{s['route_name']}»" for s in suggestions)
        best = metrics[better[0]]
        if backward:
            message = (
                f"За GPS-треком машина їхала в протилежному напрямку до вибраного маршруту — "
                f"схоже на {alt}."
            )
        else:
            message = (
                f"За GPS-треком ходка більше схожа на {alt}: поруч із його лінією "
                f"{_pct(best['share'])} точок треку{_dist(best)}; у вибраного маршруту — "
                f"{_pct(chosen['share'])}{_dist(chosen)}."
            )
        return {**result, "status": "suspect", "message": message}
    if backward:
        return {
            **result,
            "status": "no_match",
            "message": "За GPS-треком машина їхала по вибраному маршруту в протилежному напрямку, "
                       "а відповідного маршруту серед прив'язаних немає.",
        }
    if chosen["share"] < BAD_SHARE:
        return {
            **result,
            "status": "no_match",
            "message": f"GPS-трек ходки не схожий на жоден прив'язаний маршрут: поруч із лінією "
                       f"вибраного лише {_pct(chosen['share'])} точок треку{_dist(chosen)}.",
        }
    return {**result, "status": "ok"}
