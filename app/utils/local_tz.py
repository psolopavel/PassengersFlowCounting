"""Локальный часовой пояс UI (Украина, IANA Europe/Kyiv) с учётом DST."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # noqa: BLE001 — нет базы tzdata
    LOCAL_TZ = timezone(timedelta(hours=3))


def local_today(now_utc: datetime | None = None) -> date:
    """Календарная «сегодня» в локальном поясе."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(LOCAL_TZ).date()


def unix_to_local(ts: int | float) -> datetime:
    """Unix (UTC) -> aware datetime в LOCAL_TZ."""
    return datetime.fromtimestamp(ts, tz=LOCAL_TZ)


def unix_to_local_naive(ts: int | float) -> datetime:
    """Unix (UTC) -> naive локальное время (для Excel и шаблонов)."""
    return unix_to_local(ts).replace(tzinfo=None)


def combine_local(day: date, t: time) -> datetime:
    """Момент на календарный день по местным часам (учитывает DST на эту дату)."""
    return datetime.combine(day, t, tzinfo=LOCAL_TZ)


def parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    h = int(parts[0]) % 24
    m = int(parts[1]) % 60 if len(parts) > 1 else 0
    return time(h, m, 0)
