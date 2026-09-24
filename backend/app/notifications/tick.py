"""Everything the external cron's /tick ping needs to do (CLAUDE.md #12).

Time-window-aware and idempotent: each call asks "what is now due and not
yet done?" rather than reacting to an instant trigger. A missed, late, or
doubled ping self-heals because every send is gated by notification_log's
sent_flag (see notifications/service.py).

All timestamps in this app are naive datetimes interpreted as the
deployment's local timezone (Asia/Taipei, per SCHEMA.md) — there is a single
tenant and no cross-timezone users, so this is simpler than threading tzinfo
through every column. See app.utils.tz.local_now() — never datetime.utcnow().
"""
from datetime import datetime, timedelta, date

from flask import current_app

from app.extensions import db
from app.models import (
    Month, SelectionWindow, Slot, Assignment, Schedule, AttendanceSession, SessionHour, ReopenedSlot,
    NotificationLog,
)
from app.notifications.service import (
    notify_selection_open, notify_closing_warning, notify_no_show_run, notify_slots_open,
    retry_failed,
)
from app.utils.runs import contiguous_runs
from app.utils.slot_sync import sync_month_slots, month_has_slots
from app.utils.tz import local_now


def _auto_advance_selection_windows(now):
    """Move month.state when a scheduled window boundary passes — each
    boundary at most once, recorded on the window itself.

    The once-only part matters: without it every tick re-applied the rule, so
    an overseer who deliberately reopened or closed a month by hand had it
    silently reverted on the next cron ping, with no way to win. The window
    schedules; the overseer decides. Saving new times clears the stamps, which
    is how you deliberately re-arm a boundary."""
    opened = closed = 0
    for sw in SelectionWindow.query.all():
        month = Month.query.get(sw.month_id)
        if not month:
            continue

        if sw.opened_applied_at is None and now >= sw.opens_at:
            current_app.logger.info(
                "window OPEN due | %s | opens_at=%s | month state=%s",
                month.year_month, sw.opens_at, month.state)
            sw.opened_applied_at = now
            # Only advance a month that hasn't already moved past this point.
            # A month someone already pushed to committed shouldn't be dragged
            # back to selection_open just because its opens_at rolled around.
            if month.state == "setup":
                # Never announce an open month students can't pick anything
                # in: if nobody built the slots, build them from the plan now.
                if not month_has_slots(month):
                    built = sync_month_slots(month)
                    current_app.logger.info(
                        "window OPEN | %s had no slots — built %d from the plan",
                        month.year_month, built["created"])
                month.state = "selection_open"
                db.session.commit()
                notify_selection_open(month)
                opened += 1
            else:
                db.session.commit()

        if month.state == "selection_open":
            warn_at = sw.closes_at - timedelta(hours=current_app.config["CLOSING_WARNING_HOURS_BEFORE"])
            if now >= warn_at:
                notify_closing_warning(month)

        if sw.closed_applied_at is None and now >= sw.closes_at:
            current_app.logger.info(
                "window CLOSE due | %s | closes_at=%s | month state=%s",
                month.year_month, sw.closes_at, month.state)
            sw.closed_applied_at = now
            if month.state == "selection_open":
                month.state = "selection_closed"
                closed += 1
            db.session.commit()

    return {"windows_opened": opened, "windows_closed": closed}


def _start_committed_months(now):
    """Move a committed month to running once its first day arrives.

    This used to be a manual click nobody remembered, and forgetting it fails
    silently in the worst way: everything looks normal — students sign in,
    leave works — but no-show detection only runs on months in "running", so
    the app's whole reason for existing is quietly off. Nothing about the
    transition needs a human, so it doesn't ask for one."""
    started = 0
    for month in Month.query.filter_by(state="committed").all():
        if not Schedule.query.filter_by(month_id=month.id, status="committed").first():
            continue
        year, mon = (int(x) for x in month.year_month.split("-"))
        if now.date() >= date(year, mon, 1):
            month.state = "running"
            started += 1
    if started:
        db.session.commit()
    return {"months_started": started}


def _signed_in_for(assignment):
    """Whether the student has signed in for the run this hour belongs to —
    at any time, late included. The reminder says "hasn't signed in yet", so
    once they have, it has nothing left to say (lateness shows on the
    dashboard instead).

    Tied to the run, not just "any session that day": a morning session
    must not silence the reminder for an afternoon block. A finished session
    has recorded its run's hours (SessionHour, written at sign-out); a
    session still open has recorded nothing yet, so check which run it's
    covering."""
    # Imported here: attendance.routes imports the notification service.
    from app.attendance.routes import _todays_assignments, _run_for_session

    slot = assignment.slot
    sessions = AttendanceSession.query.filter_by(
        student_id=assignment.student_id, date=slot.date).all()
    if not sessions:
        return False
    recorded = SessionHour.query.filter(
        SessionHour.slot_id == slot.id,
        SessionHour.session_id.in_([s.id for s in sessions]),
    ).first()
    if recorded:
        return True
    open_sessions = [s for s in sessions if s.signed_out_at is None]
    if open_sessions:
        day = _todays_assignments(assignment.student_id, slot.date)
        for s in open_sessions:
            if any(a.slot_id == slot.id for a in _run_for_session(day, s)):
                return True
    return False


