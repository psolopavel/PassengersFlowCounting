"""Разовый бэкофилл агрегата counting_daily_summary по существующим ready-сессиям.

Запускать НА СЕРВЕРЕ (нужен доступ к БД) ПОСЛЕ применения sql/revenue_summary.sql:

    python scripts/backfill_summaries.py

Идемпотентен: для каждой пары (car_id, count_day) пересобирает каноническую
строку (materialize_day). Повторный запуск просто перезапишет значения.
"""

from sqlalchemy import select

from app.database import AppSession
from app.models import CountingSession
from app.services import summary_service


def main() -> None:
    db = AppSession()
    done = 0
    failed = 0
    try:
        pairs = db.execute(
            select(
                CountingSession.car_id,
                CountingSession.count_day,
                CountingSession.count_type,
            )
            .where(CountingSession.status == "ready")
            .distinct()
        ).all()
        print(f"Знайдено пар (car, day, count_type) з ready-сесіями: {len(pairs)}")
        for car_id, count_day, count_type in pairs:
            try:
                summary_service.materialize_day(
                    db, int(car_id), count_day,
                    count_type=count_type or "full_day",
                )
                done += 1
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                failed += 1
                print(f"  ! car={car_id} day={count_day}: {exc}")
        print(f"Готово. Оброблено: {done}, помилок: {failed}.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
