from datetime import date as date_cls

from flask import jsonify, request
from flask_login import current_user

from app.leave import bp
from app.extensions import db
from app.models import (
    LeaveRequest, Slot, Assignment, Schedule, ReopenedSlot, Student, Month, SLOT_HOURS,
)
from app.utils.decorators import login_required_api, overseer_required
from app.attendance.routes import _slot_start
from app.notifications.service import notify_slot_open, notify_leave_requested
from app.utils.tz import local_now


@bp.get("")
@login_required_api
def my_leave_requests():
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    rows = LeaveRequest.query.filter_by(student_id=current_user.student_id).order_by(
        LeaveRequest.requested_at.desc()
    ).all()
    return jsonify([r.to_dict() for r in rows])


@bp.post("")
@login_required_api
def request_leave():
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    data = request.get_json(force=True) or {}
    slot_id = data.get("slot_id")
    reason = (data.get("reason") or "").strip()
    if not slot_id or not reason:
        return jsonify({"error": "missing_fields"}), 400

    slot = Slot.query.get_or_404(slot_id)
    assignment = (
        Assignment.query.join(Schedule, Assignment.schedule_id == Schedule.id)
        .filter(Assignment.slot_id == slot_id, Assignment.student_id == current_user.student_id,
                Schedule.status == "committed")
        .first()
    )
    if not assignment:
        return jsonify({"error": "not_scheduled_for_slot"}), 400

    now = local_now()
    lead_hours = (_slot_start(slot) - now).total_seconds() / 3600.0

    lr = LeaveRequest(student_id=current_user.student_id, slot_id=slot_id, reason=reason,
                       requested_at=now, lead_time_hours=round(lead_hours, 2))
    db.session.add(lr)
    db.session.commit()
    notify_leave_requested(lr)
    return jsonify(lr.to_dict()), 201


@bp.get("/admin")
@overseer_required
def admin_list():
    status = request.args.get("status")
    q = LeaveRequest.query
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(LeaveRequest.requested_at.desc()).all()
    out = []
    for r in rows:
        d = r.to_dict()
        d["student"] = r.student.to_dict()
        d["slot"] = r.slot.to_dict()
        out.append(d)
    return jsonify(out)


@bp.patch("/admin/<int:leave_id>")
@overseer_required
def decide_leave(leave_id):
    """Approve a leave request. Denying isn't a decision the overseer gets to
    make — a student who can't come, can't come — so the only choice here is
    whether the freed hour goes straight onto the Open Shifts board.
    "advertise" defaults to true (the old always-on behaviour); pass false to
    release the student from the shift while leaving the hour uncovered, and
    advertise it later from the schedule grid if wanted."""
    lr = LeaveRequest.query.get_or_404(leave_id)
    data = request.get_json(force=True) or {}
    decision = data.get("status", "approved")
    if decision != "approved":
        return jsonify({"error": "invalid_status"}), 400
    if lr.status != "pending":
        return jsonify({"error": "already_decided"}), 409
    advertise = bool(data.get("advertise", True))

    lr.status = "approved"
    lr.decided_by = current_user.id
    lr.decided_at = local_now()

    assignment = (
        Assignment.query.join(Schedule, Assignment.schedule_id == Schedule.id)
        .filter(Assignment.slot_id == lr.slot_id, Assignment.student_id == lr.student_id,
                Schedule.status == "committed")
        .first()
    )
    if assignment:
        db.session.delete(assignment)

    if not advertise:
        lr.slot.state = "open"
        db.session.commit()
        return jsonify(lr.to_dict())

    lr.slot.state = "reopened"
    reopened = ReopenedSlot(slot_id=lr.slot_id, leave_request_id=lr.id, source="leave", opened_at=local_now())
    db.session.add(reopened)
    db.session.flush()
    db.session.commit()
    notify_slot_open(reopened)
    return jsonify(lr.to_dict())


def _open_slot_payload(r):
    d = r.to_dict()
    d["slot"] = r.slot.to_dict()
    return d


@bp.get("/reopened")
@login_required_api
def list_reopened():
    """Everything currently open for claim — the Open Shifts page. Any active
    student is eligible for any of these (deliberately no availability
    restriction — see CLAUDE.md #7's FCFS-only-for-reopens design)."""
    rows = (ReopenedSlot.query
            .filter(ReopenedSlot.claimed_by.is_(None), ReopenedSlot.retracted_at.is_(None))
            .order_by(ReopenedSlot.opened_at.desc()).all())
    return jsonify([_open_slot_payload(r) for r in rows])


