"""Данные клиентского отчёта о подсчёте (город / межгород).

Превращает data ready-сессии в плоский DTO, который без разбора JSON рисуют
report_xlsx_service и шаблоны письма. Не ходит в БД: тарифы приходят через
колбэк tariff_for_route (см. report_delivery_service).

Время — локальное (Europe/Kiev) и naive: Excel не поддерживает datetime с
часовым поясом.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from app.services import revenue_service as rev
from app.utils.local_tz import LOCAL_TZ, unix_to_local_naive

KIND_LABELS = {
    "city": "Міський маршрут",
    "intercity": "Міжміський маршрут",
}
SOURCE_LABELS = {
    "photo": "фото з камер салону",
    "video": "відеозаписи з камер салону",
    "photoAndVideo": "фото та відеозаписи з камер салону",
}


# ── DTO ──────────────────────────────────────────────────────────────

@dataclass
class RouteTariff:
    name: str
    pairs: dict | None


TariffLookup = Callable[[int], "RouteTariff | None"]


@dataclass
class ReportHeader:
    car_name: str
    imei: str
    company: str
    day: date
    kind: str
    version: int
    previous_sent_at: datetime | None
    generated_at: datetime
    source_label: str

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def is_update(self) -> bool:
        return self.version > 1

    @property
    def counted_description(self) -> str:
        """Що саме підраховано — однаково для листа і xlsx."""
        if self.kind == "intercity":
            return (
                "Для кожної ходки підраховано кількість пасажирів у салоні на кожному "
                f"відрізку між зупинками (за матеріалами: {self.source_label}). "
                "Посадки та висадки визначено за зміною кількості пасажирів між "
                "відрізками за принципом «хто раніше сів — той раніше вийшов»."
            )
        return (
            "Підраховано загальну кількість пасажирів, які увійшли до салону "
            "транспортного засобу протягом робочого дня "
            f"(за матеріалами: {self.source_label})."
        )

    @property
    def update_note(self) -> str:
        if not self.is_update:
            return ""
        note = (
            f"Це оновлена версія звіту (версія {self.version}): дані за цей день "
            "було перераховано. Вона замінює всі попередні версії"
        )
        if self.previous_sent_at:
            note += f", остання з яких надіслана {fmt_datetime(self.previous_sent_at)}"
        return note + "."


REVENUE_DESCRIPTION = (
    "Виручку розраховано за тарифом, чинним на дату підрахунку: кількість "
    "пасажирів × вартість проїзду між зупинкою посадки та зупинкою висадки. "
    "Розрахунок є оціночним і базується на підрахунку пасажирів."
)


@dataclass
class CityReport:
    header: ReportHeader
    passengers: float
    distance_km: float
    window_start: datetime | None
    window_end: datetime | None


@dataclass
class SegmentRow:
    run_number: int
    route_name: str
    occurrence: int | None
    departure: datetime | None
    arrival: datetime | None
    from_name: str
    to_name: str
    distance_km: float | None
    count: int | None
    delta: int


@dataclass
class TripRow:
    run_number: int
    route_name: str
    occurrence: int | None
    from_name: str
    to_name: str
    pax: int
    price: float | None
    subtotal: float | None


@dataclass
class RunRow:
    number: int
    route_id: int | None
    route_name: str
    occurrence: int | None
    start: datetime | None
    end: datetime | None
    duration_sec: int
    distance_km: float
    boardings: int
    revenue: float | None  # None — для маршруту немає тарифу
    tariff_name: str | None


@dataclass
class TariffRow:
    route_name: str
    tariff_name: str | None


@dataclass
class IntercityReport:
    header: ReportHeader
    passengers: float
    distance_km: float
    work_sec: int
    revenue: float
    unpriced_pax: int
    runs: list[RunRow] = field(default_factory=list)
    trips: list[TripRow] = field(default_factory=list)
    segments: list[SegmentRow] = field(default_factory=list)
    tariffs: list[TariffRow] = field(default_factory=list)

    @property
    def run_count(self) -> int:
        return len(self.runs)


# ── утилиты ──────────────────────────────────────────────────────────

def local_dt(ts) -> datetime | None:
    """Unix UTC -> naive локальное время (Europe/Kiev)."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return unix_to_local_naive(ts)


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    f = _float(value)
    return None if f is None else int(round(f))


