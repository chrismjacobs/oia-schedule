"""Idempotent notification dispatch. Every notification is gated by a
sent-flag on notification_log so a missed/late/doubled /tick self-heals
(CLAUDE.md #12)."""
from flask import current_app

from app.extensions import db
from app.models import NotificationLog
from app.notifications.backends import get_backend
from app.utils.runs import contiguous_runs, span_text
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

    backend = get_backend()
    # via=<backend> on every line: "which channel did this actually go out
    # on?" is the first question whenever a message doesn't arrive.
    current_app.logger.info("notify SEND via=%s | %s | %r", backend.name, key, message)
    try:
        backend.send(message)
    except Exception:
        # Swallowed, not re-raised. The row stays unsent, so the next /tick
        # retries it — that's the self-healing CLAUDE.md #13 asks for. Raising
        # instead meant one unreachable LINE call aborted the whole tick
        # (no-show checks, forgot-signout flags and auto-advertising never ran)
        # and 500'd whatever request triggered it, including a student simply
        # filing a leave request.
        current_app.logger.exception("notify FAIL via=%s (will retry next tick) | %s", backend.name, key)
        db.session.commit()
        return False

    row.sent_at = local_now()
    row.sent_flag = True
    db.session.commit()
    current_app.logger.info("notify OK via=%s | %s", backend.name, key)
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


def _mark_covered(type_, target, related_type, related_ids, covered_by, message):
    """Record the other members of a run as already accounted for.

    One message covers a whole run and is keyed on the run's first member.
    Without a row for the rest, the next /tick would look at each of them in
    turn, find no notification against it, and re-announce the same news hour
    by hour — which is the per-hour flood this exists to stop.

    Written sent_flag=True so notify_once skips them and retry_failed leaves
    them alone, and named as covered in the text so the log still shows which
    message spoke for them. Nothing is ever pushed for these rows.
    """
    for related_id in related_ids:
        _record_silent(type_, target, related_type, related_id,
                       f"[covered by {related_type}:{covered_by}] {message}")
    db.session.commit()


def _record_silent(type_, target, related_type, related_id, message):
    """Write a notification_log row that stands for a message nobody will be
    sent — already spoken for by another, or deliberately withheld. Nothing
    is pushed; the row exists so the key is taken."""
    existing = NotificationLog.query.filter_by(
        type=type_, target=target, related_type=related_type, related_id=related_id
    ).first()
    if existing:
        return False
    db.session.add(NotificationLog(
        type=type_, target=target, related_type=related_type, related_id=related_id,
        sent_flag=True, sent_at=local_now(), message=message,
    ))
    return True


def suppress_slot_open(reopened_slot):
    """Open a slot without announcing it (advertise announce=false).

    The tick's sweep reads "no slot_open row" as "not announced yet", so a
    deliberately quiet reopen has to leave one behind — or the next tick
    would helpfully broadcast the very thing the overseer chose not to."""
    written = _record_silent(
        "slot_open", "group", "reopened_slot", reopened_slot.id,
        "[not announced: opened quietly by the overseer]")
    db.session.commit()
    return written


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


def notify_leave_requested(leave_requests):
    """Fires the moment a student submits a leave request — before it's
    approved. Deliberately generic (no name, no reason): it flags the
    overseer to go review it, and primes students that a slot may open up
    soon, without exposing anything personal in the shared group.

    Takes the whole batch from one submission (a single hour is a batch of
    one) and sends exactly one message describing the span. A student asking
    off for a four-hour shift is one piece of news, not four — and four
    pushes for it is four off a LINE free-tier quota of 200 a month.

    Keyed on the first request's id, so a later batch for the same student and
    day is a separate notification, while a retry of this one is not.
    """
    requests = list(leave_requests)
    if not requests:
        return False
    slots = sorted((r.slot for r in requests), key=lambda s: (s.date, s.hour))
    first, last = slots[0], slots[-1]
    if len(slots) == 1:
        span = f"{first.date.isoformat()} {first.hour}:00"
    elif first.date == last.date:
        span = f"{first.date.isoformat()} {first.hour}:00-{last.hour + 1}:00 ({len(slots)} hours)"
    else:
        span = (f"{first.date.isoformat()} {first.hour}:00 to "
                f"{last.date.isoformat()} {last.hour + 1}:00 ({len(slots)} hours)")
    return notify_once(
        "leave_requested", "group", "leave_request", requests[0].id,
        f"[OIA] A leave request came in for {span} — pending review.",
    )


def notify_slots_open(reopened_slots):
    """One message per contiguous run of newly opened hours.

    Advertising an uncovered morning used to be four separate pushes, one per
    hour, because a ReopenedSlot is per-hour and every one of them announced
    itself. A group push costs one message per member of the group, so a
    four-hour morning to eleven students was 44 of a 200/month quota — the
    single thing that exhausted it in September.

    Keyed on the run's first ReopenedSlot; the rest are marked covered so a
    later sweep doesn't announce them again individually.
    """
    rows = [r for r in reopened_slots if r is not None and r.slot is not None]
    if not rows:
        return 0

    by_day = {}
    for r in rows:
        by_day.setdefault(r.slot.date, []).append(r)

    sent = 0
    for _day, day_rows in sorted(by_day.items()):
        for run in contiguous_runs(day_rows, lambda r: r.slot.hour):
            first = run[0]
            message = (f"[OIA] A slot opened up: {span_text([r.slot for r in run])}. "
                       f"First come, first served.")
            if notify_once("slot_open", "group", "reopened_slot", first.id, message):
                sent += 1
            _mark_covered("slot_open", "group", "reopened_slot",
                          [r.id for r in run[1:]], first.id, message)
    return sent


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
    # A late sign-out lands days after the shift; announcing it as a plain
    # "signed out" reads as if they were still in the office just now.
    if session.flag_reason == "late_sign_out":
        text = (f"[OIA] {session.student.short_name} signed out late for "
                f"{session.date.isoformat()}.")
    else:
        text = f"[OIA] {session.student.short_name} signed out."
    return notify_once(
        "signed_out", "group", "attendance_session", session.id, text,
    )


def notify_no_show_run(slots, student):
    """One reminder per missed run, not per missed hour.

    A student who misses a four-hour morning has had one no-show, and the
    group hearing about it four times is both noise and four times the quota
    (see notify_slots_open). The run is also the honest unit: sign-in is once
    per run (CLAUDE.md #9), so "hasn't signed in" is a fact about the run.

    Keyed on the run's first slot, with the rest marked covered — otherwise
    every later /tick would re-check the untouched hours and announce them.
    """
    ordered = sorted(slots, key=lambda s: s.hour)
    if not ordered:
        return False
    first = ordered[0]
    message = (f"[OIA] Reminder: {student.short_name} was scheduled "
               f"{span_text(ordered)} and hasn't signed in yet.")
    sent = notify_once("no_show", "group", "slot", first.id, message)
    _mark_covered("no_show", "group", "slot",
                  [s.id for s in ordered[1:]], first.id, message)
    return sent
