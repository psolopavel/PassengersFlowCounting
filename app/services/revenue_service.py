"""Расчёт выручки и пассажиропотока из data сессии подсчёта.

Две независимые части:

  1. Разбор пассажиров (не зависит от тарифов, замораживается при ready):
       * город    — passengers = data.result.in_salon_total;
       * межгород — FIFO-модель посадок/высадок по дельтам count между
         сегментами внутри ходки (route_id + occurrence). Возвращает список
         завершённых поездок [{route_id, from_id, to_id, pax}] и число посадок.

  2. Деньги (считаются на чтении, тариф — актуальный на count_day):
       * для каждой поездки берём цену пары остановок из tariffs.prices
         тарифа, действующего на день по истории car_route_tariffs.

FIFO-логика: при уменьшении count выходят те, кто сел раньше всех из ещё
едущих; точка высадки — from_stop сегмента, на котором обнаружено уменьшение
(человек уже не посчитан в этом сегменте). Остаток в конце ходки выходит на
последней to_stop ходки.
"""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CarRouteTariff, Tariff


# ── общие утилиты ────────────────────────────────────────────────────

def _parse_json(raw):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _seg_count(seg: dict, warnings: list[str]) -> int:
    """count сегмента как int. null → 0 + warning (по Q3 не должно случаться)."""
    c = seg.get("count")
    if c is None:
        warnings.append(f"segment_not_counted:{seg.get('seg_key')}")
        return 0
    try:
        return int(c)
    except (TypeError, ValueError):
        warnings.append(f"bad_count:{seg.get('seg_key')}")
        return 0


def _stop_id(seg: dict, side: str) -> int | None:
    stop = seg.get(side) or {}
    sid = stop.get("id")
    try:
        return int(sid)
    except (TypeError, ValueError):
        return None


# ── пассажиры ────────────────────────────────────────────────────────

def _run_key(seg: dict) -> tuple:
    """Ключ ходки: маршрут + номер прохождения."""
    return (seg.get("route_id"), seg.get("occurrence"))


def _compute_run(segments: list[dict]) -> tuple[list[dict], int, list[str]]:
    """FIFO по одной ходке. -> (trips, boardings, warnings)."""
    trips: list[dict] = []
    warnings: list[str] = []
    boardings = 0
    prev = 0
    route_id = segments[0].get("route_id")
    queue: list[list[int]] = []  # [[boarding_stop_id, remaining_pax], ...]

    for seg in segments:
        c = _seg_count(seg, warnings)
        delta = c - prev
        if delta > 0:
            from_id = _stop_id(seg, "from_stop")
            if from_id is not None:
                queue.append([from_id, delta])
                boardings += delta
        elif delta < 0:
            need = -delta
            drop_id = _stop_id(seg, "from_stop")  # высадка на границе перед seg
            while need > 0 and queue:
                board_id, cnt = queue[0]
                take = min(cnt, need)
                if drop_id is not None and board_id != drop_id:
                    trips.append({
                        "route_id": route_id,
                        "from_id": board_id,
                        "to_id": drop_id,
                        "pax": take,
                    })
                cnt -= take
                need -= take
                if cnt == 0:
                    queue.pop(0)
                else:
                    queue[0][1] = cnt
            if need > 0:
                warnings.append(f"count_underflow:{seg.get('seg_key')}")
        prev = c

    # Остаток в конце ходки — выходят на последней to_stop.
    last_to = _stop_id(segments[-1], "to_stop")
    for board_id, cnt in queue:
        if last_to is not None and board_id != last_to:
            trips.append({
                "route_id": route_id,
                "from_id": board_id,
                "to_id": last_to,
                "pax": cnt,
            })
    return trips, boardings, warnings


def intercity_runs(data: dict) -> list[dict]:
    """FIFO-разбор каждой ходки межгорода.

    -> [{route_id, occurrence, segments, trips, boardings, warnings}] в порядке
    появления ходок в data.segments; segments внутри ходки — по времени.
    """
    # Группируем по ходкам, сохраняя порядок появления; внутри — по времени.
    groups: dict[tuple, list[dict]] = {}
    for seg in data.get("segments") or []:
        groups.setdefault(_run_key(seg), []).append(seg)

    runs: list[dict] = []
    for (route_id, occurrence), segs in groups.items():
        segs_sorted = sorted(segs, key=lambda s: s.get("period_start_utc") or 0)
        trips, boardings, warnings = _compute_run(segs_sorted)
        runs.append({
            "route_id": route_id,
            "occurrence": occurrence,
            "segments": segs_sorted,
            "trips": trips,
            "boardings": boardings,
            "warnings": warnings,
        })
    return runs


