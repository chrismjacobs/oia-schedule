"""Idempotent notification dispatch. Every notification is gated by a
sent-flag on notification_log so a missed/late/doubled /tick self-heals
(CLAUDE.md #12)."""
from datetime import datetime

from app.extensions import db
from app.models import NotificationLog
from app.notifications.backends import get_backend


def notify_once(type_, target, related_type, related_id, message):
    """Send `message` at most once for this (type, related_type, related_id,
    target) key. Safe to call repeatedly (e.g. from every /tick)."""
    row = NotificationLog.query.filter_by(
        type=type_, target=target, related_type=related_type, related_id=related_id
    ).first()
    if row and row.sent_flag:
        return False

    if not row:
        row = NotificationLog(type=type_, target=target, related_type=related_type,
                               related_id=related_id, sent_flag=False)
        db.session.add(row)
        db.session.flush()

    try:
        get_backend().send(message)
    except Exception:
        db.session.commit()  # keep the row so it's retried next tick
        raise

    row.sent_at = datetime.utcnow()
    row.sent_flag = True
    db.session.commit()
    return True


def notify_committed(month):
    notify_once(
        "committed", "group", "month", month.id,
        f"[OIA] The duty roster for {month.year_month} has been committed. Check your shifts.",
    )


def notify_slot_open(reopened_slot):
    slot = reopened_slot.slot
    notify_once(
        "slot_open", "group", "reopened_slot", reopened_slot.id,
        f"[OIA] A slot opened up: {slot.date.isoformat()} {slot.hour}:00. First come, first served.",
    )


def notify_selection_open(month):
    notify_once(
        "selection_open", "group", "month", month.id,
        f"[OIA] Availability selection for {month.year_month} is now open.",
    )


def notify_closing_warning(month):
    notify_once(
        "closing_warning", "group", "month", month.id,
        f"[OIA] Availability selection for {month.year_month} closes soon — submit your hours.",
    )


def notify_no_show(slot, student):
    notify_once(
        "no_show", "group", "slot", slot.id,
        f"[OIA] Reminder: {student.short_name} was scheduled {slot.date.isoformat()} "
        f"{slot.hour}:00 and hasn't signed in yet.",
    )
