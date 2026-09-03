"""Idempotent notification dispatch. Every notification is gated by a
sent-flag on notification_log so a missed/late/doubled /tick self-heals
(CLAUDE.md #12)."""
from app.extensions import db
from app.models import NotificationLog
from app.notifications.backends import get_backend
from app.utils.settings import get_attendance_notify_enabled
from app.utils.tz import local_now


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

    row.sent_at = local_now()
    row.sent_flag = True
    db.session.commit()
    return True


def notify_committed(month):
    notify_once(
        "committed", "group", "month", month.id,
        f"[OIA] The duty roster for {month.year_month} has been committed. Check your shifts.",
    )


def notify_leave_requested(leave_request):
    """Fires the moment a student submits a leave request — before it's
    approved. Deliberately generic (no name, no reason): it flags the
    overseer to go review it, and primes students that a slot may open up
    soon, without exposing anything personal in the shared group."""
    slot = leave_request.slot
    notify_once(
        "leave_requested", "group", "leave_request", leave_request.id,
        f"[OIA] A leave request came in for {slot.date.isoformat()} {slot.hour}:00 — pending review.",
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


def notify_signed_in(session):
    """Toggle-gated (Advanced > sign-in/out notifications) since this can
    fire a lot on a busy day — every sign-in, not just once."""
    if not get_attendance_notify_enabled():
        return
    notify_once(
        "signed_in", "group", "attendance_session", session.id,
        f"[OIA] {session.student.short_name} signed in.",
    )


def notify_signed_out(session):
    if not get_attendance_notify_enabled():
        return
    notify_once(
        "signed_out", "group", "attendance_session", session.id,
        f"[OIA] {session.student.short_name} signed out.",
    )


def notify_no_show(slot, student):
    notify_once(
        "no_show", "group", "slot", slot.id,
        f"[OIA] Reminder: {student.short_name} was scheduled {slot.date.isoformat()} "
        f"{slot.hour}:00 and hasn't signed in yet.",
    )
