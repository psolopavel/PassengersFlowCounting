"""Выбор пути через разрыв GPS по контексту маршрутов — порт GapContextResolver.php.

Для разрыва берём проезд ДО (маршрут A, позиция a) и проезд ПОСЛЕ
(маршрут B, позиция b) и перебираем варианты пути:
  - прямо: A == B и b > a (кусок одного маршрута);
  - через n разворотов (turns): доехать A до конца → обратный маршрут
    (inverse) → ... → после n-го разворота маршрут цепочки должен совпасть
    с B, доехать до b.
Время варианта = сумма участков по средней скорости маршрута
(total_len / avg_route_pass_time). Подъезды от точек разрыва вне маршрута —
прямыми.

Выбор: вариант, у которого отношение «нужное время / длина разрыва» ближе
всего к 1, при условии отношение <= 1 + SPEED_TOLERANCE. Лишнее время —
стоянка в начале разрыва. Минимальный вариант (без лишних оборотов)
берётся, если разрыв в основном ночной или у маршрутов цепочки нет времени
проезда.

Если проезда нет с одной стороны (начало/конец дня), маршрут этой стороны
перебирается из пары известного (сам маршрут или его inverse). Если нет
проездов с обеих сторон или у маршрутов нет ни inverse, ни времени —
план не строится, и gap_fill_service работает старой логикой.

Маршрут: {'id','points','segment_lengths','cum_along','total_len',
'inverse_idx','inverse_entry_along','pass_time_s','service_zones'}
(см. route_match_service.load_car_routes / link_inverse_routes).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.route_match_service import detect_route_periods_in_ranges
from app.utils.debug_log import debug_log
from app.utils.geo import haversine, point_at_distance_along, project_point_on_polyline

try:
    from zoneinfo import ZoneInfo

    _LOCAL_TZ = ZoneInfo("Europe/Kiev")
except Exception:  # noqa: BLE001 — нет базы часовых поясов
    _LOCAL_TZ = timezone(timedelta(hours=3))

# Разрывы короче этого не заполняются (как MIN_GAP_SECONDS в gap_fill_service).
MIN_GAP_SECONDS = 60
# Насколько вариант может быть быстрее средней скорости маршрутов.
SPEED_TOLERANCE = 0.15
# Скорость для маршрутов без avg_route_pass_time (км/ч).
ASSUMED_SPEED_KMH = 30
# Проезд считается контекстом разрыва, если закончился (начался) не дальше
# этого по времени и расстоянию от границы разрыва.
CONTEXT_MAX_SEC = 3600
CONTEXT_MAX_M = 5000
# Неизвестная сторона: точка разрыва должна быть не дальше этого от маршрута.
UNKNOWN_SIDE_MAX_M = 3000
# Позиция после разрыва может быть чуть раньше точки входа на маршрут.
ENTRY_TOLERANCE_M = 300
# Подъезд короче этого не рисуем отдельной прямой.
MIN_LINE_M = 5
MAX_TURNS = 30
# Ночь: в эти часы (местное время) обороты не добавляются.
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 5
NIGHT_MAJORITY = 0.5
NIGHT_SAMPLE_SEC = 300


def _log(message: str, data: dict) -> None:
    debug_log(message, tag="gap_fill", data=data)


def _round_half_up(x: float, ndigits: int = 0) -> float:
    """Округление как в PHP round() (0.5 — от нуля), для одинаковых логов."""
    m = 10 ** ndigits
    return (int(abs(x) * m + 0.5) / m) * (1 if x >= 0 else -1)


class GapContextResolver:
    def __init__(self, routes: list[dict], periods: list[dict]):
        self.routes = routes
        self.periods = periods

    def resolve(self, track: dict, gap: dict, gap_idx: int) -> dict | None:
        """План для разрыва или None (тогда — старая логика).

        План: {'type': 'context', 'route', 'legs', 'leg_times', 'need_s',
        'ratio', 'turns', 'route_seq', 'idle_s', 'speed_factor'}.
        """
        before = self._period_before(track, gap)
        after = self._period_after(track, gap)
        if before is None and after is None:
            _log("no context periods", {"gap": gap_idx})
            return None

        starts = self._start_states(gap, before, after)
        ends = self._end_states(gap, before, after)
        if not starts or not ends:
            _log("no start/end states", {"gap": gap_idx, "starts": len(starts), "ends": len(ends)})
            return None
        if not self._has_route_params(starts, ends):
            _log("routes have no inverse/time, fallback", {"gap": gap_idx})
            return None

        dt = int(gap["dt"])
        limit = dt * (1 + SPEED_TOLERANCE)
        night = _is_night_gap(gap)

        all_hyps: list[dict] = []
        picks: list[dict] = []
        for s in starts:
            for e in ends:
                hyps = self._build_hypotheses(gap, s, e)
                all_hyps.extend(hyps)
                feasible = [h for h in hyps if h["need_s"] <= limit]
                pick = _pick_for_combo(feasible, night)
                if pick is not None:
                    picks.append(pick)

        _log("context", {
            "gap": gap_idx,
            "before": (
                f"{before['route_id']}@{int(_round_half_up(before['along_end']))}"
                if before is not None else "none"
            ),
            "after": (
                f"{after['route_id']}@{int(_round_half_up(after['along_start']))}"
                if after is not None else "none"
            ),
            "dt_s": dt,
            "night": night,
            "hypotheses": _describe_hypotheses(all_hyps),
        })

        best = _pick_across_combos(picks, night)
        if best is None:
            _log("no feasible hypothesis, fallback", {"gap": gap_idx, "limit_s": round(limit)})
            return None

        plan = _make_plan(gap, best)
        _log("picked", {
            "gap": gap_idx,
            "turns": plan["turns"],
            "route_seq": plan["route_seq"],
            "need_s": round(plan["need_s"]),
            "ratio": round(plan["ratio"], 2),
            "idle_s": round(plan["idle_s"]),
            "speed_factor": round(plan["speed_factor"], 2),
            "timed": best["timed"],
        })
        return plan

    # ── контекст: проезды до и после ────────────────────────────────

    def _period_before(self, track: dict, gap: dict) -> dict | None:
        found = None
        for p in self.periods:
            if p["end_index"] <= gap["start_index"]:
                found = p
        if found is None:
            return None
        idx = found["end_index"]
        dt_sec = int(gap["time_before"]) - found["end_utc"]
        dist = haversine(track["lat"][idx], track["lon"][idx], gap["lat_before"], gap["lon_before"])
        if dt_sec > CONTEXT_MAX_SEC or dist > CONTEXT_MAX_M:
            return None
        return found

    def _period_after(self, track: dict, gap: dict) -> dict | None:
        for p in self.periods:
            if p["start_index"] < gap["end_index"]:
                continue
            idx = p["start_index"]
            dt_sec = p["start_utc"] - int(gap["time_after"])
            dist = haversine(track["lat"][idx], track["lon"][idx], gap["lat_after"], gap["lon_after"])
            if dt_sec > CONTEXT_MAX_SEC or dist > CONTEXT_MAX_M:
                return None
            return p
        return None

    def _start_states(self, gap: dict, before: dict | None, after: dict | None) -> list[dict]:
        if before is not None:
            s = self._state_at(
                before["route_idx"], before["along_end"], gap["lat_before"], gap["lon_before"], True
            )
            return [s] if s is not None else []
        return self._unknown_side_states(after["route_idx"], gap["lat_before"], gap["lon_before"])

    def _end_states(self, gap: dict, before: dict | None, after: dict | None) -> list[dict]:
        if after is not None:
            s = self._state_at(
                after["route_idx"], after["along_start"], gap["lat_after"], gap["lon_after"], True
            )
            return [s] if s is not None else []
        return self._unknown_side_states(before["route_idx"], gap["lat_after"], gap["lon_after"])

    def _unknown_side_states(self, known_idx: int, lat: float, lon: float) -> list[dict]:
        """Неизвестная сторона: известный маршрут и его inverse, позиция —
        проекция точки разрыва на маршрут."""
        cands = [known_idx]
        inv = self.routes[known_idx].get("inverse_idx")
        if inv is not None and inv != known_idx:
            cands.append(inv)
        states: list[dict] = []
        for r_idx in cands:
            route = self.routes[r_idx]
            proj = project_point_on_polyline(lat, lon, route["points"], route["segment_lengths"])
            if proj is None or proj["distance"] > UNKNOWN_SIDE_MAX_M:
                continue
            s = self._state_at(r_idx, proj["along"], lat, lon, False)
            if s is not None:
                states.append(s)
        return states

    def _state_at(self, r_idx: int, along: float, gap_lat: float, gap_lon: float, known: bool) -> dict | None:
        route = self.routes[r_idx]
        pt = point_at_distance_along(route["points"], route["segment_lengths"], along)
        if pt is None:
            return None
        return {
            "route_idx": r_idx,
            "along": along,
            "lat": pt["lat"],
            "lon": pt["lon"],
            "gap_lat": gap_lat,
            "gap_lon": gap_lon,
            "approach_m": haversine(gap_lat, gap_lon, pt["lat"], pt["lon"]),
            "known": known,
        }

    def _has_route_params(self, starts: list[dict], ends: list[dict]) -> bool:
        for s in starts + ends:
            r = self.routes[s["route_idx"]]
            if r.get("inverse_idx") is not None or r.get("pass_time_s") is not None:
                return True
        return False

    # ── варианты пути ───────────────────────────────────────────────

    def _build_hypotheses(self, gap: dict, s: dict, e: dict) -> list[dict]:
        hyps: list[dict] = []
        a = s["route_idx"]
        b = e["route_idx"]
        dt = max(int(gap["dt"]), 1)
        limit = dt * (1 + SPEED_TOLERANCE)

        if a == b and e["along"] > s["along"]:
            legs = [self._route_leg(a, s["along"], e["along"])]
            hyps.append(self._make_hypothesis(s, e, legs, 0, dt))

        chain: list[dict] = []
        len_a = self.routes[a]["total_len"]
        if len_a - s["along"] > 1:
            chain.append(self._route_leg(a, s["along"], len_a))
        cur = a
        for n in range(1, MAX_TURNS + 1):
            inv = self.routes[cur].get("inverse_idx")
            if inv is None:
                break
            entry = self.routes[cur]["inverse_entry_along"]
            if inv == b and e["along"] >= entry - ENTRY_TOLERANCE_M:
                legs = list(chain)
                if e["along"] - entry > 1:
                    legs.append(self._route_leg(inv, entry, e["along"]))
                h = self._make_hypothesis(s, e, legs, n, dt)
                hyps.append(h)
                if h["need_s"] > limit:
                    break
            chain.append(self._route_leg(inv, entry, self.routes[inv]["total_len"]))
            cur = inv
            if self._legs_time(chain) > limit:
                break
        return hyps

    def _route_leg(self, r_idx: int, along_from: float, along_to: float) -> dict:
        return {
            "type": "route",
            "route_idx": r_idx,
            "route": self.routes[r_idx],
            "along_from": along_from,
            "along_to": along_to,
        }

    def _make_hypothesis(self, s: dict, e: dict, route_legs: list[dict], turns: int, dt: int) -> dict:
        """Вариант: участки маршрутов + подъезды прямыми от/до точек разрыва."""
        legs: list[dict] = []
        times: list[float] = []
        speed_start = self._route_speed(s["route_idx"])
        speed_end = self._route_speed(e["route_idx"])

        if s["approach_m"] > MIN_LINE_M:
            legs.append({
                "type": "line",
                "from": (s["gap_lat"], s["gap_lon"]),
                "to": (s["lat"], s["lon"]),
            })
            times.append(s["approach_m"] / speed_start)
        timed = True
        seq: list[int] = []
        for leg in route_legs:
            legs.append(leg)
            times.append((leg["along_to"] - leg["along_from"]) / self._route_speed(leg["route_idx"]))
            route = self.routes[leg["route_idx"]]
            if route.get("pass_time_s") is None:
                timed = False
            if not seq or seq[-1] != route["id"]:
                seq.append(route["id"])
        if e["approach_m"] > MIN_LINE_M:
            legs.append({
                "type": "line",
                "from": (e["lat"], e["lon"]),
                "to": (e["gap_lat"], e["gap_lon"]),
            })
            times.append(e["approach_m"] / speed_end)

        need = sum(times)
        return {
            "legs": legs,
            "leg_times": times,
            "need_s": need,
            "ratio": need / dt,
            "turns": turns,
            "timed": timed,
            "route_seq": seq,
            "approach_m": s["approach_m"] + e["approach_m"],
        }

    def _route_speed(self, r_idx: int) -> float:
        """Средняя скорость маршрута (м/с)."""
        r = self.routes[r_idx]
        pass_s = r.get("pass_time_s")
        if pass_s is not None and pass_s > 0:
            return r["total_len"] / pass_s
        return ASSUMED_SPEED_KMH / 3.6

    def _legs_time(self, route_legs: list[dict]) -> float:
        return sum(
            (leg["along_to"] - leg["along_from"]) / self._route_speed(leg["route_idx"])
            for leg in route_legs
        )


# ── выбор ───────────────────────────────────────────────────────────

def _pick_for_combo(feasible: list[dict], night: bool) -> dict | None:
    """Лучший вариант для одной пары (старт, конец) среди допустимых."""
    if not feasible:
        return None
    minimal = None
    for h in feasible:
        if minimal is None or h["turns"] < minimal["turns"]:
            minimal = h
    if night or not minimal["timed"]:
        return minimal
    best = None
    best_score = None
    for h in feasible:
        if not h["timed"]:
            continue
        score = abs(h["ratio"] - 1)
        if best_score is None or score < best_score:
            best = h
            best_score = score
    return best if best is not None else minimal


def _pick_across_combos(picks: list[dict], night: bool) -> dict | None:
    best = None
    for h in picks:
        if best is None:
            best = h
            continue
        if night:
            better = h["turns"] < best["turns"] or (
                h["turns"] == best["turns"] and h["approach_m"] < best["approach_m"]
            )
        else:
            hs = abs(h["ratio"] - 1)
            bs = abs(best["ratio"] - 1)
            better = hs < bs - 1e-9 or (
                abs(hs - bs) <= 1e-9 and h["approach_m"] < best["approach_m"]
            )
        if better:
            best = h
    return best


def _make_plan(gap: dict, h: dict) -> dict:
    dt = max(int(gap["dt"]), 1)
    need = h["need_s"]
    first_route = next((leg["route"] for leg in h["legs"] if leg["type"] == "route"), None)
    return {
        "type": "context",
        "route": first_route,
        "legs": h["legs"],
        "leg_times": h["leg_times"],
        "need_s": need,
        "ratio": need / dt,
        "turns": h["turns"],
        "route_seq": h["route_seq"],
        "idle_s": dt - need if need < dt else 0.0,
        "speed_factor": need / dt if need > dt else 1.0,
    }


def _is_night_gap(gap: dict) -> bool:
    """Разрыв ночной, если больше половины его времени приходится на
    NIGHT_START_HOUR..NIGHT_END_HOUR местного времени."""
    t_from = int(gap["time_before"])
    t_to = int(gap["time_after"])
    if t_to <= t_from:
        return False
    total = 0
    night = 0
    for t in range(t_from, t_to, NIGHT_SAMPLE_SEC):
        hour = datetime.fromtimestamp(t, tz=_LOCAL_TZ).hour
        total += 1
        if hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR:
            night += 1
    return total > 0 and (night / total) > NIGHT_MAJORITY


def _describe_hypotheses(hyps: list[dict]) -> list[str]:
    """Краткое описание вариантов для лога: «488>487>488>487:turns4:136min:r1»."""
    out = []
    for h in hyps:
        ratio = _round_half_up(h["ratio"], 2)
        ratio_s = f"{ratio:g}"
        out.append(
            ">".join(str(x) for x in h["route_seq"])
            + f":turns{h['turns']}"
            + f":{int(_round_half_up(h['need_s'] / 60))}min"
            + f":r{ratio_s}"
            + ("" if h["timed"] else ":untimed")
        )
    return out


# ── точка входа для gap_fill_service ────────────────────────────────

def build_context_ranges(track: dict, gaps: list[dict]) -> list[tuple[int, int]]:
    """Куски трека между разрывами (включительно).

    Рвём только по разрывам, которые будут заполняться (dt >= MIN_GAP_SECONDS):
    короткие скачки GPS-дребезга проезд не разрывают. Зависшие точки внутри
    разрыва в куски не попадают.
    """
    n = len(track["lat"])
    ranges: list[tuple[int, int]] = []
    start = 0
    for gap in gaps:
        if int(gap["dt"]) < MIN_GAP_SECONDS:
            continue
        if gap["start_index"] >= start:
            ranges.append((start, gap["start_index"]))
        start = gap["end_index"]
    if start <= n - 1:
        ranges.append((start, n - 1))
    return ranges


def resolve_gaps_by_context(track: dict, gaps: list[dict], routes: list[dict]) -> dict[int, dict]:
    """Проезды по реальным точкам → план для каждого разрыва.

    Возвращает {gap_idx: план} только для решённых разрывов.
    """
    ranges = build_context_ranges(track, gaps)
    periods = detect_route_periods_in_ranges(track, routes, ranges)
    resolver = GapContextResolver(routes, periods)
    plans: dict[int, dict] = {}
    for gap_idx, gap in enumerate(gaps):
        if int(gap["dt"]) < MIN_GAP_SECONDS:
            continue
        plan = resolver.resolve(track, gap, gap_idx)
        if plan is not None:
            plans[gap_idx] = plan
    _log("context pass done", {"gaps": len(gaps), "periods": len(periods), "plans": len(plans)})
    return plans
