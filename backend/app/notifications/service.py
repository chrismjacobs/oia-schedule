"""Idempotent notification dispatch. Every notification is gated by a
sent-flag on notification_log so a missed/late/doubled /tick self-heals
(CLAUDE.md #12)."""
from flask import current_app

from app.extensions import db
from app.models import NotificationLog
from app.notifications.backends import get_backend
from app.utils.settings import get_attendance_notify_enabled
from app.utils.tz import local_now


def notify_once(type_, target, related_type, related_id, message):
    """Send `message` at most once for this (type, related_type, related_id,
    target) key. Safe to call repeatedly (e.g. from every /tick)."""
    key = f"{type_} {related_type}:{related_id} -> {target}"
    row = NotificationLog.query.filter_by(
        type=type_, target=target, related_type=related_type, related_id=related_id
    ).first()
    if row and row.sent_flag:
        # The single most confusing outcome — nothing sends and nothing errors.
        # Say so, with when it originally went, so "why didn't I get a message?"
        # is answerable from the log.
        current_app.logger.info(
            "notify SKIP (already sent %s) | %s", row.sent_at, key)
        return False

    if not row:
        row = NotificationLog(type=type_, target=target, related_type=related_type,
                               related_id=related_id, sent_flag=False, message=message)
        db.session.add(row)
        db.session.flush()
    else:
        current_app.logger.info("notify RETRY (previous attempt failed) | %s", key)
        row.message = message

    current_app.logger.info("notify SEND | %s | %r", key, message)
    try:
        get_backend().send(message)
    except Exception:
        # Swallowed, not re-raised. The row stays unsent, so the next /tick
        # retries it — that's the self-healing CLAUDE.md #13 asks for. Raising
        # instead meant one unreachable LINE call aborted the whole tick
        # (no-show checks, forgot-signout flags and auto-advertising never ran)
        # and 500'd whatever request triggered it, including a student simply
        # filing a leave request.
        current_app.logger.exception("notify FAIL (will retry next tick) | %s", key)
        db.session.commit()
        return False

    row.sent_at = local_now()
    row.sent_flag = True
    db.session.commit()
    current_app.logger.info("notify OK | %s", key)
    return True


def retry_failed(limit=25):
    """Re-send anything still marked unsent. Called from /tick.

    The trigger that composed a message fires once — a window boundary gets
    stamped, a no-show moment passes — so without this an unsent row would
    never be attempted again, and would keep blocking its own key forever.
    Rows created before `message` existed can't be replayed; they're dropped
    so they stop blocking."""
    rows = (NotificationLog.query
            .filter_by(sent_flag=False)
            .order_by(NotificationLog.id)
            .limit(limit).all())
    retried = sent = dropped = 0
    for row in rows:
        if not row.message:
            current_app.logger.warning(
                "notify DROP (no stored text, cannot retry) | %s %s:%s",
                row.type, row.related_type, row.related_id)
            db.session.delete(row)
            dropped += 1
            continue
        retried += 1
        if notify_once(row.type, row.target, row.related_type, row.related_id, row.message):
            sent += 1
    if dropped:
        db.session.commit()
    return {"notify_retried": retried, "notify_retry_sent": sent, "notify_dropped": dropped}


def reset_notification(type_, target, related_type, related_id):
    """Forget that a notification was sent, so it can fire again.

    Dedup is keyed for all time, which is right for "the roster was committed"
    but wrong for anything that can legitimately happen twice. Deliberately
    reopening selection is the case in point: the month announces itself the
    first time, then a later reopen is silently suppressed because the key
    already exists."""
    row = NotificationLog.query.filter_by(
        type=type_, target=target, related_type=related_type, related_id=related_id
    ).first()
    if row:
        db.session.delete(row)
        db.session.commit()
        return True
    return False


def notify_committed(month):
    return notify_once(
        "committed", "group", "month", month.id,
        f"[OIA] The duty roster for {month.year_month} has been committed. Check your shifts.",
    )


def notify_leave_requested(leave_request):
    """Fires the moment a student submits a leave request — before it's
    approved. Deliberately generic (no name, no reason): it flags the
    overseer to go review it, and primes students that a slot may open up
    soon, without exposing anything personal in the shared group."""
    slot = leave_request.slot
    return notify_once(
        "leave_requested", "group", "leave_request", leave_request.id,
        f"[OIA] A leave request came in for {slot.date.isoformat()} {slot.hour}:00 — pending review.",
    )


def notify_slot_open(reopened_slot):
    slot = reopened_slot.slot
    return notify_once(
        "slot_open", "group", "reopened_slot", reopened_slot.id,
        f"[OIA] A slot opened up: {slot.date.isoformat()} {slot.hour}:00. First come, first served.",
    )


def notify_selection_open(month):
    return notify_once(
        "selection_open", "group", "month", month.id,
        f"[OIA] Availability selection for {month.year_month} is now open.",
    )


def notify_closing_warning(month):
    return notify_once(
        "closing_warning", "group", "month", month.id,
        f"[OIA] Availability selection for {month.year_month} closes soon — submit your hours.",
    )


def notify_signed_in(session):
    """Toggle-gated (Advanced > sign-in/out notifications) since this can
    fire a lot on a busy day — every sign-in, not just once."""
    if not get_attendance_notify_enabled():
        # Said out loud: this is one of the two ways a notification vanishes
        # without any error, and it looks identical to a broken integration.
        current_app.logger.info(
            "notify OFF (Advanced > sign-in/out notifications is unticked) | "
            "signed_in attendance_session:%s", session.id)
        return False
    return notify_once(
        "signed_in", "group", "attendance_session", session.id,
        f"[OIA] {session.student.short_name} signed in.",
    )


def notify_signed_out(session):
    if not get_attendance_notify_enabled():
        current_app.logger.info(
            "notify OFF (Advanced > sign-in/out notifications is unticked) | "
            "signed_out attendance_session:%s", session.id)
        return False
    return notify_once(
        "signed_out", "group", "attendance_session", session.id,
        f"[OIA] {session.student.short_name} signed out.",
    )


def notify_no_show(slot, student):
    return notify_once(
        "no_show", "group", "slot", slot.id,
        f"[OIA] Reminder: {student.short_name} was scheduled {slot.date.isoformat()} "
        f"{slot.hour}:00 and hasn't signed in yet.",
    )
