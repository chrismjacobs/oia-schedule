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
from datetime import datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models import Month, SelectionWindow, Slot, Assignment, Schedule, AttendanceSession, ReopenedSlot
from app.notifications.service import notify_selection_open, notify_closing_warning, notify_no_show, notify_slot_open
from app.utils.tz import local_now


def _auto_advance_selection_windows(now):
    """Selection open/close times are config, not a button push — the window
    itself drives the month.state transition."""
    opened = closed = 0
    for month in Month.query.filter(Month.state.in_(("setup", "selection_open"))).all():
        sw = SelectionWindow.query.filter_by(month_id=month.id).first()
        if not sw:
            continue
        if month.state == "setup" and now >= sw.opens_at:
            month.state = "selection_open"
            db.session.commit()
            notify_selection_open(month)
            opened += 1
        if month.state == "selection_open":
            warn_at = sw.closes_at - timedelta(hours=current_app.config["CLOSING_WARNING_HOURS_BEFORE"])
            if now >= warn_at:
                notify_closing_warning(month)
            if now >= sw.closes_at:
                month.state = "selection_closed"
                db.session.commit()
                closed += 1
    return {"windows_opened": opened, "windows_closed": closed}


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

    checked = 0
    for a in assignments:
        slot = a.slot
        slot_start = datetime.combine(slot.date, datetime.min.time()).replace(hour=slot.hour)
        if now < slot_start + grace:
            continue
        session_covers = AttendanceSession.query.filter(
            AttendanceSession.student_id == a.student_id,
            AttendanceSession.date == slot.date,
            AttendanceSession.signed_in_at <= slot_start + grace,
        ).first()
        if session_covers:
            continue
        notify_no_show(slot, a.student)
        checked += 1
    return {"no_show_checked": checked}


def _flag_forgotten_signouts(now):
    """Forgot-to-sign-out is common: flag it, never auto-close at a guessed
    time (CLAUDE.md #8)."""
    cutoff = now - timedelta(hours=current_app.config["FORGOT_SIGNOUT_AFTER_HOURS"])
    open_sessions = AttendanceSession.query.filter(
        AttendanceSession.signed_out_at.is_(None),
        AttendanceSession.signed_in_at <= cutoff,
        AttendanceSession.flag_reason != "forgot_sign_out",
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
        notify_slot_open(reopened)
        advertised += 1
    if advertised:
        db.session.commit()
    return {"auto_advertised": advertised}


def run_tick():
    now = local_now()
    result = {"ran_at": now.isoformat()}
    result.update(_auto_advance_selection_windows(now))
    result.update(_check_no_shows(now))
    result.update(_flag_forgotten_signouts(now))
    result.update(_auto_advertise_unfilled_slots(now))
    return result
