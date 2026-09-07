"""The overseer dashboard — the centre of gravity (CLAUDE.md #1, #15).
Surfaces: scheduled-vs-recorded gaps, no-shows, leave patterns, uncovered
slots, task completion. All values here are derived, never stored
(SCHEMA.md 'Derived values')."""
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import jsonify, request, current_app

from app.dashboard import bp
from app.models import (
    Month, Schedule, Assignment, Slot, Student, SessionHour, AttendanceSession,
    LeaveRequest, RegularTask, TaskCompletion, CustomTask, ReopenedSlot, ClosedDate,
)
from app.utils.decorators import overseer_required
from app.utils.periods import weekdays_in_month
from app.utils.tz import local_now, local_today


def _committed_schedule(month_id):
    return (Schedule.query.filter_by(month_id=month_id, status="committed")
            .order_by(Schedule.generated_at.desc()).first())


def build_month_dashboard(month):
    schedule = _committed_schedule(month.id)
    today = local_today()

    students = {s.id: s for s in Student.query.filter_by(is_active=True).all()}
    slots = Slot.query.filter_by(month_id=month.id).all()
    slot_by_id = {s.id: s for s in slots}

    assignments = []
    assigned_pairs = set()  # (student_id, slot_id)
    if schedule:
        assignments = Assignment.query.filter_by(schedule_id=schedule.id).all()
        assigned_pairs = {(a.student_id, a.slot_id) for a in assignments}

    scheduled_hours = defaultdict(int)
    for a in assignments:
        scheduled_hours[a.student_id] += 1

    recorded = (
        SessionHour.query.join(AttendanceSession, SessionHour.session_id == AttendanceSession.id)
        .join(Slot, SessionHour.slot_id == Slot.id)
        .filter(Slot.month_id == month.id)
        .all()
    )
    recorded_hours = defaultdict(int)
    reported_pairs = set()  # (student_id, slot_id)
    for sh in recorded:
        student_id = sh.session.student_id
        recorded_hours[student_id] += 1
        reported_pairs.add((student_id, sh.slot_id))

    all_student_ids = set(students.keys()) | set(scheduled_hours.keys()) | set(recorded_hours.keys())
    gap_rows = []
    for sid in all_student_ids:
        sched = scheduled_hours.get(sid, 0)
        rec = recorded_hours.get(sid, 0)
        gap_rows.append({
            "student_id": sid,
            "student": students[sid].to_dict() if sid in students else None,
            "scheduled_hours": sched,
            "recorded_hours": rec,
            "gap": sched - rec,
        })
    gap_rows.sort(key=lambda r: -abs(r["gap"]))

    no_shows = []
    for a in assignments:
        slot = slot_by_id[a.slot_id]
        if slot.date >= today:
            continue
        if (a.student_id, a.slot_id) in reported_pairs:
            continue
        no_shows.append({"student_id": a.student_id, "slot": slot.to_dict()})

    signed_in_not_scheduled = []
    for (sid, slot_id) in reported_pairs:
        if (sid, slot_id) not in assigned_pairs and slot_id in slot_by_id:
            signed_in_not_scheduled.append({"student_id": sid, "slot": slot_by_id[slot_id].to_dict()})

    leave_rows = (
        LeaveRequest.query.join(Slot, LeaveRequest.slot_id == Slot.id)
        .filter(Slot.month_id == month.id).all()
    )
    approved_count = defaultdict(int)
    for lr in leave_rows:
        if lr.status == "approved":
            approved_count[lr.student_id] += 1
    too_often_threshold = current_app.config["LEAVE_TOO_OFTEN_COUNT"]
    too_late_threshold = current_app.config["LEAVE_TOO_LATE_HOURS"]
    too_often = [{"student_id": sid, "count": c} for sid, c in approved_count.items() if c >= too_often_threshold]
    too_late = [
        {"student_id": lr.student_id, "leave_request": lr.to_dict()}
        for lr in leave_rows if lr.lead_time_hours is not None and lr.lead_time_hours < too_late_threshold
    ]

    assigned_slot_ids = {a.slot_id for a in assignments}
    uncovered = [s.to_dict() for s in slots if s.id not in assigned_slot_ids]

    coverage_pct = round(100.0 * len(assignments) / len(slots), 1) if slots else 0.0

    year, month_no = (int(x) for x in month.year_month.split("-"))
    month_start = date(year, month_no, 1)
    month_end = date(year + (month_no == 12), (month_no % 12) + 1, 1)
    regular_completions = (
        TaskCompletion.query.join(AttendanceSession, TaskCompletion.session_id == AttendanceSession.id)
        .filter(AttendanceSession.date >= month_start, AttendanceSession.date < month_end).all()
    )
    completions_by_task = defaultdict(int)
    for tc in regular_completions:
        completions_by_task[tc.regular_task_id] += 1
    regular_task_summary = [
        {"task": t.to_dict(), "completions_this_month": completions_by_task.get(t.id, 0)}
        for t in RegularTask.query.filter_by(is_active=True).all()
    ]
    custom_task_counts = defaultdict(int)
    for ct in CustomTask.query.all():
        custom_task_counts[ct.status] += 1

    return {
        "month": month.to_dict(),
        "schedule": schedule.to_dict() if schedule else None,
        "gap": gap_rows,
        "no_shows": no_shows,
        "signed_in_not_scheduled": signed_in_not_scheduled,
        "leave_patterns": {"too_often": too_often, "too_late": too_late},
        "uncovered_slots": uncovered,
        "coverage_pct": coverage_pct,
        "task_completion": {"regular": regular_task_summary, "custom_by_status": dict(custom_task_counts)},
    }