def _resolve_or_create_slot(data):
    """Find the Slot the overseer is pointing at, creating it if it doesn't
    exist. Any hour of the working week can turn out to need cover — an hour
    the regular schedule marked "unavailable" (so slot generation skipped it)
    is exactly the case where an unplanned need shows up. So the grid offers
    Advertise on every unfilled cell, and a cell with no Slot behind it gets
    one made here on demand. Returns (slot, error_response)."""
    slot_id = data.get("slot_id")
    if slot_id:
        return Slot.query.get_or_404(slot_id), None

    month_id = data.get("month_id")
    date_str = data.get("date")
    hour = data.get("hour")
    if not month_id or not date_str or hour is None:
        return None, (jsonify({"error": "slot_id_or_month_date_hour_required"}), 400)
    try:
        hour = int(hour)
        d = date_cls.fromisoformat(date_str)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "invalid_date_or_hour"}), 400)
    if hour not in SLOT_HOURS:
        return None, (jsonify({"error": "invalid_hour"}), 400)

    month = Month.query.get_or_404(month_id)
    slot = Slot.query.filter_by(date=d, hour=hour).first()
    if slot:
        return slot, None

    slot = Slot(month_id=month.id, date=d, hour=hour,
                period="morning" if hour < 12 else "afternoon", state="open")
    db.session.add(slot)
    db.session.flush()
    return slot, None


@bp.post("/advertise")
@overseer_required
def advertise_slot():
    """Manually open a slot for FCFS claim — the off-the-books case: leave
    taken without going through a LeaveRequest, an hour that was never meant
    to be staffed but now needs someone, or pushing a never-filled slot live
    now instead of waiting for /tick's lookahead window. Takes either a
    slot_id or a bare (month_id, date, hour) for a cell with no Slot yet."""
    data = request.get_json(force=True) or {}
    slot, err = _resolve_or_create_slot(data)
    if err:
        return err

    if ReopenedSlot.query.filter(
        ReopenedSlot.slot_id == slot.id,
        ReopenedSlot.claimed_by.is_(None),
        ReopenedSlot.retracted_at.is_(None),
    ).first():
        return jsonify({"error": "already_advertised"}), 409

    schedule = (
        Schedule.query.filter_by(month_id=slot.month_id, status="committed")
        .order_by(Schedule.generated_at.desc()).first()
    )
    if not schedule:
        db.session.rollback()
        return jsonify({"error": "no_committed_schedule"}), 409

    assignment = Assignment.query.filter_by(schedule_id=schedule.id, slot_id=slot.id).first()
    if assignment:
        db.session.delete(assignment)

    slot.state = "reopened"
    reopened = ReopenedSlot(slot_id=slot.id, source="manual", opened_at=local_now())
    db.session.add(reopened)
    db.session.flush()
    db.session.commit()
    notify_slot_open(reopened)
    return jsonify(_open_slot_payload(reopened)), 201


@bp.delete("/reopened/<int:reopened_id>")
@overseer_required
def retract_reopened(reopened_id):
    """Pull an open shift back off the board — advertised by mistake, or the
    need went away. Only while it's still unclaimed: once someone has taken
    it they're scheduled, and that's an assignment to edit on the grid, not
    an offer to withdraw. The hour simply goes back to uncovered; an approved
    leave request behind it stays approved (the student is still off), it
    just isn't being offered around any more.

    Stamped, not deleted: /tick's auto-advertise skips slots that already have
    a reopened_slot row, so the tombstone is what keeps it from putting the
    withdrawn hour straight back on the board an hour later."""
    reopened = ReopenedSlot.query.get_or_404(reopened_id)
    if reopened.claimed_by:
        return jsonify({"error": "already_claimed"}), 409
    if reopened.retracted_at:
        return jsonify({"error": "already_retracted"}), 409

    slot = reopened.slot
    reopened.retracted_at = local_now()

    schedule = (
        Schedule.query.filter_by(month_id=slot.month_id, status="committed")
        .order_by(Schedule.generated_at.desc()).first()
    )
    still_assigned = bool(
        schedule and Assignment.query.filter_by(schedule_id=schedule.id, slot_id=slot.id).first()
    )
    slot.state = "assigned" if still_assigned else "open"
    db.session.commit()
    return jsonify({"ok": True, "slot_id": slot.id})


@bp.post("/reopened/<int:reopened_id>/claim")
@login_required_api
def claim_reopened(reopened_id):
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403

    now = local_now()
    updated = (
        db.session.query(ReopenedSlot)
        .filter(ReopenedSlot.id == reopened_id, ReopenedSlot.claimed_by.is_(None),
                ReopenedSlot.retracted_at.is_(None))
        .update({"claimed_by": current_user.student_id, "claimed_at": now}, synchronize_session=False)
    )
    if updated == 0:
        db.session.rollback()
        return jsonify({"error": "already_claimed_or_not_found"}), 409
    db.session.commit()

    reopened = ReopenedSlot.query.get(reopened_id)
    schedule = (
        Schedule.query.filter_by(month_id=reopened.slot.month_id, status="committed")
        .order_by(Schedule.generated_at.desc()).first()
    )
    a = Assignment(schedule_id=schedule.id, slot_id=reopened.slot_id,
                    student_id=current_user.student_id, source="claimed", created_at=now)
    db.session.add(a)
    reopened.slot.state = "assigned"
    db.session.commit()
    return jsonify(a.to_dict())
