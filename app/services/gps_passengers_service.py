"""Запись городского подсчёта в gps.passengers (legacy-интеграция с другим сайтом).

При завершении міського підрахунку создаём или обновляем строку:
  imei, data_type=2, timestart=timestop=count_day (YYYY-MM-DD),
  type=0, data=[["1","","","","","","",<passengers>]], route_id=0.
"""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

_DATA_TYPE_CITY = 2
_TYPE_CITY = 0


def _day_str(day: date) -> str:
    return day.isoformat()


def _city_data_payload(passengers: float) -> str:
    return json.dumps([["1", "", "", "", "", "", "", float(passengers)]], ensure_ascii=False)


def _find_row_id(gps_db: Session, imei: str, day: date) -> int | None:
    row = gps_db.execute(
        text(
            "SELECT id FROM passengers "
            "WHERE imei = :imei AND data_type = :dt AND timestart = :d "
            "AND timestop = :d AND type = :t "
            "LIMIT 1"
        ),
        {
            "imei": imei,
            "dt": _DATA_TYPE_CITY,
            "d": _day_str(day),
            "t": _TYPE_CITY,
        },
    ).first()
    if row is None:
        return None
    return int(row[0])


def sync_city_count(
    gps_db: Session, imei: str, day: date, passengers: float
) -> None:
    """Создаёт или заменяет запись gps.passengers за день."""
    imei = str(imei).strip()
    if not imei:
        return
    day_s = _day_str(day)
    data = _city_data_payload(passengers)
    row_id = _find_row_id(gps_db, imei, day)
    if row_id is not None:
        gps_db.execute(
            text("UPDATE passengers SET data = :data WHERE id = :id"),
            {"data": data, "id": row_id},
        )
        return
    gps_db.execute(
        text(
            "INSERT INTO passengers "
            "(imei, data_type, timestart, timestop, type, data, route_id) "
            "VALUES (:imei, :dt, :d, :d, :t, :data, 0)"
        ),
        {
            "imei": imei,
            "dt": _DATA_TYPE_CITY,
            "d": day_s,
            "t": _TYPE_CITY,
            "data": data,
        },
    )


def delete_city_count(gps_db: Session, imei: str, day: date) -> None:
    """Удаляет запись gps.passengers за день (когда ready-сессии больше нет)."""
    imei = str(imei).strip()
    if not imei:
        return
    gps_db.execute(
        text(
            "DELETE FROM passengers "
            "WHERE imei = :imei AND data_type = :dt AND timestart = :d "
            "AND timestop = :d AND type = :t"
        ),
        {
            "imei": imei,
            "dt": _DATA_TYPE_CITY,
            "d": _day_str(day),
            "t": _TYPE_CITY,
        },
    )