def _check_no_shows(now):
    """Scheduled but not signed in, past slot start + grace (CLAUDE.md #8, #11)."""
    grace = timedelta(minutes=current_app.config["NO_SHOW_GRACE_MINUTES"])
    lookback_date = (now - timedelta(days=2)).date()

    running_month_ids = [m.id for m in Month.query.filter_by(state="running").all()]
    if not running_month_ids:
        return {"no_show_checked": 0}

    schedules = Schedule.query.filter(
        Schedule.month_id.in_(running_month_ids), Schedule.status == "committed"
    ).all()
    schedule_ids = [s.id for s in schedules]
    if not schedule_ids:
        return {"no_show_checked": 0}

    assignments = (
        Assignment.query.join(Slot, Assignment.slot_id == Slot.id)
        .filter(Assignment.schedule_id.in_(schedule_ids), Slot.date >= lookback_date, Slot.date <= now.date())
        .all()
    )

    current_app.logger.info(
        "no-show scan | running months=%s | assignments in window %s..%s: %d",
        running_month_ids, lookback_date, now.date(), len(assignments))

    # Grouped into runs before anything is judged: a missed four-hour morning
    # is one no-show, not four (CLAUDE.md #9 — sign-in is once per run), and
    # announcing it per hour is what emptied the LINE quota in September.
    by_student_day = {}
    for a in assignments:
        by_student_day.setdefault((a.student_id, a.slot.date), []).append(a)

    checked = 0
    for (_student_id, _day), group in by_student_day.items():
        for run in contiguous_runs(group, lambda a: a.slot.hour):
            first = run[0]
            run_start = datetime.combine(
                first.slot.date, datetime.min.time()).replace(hour=first.slot.hour)
            if now < run_start + grace:
                continue
            # Any hour of the run signed in means they turned up — a late
            # arrival covering 9:00-12:00 must not be announced as having
            # missed the whole 8:00-12:00 shift. Lateness is the dashboard's
            # to report, not the group's.
            if any(_signed_in_for(a) for a in run):
                continue
            # Named here as well as in the message, so a no-show for a student
            # who shouldn't be on the roster any more is traceable to its
            # assignments.
            current_app.logger.info(
                "no-show candidate | %s %s:00-%s:00 | student=%s (id=%s) | assignments=%s",
                first.slot.date, first.slot.hour, run[-1].slot.hour + 1,
                first.student.short_name if first.student else "MISSING",
                first.student_id, [a.id for a in run])
            notify_no_show_run([a.slot for a in run], first.student)
            checked += 1
    return {"no_show_checked": checked}


def _flag_forgotten_signouts(now):
    """Forgot-to-sign-out is common: flag it, never auto-close at a guessed
    time (CLAUDE.md #8)."""
    cutoff = now - timedelta(hours=current_app.config["FORGOT_SIGNOUT_AFTER_HOURS"])
    open_sessions = AttendanceSession.query.filter(
        AttendanceSession.signed_out_at.is_(None),
        AttendanceSession.signed_in_at <= cutoff,
        # NULL-safe: a plain session has flag_reason NULL, and in SQL
        # `NULL != 'forgot_sign_out'` is NULL, not true — written without
        # is_distinct_from this filter silently excluded every session that
        # had never been flagged, i.e. exactly the ones to flag.
        AttendanceSession.flag_reason.is_distinct_from("forgot_sign_out"),
    ).all()
    for s in open_sessions:
        s.flagged = True
        s.flag_reason = "forgot_sign_out"
    if open_sessions:
        db.session.commit()
    return {"forgot_signout_flagged": len(open_sessions)}


