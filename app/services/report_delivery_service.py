"""Очередь отправки клиентских xlsx-отчётов (таблица report_deliveries).

Две стороны:

  * schedule_for_day — хук после материализации агрегата (summary_service):
    ставит в очередь новую версию данных дня и отменяет устаревшие pending;
  * process_due — вызывает демон app/workers/report_mailer.py: проверяет, что
    данные всё ещё актуальны, собирает xlsx + письмо и шлёт через SendLayer;
    при сбое — повтор с нарастающей паузой.

Получатели: cars.owner_user_id -> jos_users.username -> gps.user_settings
(type = 1, params — адреса через запятую). Каждый адрес валидируется.
Отчёты шлются только для city / intercity с count_type = full_day.

Версия отчёта = MAX(version отправленных за день) + 1, присваивается в момент
успешной отправки — отменённые и неотправленные строки номер не занимают.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import GpsSession, JoomlaSession
from app.models import Car, CountingDailySummary, CountingSession, JosUser, ReportDelivery
from app.services import report_data_service as rds
from app.services import report_xlsx_service as xlsx
from app.services import revenue_service as rev
from app.services import sendlayer_service as mailer
from app.utils.debug_log import debug_exc, debug_log

REPORT_KINDS = ("city", "intercity")
REPORT_COUNT_TYPE = "full_day"
USER_SETTINGS_EMAIL_TYPE = 1

# Паузы между повторами (мин) после 1-й, 2-й, ... неудачной попытки.
_RETRY_DELAYS_MIN = (1, 5, 15, 60, 180)
_MAX_ERROR_LEN = 2000
_DONE_STATUSES = ("sending", "sent", "failed", "no_recipient")

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)

_templates = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "templates" / "emails")),
    autoescape=select_autoescape(["html"]),
    keep_trailing_newline=True,
)
_templates.filters.update({
    "ua_num": rds.fmt_num,
    "ua_money": rds.fmt_money,
    "ua_date": rds.fmt_date,
    "ua_datetime": rds.fmt_datetime,
    "ua_duration": rds.fmt_duration,
})


# ── адреса ───────────────────────────────────────────────────────────

def is_valid_email(email: str) -> bool:
    if not email or len(email) > 254:
        return False
    local, _, _domain = email.rpartition("@")
    if not local or len(local) > 64 or ".." in email:
        return False
    if local.startswith(".") or local.endswith("."):
        return False
    return bool(_EMAIL_RE.match(email))


def parse_emails(raw: str | None) -> tuple[list[str], list[str]]:
    """'a@x.ua, b@y.com,bad' -> (валидные, невалидные). Дубли убираются."""
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;\s]+", raw or ""):
        email = part.strip()
        if not email or email.lower() in seen:
            continue
        seen.add(email.lower())
        (valid if is_valid_email(email) else invalid).append(email)
    return valid, invalid


def _owner_username(car: Car) -> str:
    if car is None or not car.owner_user_id:
        return ""
    joomla_db = JoomlaSession()
    try:
        owner = joomla_db.get(JosUser, int(car.owner_user_id))
        return (owner.username or "").strip().lower() if owner else ""
    finally:
        joomla_db.close()


def resolve_recipients(car: Car) -> tuple[list[str], list[str]]:
    """Адреса владельца машины из gps.user_settings (type = 1).

    -> (валидные, невалидные). Владелец не задан / нет настроек -> ([], []).
    """
    username = _owner_username(car)
    if not username:
        return [], []
    gps_db = GpsSession()
    try:
        rows = gps_db.execute(
            text(
                "SELECT params FROM user_settings "
                "WHERE username = :username AND type = :type"
            ),
            {"username": username, "type": USER_SETTINGS_EMAIL_TYPE},
        ).scalars().all()
    finally:
        gps_db.close()
    return parse_emails(",".join(r for r in rows if r))


def _bcc_list(to: list[str]) -> list[str]:
    taken = {e.lower() for e in to}
    valid, _invalid = parse_emails(get_settings().report_bcc)
    return [e for e in valid if e.lower() not in taken]


# ── выборки ──────────────────────────────────────────────────────────

def _load_data(session: CountingSession | None) -> dict | None:
    if session is None or not session.data:
        return None
    try:
        data = json.loads(session.data)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _day_rows(db: Session, car_id: int, day: date, count_type: str, statuses) -> list[ReportDelivery]:
    return list(db.execute(
        select(ReportDelivery)
        .where(
            ReportDelivery.car_id == car_id,
            ReportDelivery.count_day == day,
            ReportDelivery.count_type == count_type,
            ReportDelivery.status.in_(statuses),
        )
        .order_by(ReportDelivery.id.desc())
    ).scalars())


def _last_sent(db: Session, car_id: int, day: date, count_type: str) -> ReportDelivery | None:
    return db.execute(
        select(ReportDelivery)
        .where(
            ReportDelivery.car_id == car_id,
            ReportDelivery.count_day == day,
            ReportDelivery.count_type == count_type,
            ReportDelivery.status == "sent",
        )
        .order_by(ReportDelivery.version.desc(), ReportDelivery.id.desc())
        .limit(1)
    ).scalars().first()


def _summary(db: Session, car_id: int, day: date, count_type: str) -> CountingDailySummary | None:
    return db.execute(
        select(CountingDailySummary).where(
            CountingDailySummary.car_id == car_id,
            CountingDailySummary.count_day == day,
            CountingDailySummary.count_type == count_type,
        )
    ).scalars().first()


def _cancel(rows: list[ReportDelivery], reason: str) -> None:
    for row in rows:
        row.status = "cancelled"
        row.last_error = reason


# ── постановка в очередь (хук) ───────────────────────────────────────

def schedule_for_day(
    db: Session,
    car_id: int,
    day: date,
    *,
    count_type: str,
    summary: CountingDailySummary | None,
) -> ReportDelivery | None:
    """Синхронизирует очередь с текущим агрегатом дня.

    summary — результат summary_service.materialize_day (None, если ready-сессии
    больше нет). Те же данные, что уже в очереди/отправлены, новой строки не дают.
    """
    settings = get_settings()
    if not settings.report_emails_enabled or count_type != REPORT_COUNT_TYPE:
        return None

    pending = _day_rows(db, car_id, day, count_type, ("pending",))
    if summary is None or summary.kind not in REPORT_KINDS:
        if pending:
            _cancel(pending, "Підрахунок більше не завершений")
            db.commit()
        return None

    data = _load_data(db.get(CountingSession, int(summary.session_id)))
    if data is None:
        if pending:
            _cancel(pending, "Немає даних сесії")
            db.commit()
        return None
    data_hash = rds.data_fingerprint(data)

    same = [r for r in pending if r.data_hash == data_hash]
    if same:
        keep = same[0]
        keep.session_id = int(summary.session_id)
        _cancel([r for r in pending if r is not keep], "Дублікат у черзі")
        db.commit()
        return keep

    _cancel(pending, "Дані змінено — у черзі нова версія")
    done = _day_rows(db, car_id, day, count_type, _DONE_STATUSES)
    if done and done[0].data_hash == data_hash:
        # Повернулися до вже надісланих даних — нова версія не потрібна.
        db.commit()
        return None

    car = db.get(Car, int(car_id))
    try:
        valid, invalid = resolve_recipients(car)
        recipients = valid
    except Exception as exc:  # noqa: BLE001 — адреси дочитає демон перед відправкою
        debug_log("resolve_recipients failed on schedule", tag="report_mail",
                  data={"car_id": car_id, "error": str(exc)})
        recipients, invalid = None, []

    row = ReportDelivery(
        session_id=int(summary.session_id),
        car_id=int(car_id),
        imei=(car.imei if car else "") or "",
        count_day=day,
        count_type=count_type,
        kind=summary.kind,
        data_hash=data_hash,
        recipient_emails=recipients,
        skipped_emails=invalid or None,
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now() + timedelta(minutes=settings.report_send_delay_min),
    )
    db.add(row)
    db.commit()
    return row


def safe_schedule_for_day(
    db: Session,
    car_id: int,
    day: date,
    *,
    count_type: str,
    summary: CountingDailySummary | None,
) -> None:
    """Обёртка для хука: сбой очереди не должен ломать сохранение подсчёта."""
    try:
        schedule_for_day(db, car_id, day, count_type=count_type, summary=summary)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        debug_exc("schedule_for_day failed", tag="report_mail", exc=exc)


# ── сборка отчёта и письма ───────────────────────────────────────────

def build_report(
    db: Session,
    car: Car,
    data: dict,
    *,
    day: date,
    kind: str,
    version: int,
    previous_sent_at: datetime | None = None,
) -> rds.CityReport | rds.IntercityReport:
    header = rds.build_header(
        car=car, data=data, day=day, kind=kind,
        version=version, previous_sent_at=previous_sent_at,
    )
    if kind != "intercity":
        return rds.build_city_report(header, data)

    def tariff_for_route(route_id: int) -> rds.RouteTariff | None:
        tariff = rev.resolve_tariff(db, int(car.id), route_id, day)
        if tariff is None:
            return None
        return rds.RouteTariff(name=tariff.name, pairs=rev.tariff_pairs(tariff))

    return rds.build_intercity_report(header, data, tariff_for_route)


def email_subject(header: rds.ReportHeader) -> str:
    subject = f"Звіт про підрахунок пасажирів за {rds.fmt_date(header.day)} — {header.car_name}"
    if header.is_update:
        subject += f" (версія {header.version}, оновлено)"
    return subject


def render_email(report, *, subject: str, filename: str) -> tuple[str, str]:
    """-> (html, plain text) письма."""
    context = {
        "report": report,
        "h": report.header,
        "intercity": isinstance(report, rds.IntercityReport),
        "subject": subject,
        "filename": filename,
        "revenue_description": rds.REVENUE_DESCRIPTION,
        "reply_to": get_settings().report_reply_to,
    }
    html = _templates.get_template("count_report.html").render(**context)
    plain = _templates.get_template("count_report.txt").render(**context)
    return html, plain


# ── отправка (демон) ─────────────────────────────────────────────────

def _finish(db: Session, row: ReportDelivery, status: str, reason: str) -> bool:
    row.status = status
    row.last_error = reason
    db.commit()
    return False


def _schedule_retry(db: Session, row: ReportDelivery, error: str, *, retryable: bool) -> None:
    settings = get_settings()
    row.attempts = int(row.attempts or 0) + 1
    row.last_error = error[:_MAX_ERROR_LEN]
    if not retryable or row.attempts >= settings.report_max_attempts:
        row.status = "failed"
    else:
        delay = _RETRY_DELAYS_MIN[min(row.attempts, len(_RETRY_DELAYS_MIN)) - 1]
        row.status = "pending"
        row.next_attempt_at = datetime.now() + timedelta(minutes=delay)
    db.commit()


def _deliver(db: Session, row: ReportDelivery) -> bool:
    """Одна отправка строки в статусе sending. -> True, если письмо ушло."""
    summary = _summary(db, int(row.car_id), row.count_day, row.count_type)
    if summary is None or summary.status != "ready":
        return _finish(db, row, "cancelled", "Підрахунок більше не завершений")
    data = _load_data(db.get(CountingSession, int(summary.session_id)))
    if data is None or rds.data_fingerprint(data) != row.data_hash:
        return _finish(db, row, "cancelled", "Дані змінилися після постановки в чергу")
    car = db.get(Car, int(row.car_id))
    if car is None:
        return _finish(db, row, "cancelled", "Машину видалено")

    valid, invalid = resolve_recipients(car)
    row.recipient_emails = valid
    row.skipped_emails = invalid or None
    if not valid:
        return _finish(
            db, row, "no_recipient",
            "Немає валідних адрес у gps.user_settings (type = 1) для власника машини",
        )

    previous = _last_sent(db, int(row.car_id), row.count_day, row.count_type)
    version = int(previous.version or 0) + 1 if previous else 1
    report = build_report(
        db, car, data,
        day=row.count_day, kind=row.kind, version=version,
        previous_sent_at=previous.sent_at if previous else None,
    )
    content = xlsx.build_xlsx(report)
    filename = xlsx.report_filename(report.header)
    subject = email_subject(report.header)
    html, plain = render_email(report, subject=subject, filename=filename)

    try:
        message_id = mailer.send_email(
            to=valid,
            subject=subject,
            html=html,
            text=plain,
            attachments=[mailer.Attachment(filename, content, xlsx.XLSX_MIME)],
            bcc=_bcc_list(valid),
            reply_to=get_settings().report_reply_to,
            tags=["count-report", row.kind],
        )
    except mailer.SendLayerError as exc:
        _schedule_retry(db, row, str(exc), retryable=exc.retryable)
        return False

    row.status = "sent"
    row.version = version
    row.provider_message_id = message_id[:128] or None
    row.sent_at = datetime.now()
    row.attempts = int(row.attempts or 0) + 1
    row.last_error = None
    db.commit()
    debug_log("report sent", tag="report_mail", data={
        "delivery_id": row.id, "car_id": row.car_id, "day": row.count_day.isoformat(),
        "version": version, "to": valid,
    })
    return True


def process_due(db: Session, *, limit: int = 20) -> int:
    """Отправляет pending-строки, чей next_attempt_at наступил. -> число отправленных."""
    ids = db.execute(
        select(ReportDelivery.id)
        .where(
            ReportDelivery.status == "pending",
            ReportDelivery.next_attempt_at <= datetime.now(),
        )
        .order_by(ReportDelivery.next_attempt_at, ReportDelivery.id)
        .limit(limit)
    ).scalars().all()

    sent = 0
    for delivery_id in ids:
        claimed = db.execute(
            update(ReportDelivery)
            .where(ReportDelivery.id == delivery_id, ReportDelivery.status == "pending")
            .values(status="sending")
        ).rowcount
        db.commit()
        if not claimed:
            continue
        row = db.get(ReportDelivery, delivery_id)
        try:
            if _deliver(db, row):
                sent += 1
        except Exception as exc:  # noqa: BLE001 — сбой БД/сборки: повторим позже
            db.rollback()
            debug_exc(f"deliver failed id={delivery_id}", tag="report_mail", exc=exc)
            row = db.get(ReportDelivery, delivery_id)
            if row is not None:
                _schedule_retry(db, row, f"{type(exc).__name__}: {exc}", retryable=True)
    return sent


def recover_stale_sending(db: Session) -> int:
    """sending -> pending после аварийной остановки демона (вызывать при старте)."""
    count = db.execute(
        update(ReportDelivery)
        .where(ReportDelivery.status == "sending")
        .values(status="pending", next_attempt_at=datetime.now())
    ).rowcount
    db.commit()
    return int(count or 0)


def build_download(db: Session, car: Car, session: CountingSession) -> tuple[bytes, str]:
    """xlsx дня за запитом (API) — той самий, що йде в листі. -> (bytes, filename).

    Версія узгоджена з надісланими листами: дані не змінилися — номер останнього
    листа, змінилися — номер наступного.
    """
    data = _load_data(session)
    if data is None:
        raise ValueError("Немає даних сесії")
    kind = data.get("kind") or session.kind
    if kind not in REPORT_KINDS:
        raise ValueError(f"Звіт для типу «{kind}» не формується")

    last = _last_sent(db, int(car.id), session.count_day, session.count_type)
    if last is None:
        version, previous_sent_at = 1, None
    elif last.data_hash == rds.data_fingerprint(data):
        version, previous_sent_at = int(last.version or 1), None
    else:
        version, previous_sent_at = int(last.version or 0) + 1, last.sent_at

    report = build_report(
        db, car, data, day=session.count_day, kind=kind,
        version=version, previous_sent_at=previous_sent_at,
    )
    return xlsx.build_xlsx(report), xlsx.report_filename(report.header)


def preview(db: Session, car_id: int, day: date, out_dir: Path) -> list[Path]:
    """Собирает xlsx + письмо по текущему агрегату дня в out_dir, без отправки."""
    summary = _summary(db, car_id, day, REPORT_COUNT_TYPE)
    if summary is None or summary.kind not in REPORT_KINDS:
        raise ValueError("Немає ready-підрахунку city/intercity за цей день")
    data = _load_data(db.get(CountingSession, int(summary.session_id)))
    car = db.get(Car, int(car_id))
    if data is None or car is None:
        raise ValueError("Немає даних сесії або машини")

    previous = _last_sent(db, car_id, day, REPORT_COUNT_TYPE)
    version = int(previous.version or 0) + 1 if previous else 1
    report = build_report(
        db, car, data, day=day, kind=summary.kind, version=version,
        previous_sent_at=previous.sent_at if previous else None,
    )
    filename = xlsx.report_filename(report.header)
    subject = email_subject(report.header)
    html, plain = render_email(report, subject=subject, filename=filename)

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / filename, out_dir / "email.html", out_dir / "email.txt"]
    paths[0].write_bytes(xlsx.build_xlsx(report))
    paths[1].write_text(html, encoding="utf-8")
    paths[2].write_text(f"Тема: {subject}\n\n{plain}", encoding="utf-8")
    return paths
