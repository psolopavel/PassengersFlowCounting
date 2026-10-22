"""Разовая миграция истории тарифов из JSON в нормализованную таблицу.

Переносит данные из старой таблицы car_tariff_history(car_id, history JSON)
в новую car_route_tariffs(car_id, route_id, tariff_id, start_date).

Запускать НА СЕРВЕРЕ (нужен доступ к БД) ПОСЛЕ создания car_route_tariffs
(ШАГ 1 в sql/tariff_relations.sql) и ДО удаления старой таблицы:

    python scripts/migrate_tariff_history.py

Записи, ссылающиеся на несуществующие машины/тарифы, пропускаются
(их бы всё равно не пропустил FK). Скрипт можно запускать повторно —
существующие пары (car_id, route_id, start_date) не дублируются.
"""

import json
from datetime import datetime

from sqlalchemy import text

from app.database import AppSession


def _parse_history(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def main() -> None:
    db = AppSession()
    inserted = skipped = 0
    try:
        car_ids = {r[0] for r in db.execute(text("SELECT id FROM cars"))}
        tariff_ids = {r[0] for r in db.execute(text("SELECT id FROM tariffs"))}
        existing = {
            (r[0], r[1], r[2])
            for r in db.execute(
                text("SELECT car_id, route_id, start_date FROM car_route_tariffs")
            )
        }

        rows = db.execute(text("SELECT car_id, history FROM car_tariff_history")).all()
        for car_id, history in rows:
            car_id = int(car_id)
            if car_id not in car_ids:
                continue
            for route_id, periods in _parse_history(history).items():
                try:
                    route_id = int(route_id)
                except (ValueError, TypeError):
                    continue
                for p in periods or []:
                    try:
                        tariff_id = int(p.get("tariff_id"))
                        start = datetime.strptime(
                            (p.get("from") or "").strip(), "%Y-%m-%d"
                        ).date()
                    except (ValueError, TypeError, AttributeError):
                        skipped += 1
                        continue
                    if tariff_id not in tariff_ids:
                        skipped += 1
                        continue
                    if (car_id, route_id, start) in existing:
                        continue
                    db.execute(
                        text(
                            "INSERT INTO car_route_tariffs "
                            "(car_id, route_id, tariff_id, start_date) "
                            "VALUES (:c, :r, :t, :d)"
                        ),
                        {"c": car_id, "r": route_id, "t": tariff_id, "d": start},
                    )
                    existing.add((car_id, route_id, start))
                    inserted += 1
        db.commit()
        print(f"Готово. Добавлено строк: {inserted}, пропущено: {skipped}.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