def compute_intercity_passengers(data: dict) -> tuple[list[dict], float, list[str]]:
    """Разбирает все ходки межгорода. -> (trips, passengers_total, warnings).

    trips — список завершённых поездок для расчёта денег.
    passengers_total — суммарное число посадок за день.
    """
    all_trips: list[dict] = []
    total_boardings = 0.0
    all_warnings: list[str] = []
    for run in intercity_runs(data):
        all_trips.extend(run["trips"])
        total_boardings += run["boardings"]
        all_warnings.extend(run["warnings"])
    return all_trips, total_boardings, all_warnings


def city_passengers(data: dict) -> float:
    """Город: всего подсчитанных пассажиров за день (in_salon_total)."""
    total = (data.get("result") or {}).get("in_salon_total")
    try:
        return float(total)
    except (TypeError, ValueError):
        return 0.0


# ── тарифы ───────────────────────────────────────────────────────────

def resolve_tariff(
    db: Session, car_id: int, route_id, day: date
) -> Tariff | None:
    """Тариф маршрута, действующий на day (макс. start_date <= day)."""
    try:
        route_id = int(route_id)
    except (TypeError, ValueError):
        return None
    tariff_id = db.execute(
        select(CarRouteTariff.tariff_id)
        .where(
            CarRouteTariff.car_id == car_id,
            CarRouteTariff.route_id == route_id,
            CarRouteTariff.start_date <= day,
        )
        .order_by(CarRouteTariff.start_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if tariff_id is None:
        return None
    return db.get(Tariff, int(tariff_id))


def tariff_pairs(tariff: Tariff | None) -> dict | None:
    """Цены пар остановок тарифа: {"<min_id>-<max_id>": price} или None."""
    if tariff is None:
        return None
    prices = _parse_json(tariff.prices)
    if not isinstance(prices, dict):
        return None
    pairs = prices.get("pairs")
    return pairs if isinstance(pairs, dict) else None


def pair_price(pairs: dict, a: int, b: int):
    """-> (цена пары a-b или None, ключ пары). Направление не важно."""
    key = f"{min(a, b)}-{max(a, b)}"
    return pairs.get(key), key


def compute_revenue(
    db: Session, car_id: int, trips: list[dict], day: date
) -> tuple[float, list[str]]:
    """Сумма грн по поездкам × цена пары остановок актуального тарифа."""
    revenue = 0.0
    warnings: list[str] = []
    pairs_cache: dict[int, dict | None] = {}

    for t in trips:
        rid = t["route_id"]
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            warnings.append(f"bad_route_id:{rid}")
            continue
        if rid_int not in pairs_cache:
            pairs_cache[rid_int] = tariff_pairs(resolve_tariff(db, car_id, rid_int, day))
        pairs = pairs_cache[rid_int]
        if not pairs:
            warnings.append(f"no_tariff:{rid_int}")
            continue
        a, b = t["from_id"], t["to_id"]
        if a == b:
            continue
        price, key = pair_price(pairs, a, b)
        if price is None:
            warnings.append(f"no_pair_price:{rid_int}:{key}")
            continue
        try:
            revenue += t["pax"] * float(price)
        except (TypeError, ValueError):
            warnings.append(f"bad_price:{rid_int}:{key}")

    revenue = round(revenue, 2)
    # Возвращаем int, если сумма целая (грн обычно без копеек).
    if revenue == int(revenue):
        revenue = int(revenue)
    return revenue, warnings


def active_tariffs_for_routes(
    db: Session, car_id: int, route_ids: list[int], day: date
) -> list[dict]:
    """Список тарифов, актуальных на day, по маршрутам (для Type 2 API)."""
    result: list[dict] = []
    seen: set[int] = set()
    for rid in route_ids:
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            continue
        if rid_int in seen:
            continue
        seen.add(rid_int)
        tariff = resolve_tariff(db, car_id, rid_int, day)
        result.append({
            "route_id": rid_int,
            "tariff_id": int(tariff.id) if tariff else None,
            "tariff_name": tariff.name if tariff else None,
            "prices": _parse_json(tariff.prices) if tariff else None,
        })
    return result