def fmt_num(value, decimals: int = 2, *, keep_zeros: bool = False) -> str:
    """1234.5 -> '1 234,5' (украинский формат; хвостовые нули убираются)."""
    f = _float(value)
    if f is None:
        return "—"
    text = f"{f:,.{decimals}f}".replace(",", "\u00a0").replace(".", ",")
    if "," in text and not keep_zeros:
        text = text.rstrip("0").rstrip(",")
    return text


def fmt_money(value) -> str:
    """1234 -> '1 234 ₴', 1234.5 -> '1 234,50 ₴'."""
    f = _float(value)
    if f is None:
        return "—"
    if f.is_integer():
        return f"{fmt_num(f, 0)} ₴"
    return f"{fmt_num(f, 2, keep_zeros=True)} ₴"


def fmt_date(value: date | datetime | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def fmt_datetime(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"


def fmt_duration(seconds) -> str:
    sec = _int(seconds) or 0
    if sec <= 0:
        return "0 хв"
    h, m = sec // 3600, (sec % 3600) // 60
    if h and m:
        return f"{h} год {m} хв"
    if h:
        return f"{h} год"
    return f"{m} хв" if m else "< 1 хв"


# ── отпечаток данных ─────────────────────────────────────────────────

def data_fingerprint(data: dict) -> str:
    """sha256 значимой для клиента части data.

    Служебные поля (generated_at, meta, warnings, флаги выбора) не входят —
    «Перерахувати» без изменения чисел не порождает новую версию отчёта.
    """
    segments = []
    for s in data.get("segments") or []:
        segments.append([
            s.get("seg_key"),
            _float(s.get("count")),
            _float(s.get("distance_km")),
            (s.get("from_stop") or {}).get("id"),
            (s.get("to_stop") or {}).get("id"),
            s.get("period_start_utc"),
            s.get("period_end_utc"),
        ])
    payload = {
        "kind": data.get("kind"),
        "distance_km": _float(data.get("distance_km")),
        "in_salon_total": _float((data.get("result") or {}).get("in_salon_total")),
        "segments": segments,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── сборка отчёта ────────────────────────────────────────────────────

def build_header(
    *,
    car,
    data: dict,
    day: date,
    kind: str,
    version: int,
    previous_sent_at: datetime | None = None,
) -> ReportHeader:
    source = data.get("source_used") or getattr(car, "count_source", "") or ""
    return ReportHeader(
        car_name=(getattr(car, "name", "") or "").strip() or str(getattr(car, "imei", "")),
        imei=str(getattr(car, "imei", "") or ""),
        company=(getattr(car, "company_name", "") or "").strip(),
        day=day,
        kind=kind,
        version=max(1, int(version)),
        previous_sent_at=previous_sent_at,
        generated_at=now_local(),
        source_label=SOURCE_LABELS.get(source, "записи з камер салону"),
    )


def build_city_report(header: ReportHeader, data: dict) -> CityReport:
    window = data.get("window") or {}
    return CityReport(
        header=header,
        passengers=rev.city_passengers(data),
        distance_km=_float(data.get("distance_km")) or 0.0,
        window_start=local_dt(window.get("start_utc")),
        window_end=local_dt(window.get("end_utc")),
    )


def _stop_names(segments: list[dict]) -> dict[int, str]:
    names: dict[int, str] = {}
    for seg in segments:
        for side in ("from_stop", "to_stop"):
            stop = seg.get(side) or {}
            sid = _int(stop.get("id"))
            if sid is not None:
                names[sid] = stop.get("name") or str(sid)
    return names


def build_intercity_report(
    header: ReportHeader,
    data: dict,
    tariff_for_route: TariffLookup,
) -> IntercityReport:
    """Ходки, поездки с ценой по актуальному тарифу и отрезки межгорода.

    FIFO-разбор — rev.intercity_runs (тот же, что в API выручки), цена поездки —
    пара «остановка посадки — остановка высадки» тарифа маршрута.
    """
    stop_names = _stop_names(data.get("segments") or [])
    periods = {
        (p.get("route_id"), p.get("occurrence")): p
        for p in data.get("route_periods") or []
    }
    tariff_cache: dict[int, RouteTariff | None] = {}

    def tariff(route_id) -> RouteTariff | None:
        rid = _int(route_id)
        if rid is None:
            return None
        if rid not in tariff_cache:
            tariff_cache[rid] = tariff_for_route(rid)
        return tariff_cache[rid]

    report = IntercityReport(
        header=header,
        passengers=0.0,
        distance_km=_float(data.get("distance_km")) or 0.0,
        work_sec=0,
        revenue=0.0,
        unpriced_pax=0,
    )
    seen_routes: set = set()

    for number, run in enumerate(rev.intercity_runs(data), start=1):
        segs = run["segments"]
        route_id = run["route_id"]
        occurrence = _int(run["occurrence"])
        route_name = segs[0].get("route_name") or f"Маршрут {route_id}"
        period = periods.get((route_id, run["occurrence"])) or {}
        start_ts = period.get("start_utc") or segs[0].get("period_start_utc")
        end_ts = period.get("end_utc") or segs[-1].get("period_end_utc")
        duration = max(0, (_int(end_ts) or 0) - (_int(start_ts) or 0))
        run_tariff = tariff(route_id)
        pairs = run_tariff.pairs if run_tariff else None

        run_revenue = 0.0
        for trip in run["trips"]:
            price = None
            if pairs:
                raw_price, _key = rev.pair_price(pairs, trip["from_id"], trip["to_id"])
                price = _float(raw_price)
            pax = int(trip["pax"])
            subtotal = round(pax * price, 2) if price is not None else None
            if subtotal is None:
                report.unpriced_pax += pax
            else:
                run_revenue += subtotal
            report.trips.append(TripRow(
                run_number=number,
                route_name=route_name,
                occurrence=occurrence,
                from_name=stop_names.get(trip["from_id"], str(trip["from_id"])),
                to_name=stop_names.get(trip["to_id"], str(trip["to_id"])),
                pax=pax,
                price=price,
                subtotal=subtotal,
            ))

        prev = 0
        run_km = 0.0
        for seg in segs:
            count = _int(seg.get("count"))
            km = _float(seg.get("distance_km"))
            run_km += km or 0.0
            report.segments.append(SegmentRow(
                run_number=number,
                route_name=route_name,
                occurrence=occurrence,
                departure=local_dt((seg.get("from_stop") or {}).get("departure_utc")
                                   or seg.get("period_start_utc")),
                arrival=local_dt((seg.get("to_stop") or {}).get("arrival_utc")
                                 or seg.get("period_end_utc")),
                from_name=(seg.get("from_stop") or {}).get("name") or "—",
                to_name=(seg.get("to_stop") or {}).get("name") or "—",
                distance_km=km,
                count=count,
                delta=(count or 0) - prev,
            ))
            prev = count or 0

        report.runs.append(RunRow(
            number=number,
            route_id=_int(route_id),
            route_name=route_name,
            occurrence=occurrence,
            start=local_dt(start_ts),
            end=local_dt(end_ts),
            duration_sec=duration,
            distance_km=round(run_km, 2),
            boardings=int(run["boardings"]),
            revenue=round(run_revenue, 2) if pairs else None,
            tariff_name=run_tariff.name if run_tariff else None,
        ))
        report.passengers += run["boardings"]
        report.work_sec += duration
        report.revenue += run_revenue

        if route_id not in seen_routes:
            seen_routes.add(route_id)
            report.tariffs.append(TariffRow(
                route_name=route_name,
                tariff_name=run_tariff.name if run_tariff else None,
            ))

    report.revenue = round(report.revenue, 2)
    return report