@bp.get("/months/<int:month_id>")
@overseer_required
def month_dashboard(month_id):
    month = Month.query.get_or_404(month_id)
    return jsonify(build_month_dashboard(month))


def _slot_status_rows(slots):
    """Per-slot status the overseer's schedule grid needs: who's assigned,
    whether they've actually shown up, whether an uncovered slot is already
    advertised, and whether someone has asked for leave against it that
    hasn't been decided yet. Shared by the month grid below — used to be
    day-view-only, generalised so the merged Day+Week view can show the same
    status for every slot in a month, not just one day."""
    if not slots:
        return []

    schedule = _committed_schedule(slots[0].month_id)
    students = {s.id: s.to_dict() for s in Student.query.all()}
    slot_ids = [s.id for s in slots]

    assignment_by_slot = {}
    if schedule:
        for a in Assignment.query.filter(
            Assignment.schedule_id == schedule.id, Assignment.slot_id.in_(slot_ids)
        ).all():
            assignment_by_slot[a.slot_id] = a

    # One report per session (CLAUDE.md #9), so every hour of a run carries
    # that session's write-up and its ticked tasks — the work crosses the
    # hour boundaries, and splitting it per cell would invent detail nobody
    # entered.
    recorded = SessionHour.query.filter(SessionHour.slot_id.in_(slot_ids)).all()
    session_by_slot = {sh.slot_id: sh.session for sh in recorded}
    session_ids = {sh.session_id for sh in recorded}

    tasks_by_session = {}
    if session_ids:
        for tc in TaskCompletion.query.filter(TaskCompletion.session_id.in_(session_ids)).all():
            tasks_by_session.setdefault(tc.session_id, {"regular": [], "custom": []})["regular"].append(
                tc.regular_task.to_dict())
        for ct in CustomTask.query.filter(CustomTask.session_id.in_(session_ids)).all():
            tasks_by_session.setdefault(ct.session_id, {"regular": [], "custom": []})["custom"].append(
                ct.to_dict())

    grace = timedelta(minutes=current_app.config["NO_SHOW_GRACE_MINUTES"])
    now = local_now()

    open_reopens = {
        r.slot_id: r for r in ReopenedSlot.query.filter(
            ReopenedSlot.slot_id.in_(slot_ids),
            ReopenedSlot.claimed_by.is_(None),
            ReopenedSlot.retracted_at.is_(None),
        ).all()
    }

    # Leave asked for but not yet decided — the overseer needs to see it on the
    # grid, not only on the Leave page: a slot that is about to free up reads
    # very differently from one that's settled, and it's the same glance that
    # tells them whether it will need advertising.
    pending_leave = {
        lr.slot_id: lr for lr in LeaveRequest.query.filter(
            LeaveRequest.slot_id.in_(slot_ids), LeaveRequest.status == "pending"
        ).all()
    }

    rows = []
    for slot in slots:
        a = assignment_by_slot.get(slot.id)
        session = session_by_slot.get(slot.id)
        status = "uncovered"
        if a:
            if session:
                status = "recorded"
            else:
                # No-show = assignment for a past slot with no covering session
                # (SCHEMA.md 'Derived values') — independent of the session's
                # own forgot-to-sign-out flag, which is a different condition.
                slot_start = datetime.combine(slot.date, datetime.min.time()).replace(hour=slot.hour)
                status = "flagged" if now >= slot_start + grace else "scheduled"
        reopened = open_reopens.get(slot.id)
        lr = pending_leave.get(slot.id)
        rows.append({
            "slot": slot.to_dict(),
            "assignment": a.to_dict() if a else None,
            "student": students.get(a.student_id) if a else None,
            "status": status,
            "advertised": reopened is not None,
            "reopened_id": reopened.id if reopened else None,
            "pending_leave": {
                "id": lr.id,
                "student": students.get(lr.student_id),
                "reason": lr.reason,
                "lead_time_hours": lr.lead_time_hours,
            } if lr else None,
            "note": session.note if session else None,
            "tasks_done": tasks_by_session.get(session.id) if session else None,
        })
    return rows


@bp.get("/months/<int:month_id>/grid")
@overseer_required
def month_grid(month_id):
    """The merged Day+Week schedule view: every slot in the month with live
    status (scheduled/recorded/no-show/uncovered/advertised/leave-pending),
    for the overseer's week-by-week grid — not just the assignment, like
    /api/schedule/months/<id> gives.

    `dates` is the month's whole working calendar, not just the days that
    happen to have slots. An hour the regular schedule marked unavailable
    never gets a Slot generated, but it can still turn out to need cover, so
    the grid draws every working day and every hour and lets the overseer
    advertise any empty cell — /api/leave/advertise makes the Slot on
    demand."""
    month = Month.query.get_or_404(month_id)
    slots = Slot.query.filter_by(month_id=month_id).order_by(Slot.date, Slot.hour).all()
    closed = {c.date for c in ClosedDate.query.filter_by(month_id=month_id).all()}
    dates = [d.isoformat() for d in weekdays_in_month(month.year_month) if d not in closed]
    return jsonify({
        "month_id": month_id,
        "students": {s.id: s.to_dict() for s in Student.query.filter_by(is_active=True).all()},
        "dates": dates,
        "slots": _slot_status_rows(slots),
    })
