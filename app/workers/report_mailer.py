"""Демон отправки клиентских xlsx-отчётов из очереди report_deliveries.

Отдельный процесс (systemd: deploy/passengersflow-mailer.service), а не часть
gunicorn: у приложения несколько воркеров, и каждый отправил бы письмо.

    python -m app.workers.report_mailer             # цикл раз в REPORT_WORKER_INTERVAL_SEC
    python -m app.workers.report_mailer --once      # один проход очереди и выход
    python -m app.workers.report_mailer --preview 12 2026-10-20 [--out DIR]
                                                    # xlsx + письмо в папку, без отправки
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.database import AppSession, app_engine, gps_engine, joomla_engine
from app.services import report_delivery_service as delivery

log = logging.getLogger("report_mailer")


def _run_once() -> int:
    db = AppSession()
    try:
        return delivery.process_due(db)
    finally:
        db.close()


def _disabled_reason() -> str:
    settings = get_settings()
    if not settings.report_emails_enabled:
        return "REPORT_EMAILS_ENABLED=false — відправку вимкнено"
    if not settings.sendlayer_api_key:
        return "SENDLAYER_API_KEY не задано — відправку вимкнено"
    return ""


def run_forever() -> None:
    stop = threading.Event()

    def _stop(signum, _frame):
        log.info("Отримано сигнал %s, зупиняюсь", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    db = AppSession()
    try:
        recovered = delivery.recover_stale_sending(db)
    finally:
        db.close()
    if recovered:
        log.warning("Повернуто в чергу %s листів, що зависли у статусі sending", recovered)

    interval = max(5, get_settings().report_worker_interval_sec)
    log.info("Демон відправки звітів запущено, інтервал %s с", interval)
    disabled = _disabled_reason()
    if disabled:
        log.warning(disabled)
    while not stop.is_set():
        if not disabled:
            try:
                sent = _run_once()
                if sent:
                    log.info("Надіслано звітів: %s", sent)
            except Exception:  # noqa: BLE001 — БД недоступна и т.п.: ждём следующий цикл
                log.exception("Помилка обробки черги")
        stop.wait(interval)
    log.info("Демон зупинено")


def run_preview(car_id: int, day: date, out_dir: Path) -> None:
    db = AppSession()
    try:
        paths = delivery.preview(db, car_id, day, out_dir)
    finally:
        db.close()
    for path in paths:
        print(path)  # noqa: T201


def main() -> int:
    parser = argparse.ArgumentParser(description="Відправка клієнтських звітів підрахунку")
    parser.add_argument("--once", action="store_true", help="один прохід черги і вихід")
    parser.add_argument(
        "--preview", nargs=2, metavar=("CAR_ID", "YYYY-MM-DD"),
        help="зібрати xlsx і лист без відправки",
    )
    parser.add_argument("--out", default="report_preview", help="папка для --preview")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # SQL-эхо движков (DEBUG=true) в журнале демона не нужно; echo=True
    # игнорирует уровень логгера, поэтому выключаем на самих движках.
    for engine in (app_engine, gps_engine, joomla_engine):
        engine.echo = False

    if args.preview:
        run_preview(int(args.preview[0]), date.fromisoformat(args.preview[1]), Path(args.out))
        return 0
    if args.once:
        disabled = _disabled_reason()
        if disabled:
            log.warning(disabled)
            return 1
        print(f"Надіслано: {_run_once()}")  # noqa: T201
        return 0
    run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