def _auto_advertise_unfilled_slots(now):
    """A committed slot nobody ever offered availability for stays uncovered
    forever unless someone acts — auto-open it for FCFS claim once its date
    is within ADVERTISE_LOOKAHEAD_DAYS, rather than waiting on the overseer
    to notice and click Advertise manually. Approved-leave reopens and manual
    advertises both create their own ReopenedSlot immediately, so this only
    ever needs to catch slots that were never touched by either path."""
    lookahead_date = now.date() + timedelta(days=current_app.config["ADVERTISE_LOOKAHEAD_DAYS"])
    month_ids = [m.id for m in Month.query.filter(Month.state.in_(("committed", "running"))).all()]
    if not month_ids:
        return {"auto_advertised": 0}

    schedules = Schedule.query.filter(Schedule.month_id.in_(month_ids), Schedule.status == "committed").all()
    schedule_ids = [s.id for s in schedules]
    if not schedule_ids:
        return {"auto_advertised": 0}

    assigned_slot_ids = {
        a.slot_id for a in Assignment.query.filter(Assignment.schedule_id.in_(schedule_ids)).all()
    }
    # Deliberately unfiltered: a retracted row counts as "already handled" too,
    # so an offer the overseer withdrew doesn't reappear on the next tick.
    already_reopened_slot_ids = {r.slot_id for r in ReopenedSlot.query.all()}

    candidates = Slot.query.filter(
        Slot.month_id.in_(month_ids), Slot.date >= now.date(), Slot.date <= lookahead_date,
    ).all()

    advertised = 0
    for slot in candidates:
        if slot.id in assigned_slot_ids or slot.id in already_reopened_slot_ids:
            continue
        slot.state = "reopened"
        reopened = ReopenedSlot(slot_id=slot.id, source="auto_unfilled", opened_at=now)
        db.session.add(reopened)
        db.session.flush()
        advertised += 1
    if advertised:
        db.session.commit()
    return {"auto_advertised": advertised}


def _announce_reopened_slots(now):
    """Announce newly opened hours, grouped into runs.

    Every reopen path — approved leave, a manual Advertise, the auto_unfilled
    sweep above — now creates its ReopenedSlot and leaves the announcing to
    this one stage, so all three "fire the same way" (CLAUDE.md #12) and a
    batch that arrives an hour at a time still goes out as a single message.
    An overseer approving a student's four-hour leave clicks four times; the
    group hears about it once.

    What this costs is up to one cron interval before an open shift is
    announced. FCFS can afford minutes, and it arguably improves it: the news
    stops going out to whoever happens to be looking at that exact second.
    """
    pending_rows = (
        ReopenedSlot.query.join(Slot, ReopenedSlot.slot_id == Slot.id)
        .filter(
            ReopenedSlot.claimed_by.is_(None),
            ReopenedSlot.retracted_at.is_(None),
            # Nobody can claim yesterday. An un-announced row for a past date
            # is stale, not news, and announcing it would waste a push.
            Slot.date >= now.date(),
        ).all()
    )
    if not pending_rows:
        return {"slot_open_announced": 0}

    # The notification_log row IS the announced-flag — including the covered
    # rows notify_slots_open writes for the tail of each run, which is what
    # stops the rest of a run being announced again one hour at a time.
    already_announced = {
        row.related_id for row in NotificationLog.query.filter_by(
            type="slot_open", related_type="reopened_slot").all()
    }
    pending = [r for r in pending_rows if r.id not in already_announced]
    if not pending:
        return {"slot_open_announced": 0}

    current_app.logger.info(
        "slot_open sweep | %d un-announced open slot(s): %s",
        len(pending), [r.id for r in pending])
    return {"slot_open_announced": notify_slots_open(pending)}


def run_tick():
    """Run every due check. Each stage is logged separately: a tick that does
    nothing and a tick that half-crashed used to look identical from outside,
    and a stage that throws shouldn't take the rest of the run down with it."""
    from app.utils.settings import set_setting, get_setting
    import time as _time

    now = local_now()
    previous = get_setting("last_tick_at")
    log = current_app.logger
    log.info("tick START | taipei_now=%s | previous_tick=%s", now.isoformat(), previous)

    result = {"ran_at": now.isoformat(), "previous_tick_at": previous}
    # Recorded so /api/health can say when the cron last got through. A tick
    # that silently never runs looks identical to one with nothing to do, and
    # that is exactly how a misconfigured ping URL goes unnoticed for days.
    set_setting("last_tick_at", now.isoformat())

    stages = (
        ("selection_windows", _auto_advance_selection_windows),
        ("start_months", _start_committed_months),
        ("no_shows", _check_no_shows),
        ("forgot_signouts", _flag_forgotten_signouts),
        ("advertise", _auto_advertise_unfilled_slots),
        # After advertise, so slots opened a moment ago by this same tick are
        # announced in the same run as ones opened by leave or by hand.
        ("announce_open", _announce_reopened_slots),
        # Last: anything the stages above failed to send gets another go,
        # including failures from previous ticks.
        ("notify_retry", lambda _now: retry_failed()),
    )
    errors = {}
    began = _time.monotonic()
    for name, fn in stages:
        t0 = _time.monotonic()
        try:
            out = fn(now)
            result.update(out)
            log.info("tick stage %-18s %sms | %s", name,
                     round((_time.monotonic() - t0) * 1000), out)
        except Exception as exc:
            # One broken stage must not cost the other four.
            db.session.rollback()
            errors[name] = f"{type(exc).__name__}: {exc}"
            log.exception("tick stage %s FAILED — continuing with the rest", name)

    result["errors"] = errors
    log.info("tick DONE in %sms | %s", round((_time.monotonic() - began) * 1000),
             {k: v for k, v in result.items() if k not in ("ran_at", "previous_tick_at")})
    return result
