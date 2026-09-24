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
from app.notifications.service import suppress_slot_open, notify_leave_requested
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


LIVE_LEAVE_STATUSES = ("pending", "approved")


def _my_scheduled_slots(slot_ids):
    """The subset of `slot_ids` the logged-in student is actually on, per the
    committed schedule. Leave can only be taken from a shift you hold."""
    rows = (
        Assignment.query.join(Schedule, Assignment.schedule_id == Schedule.id)
        .filter(Assignment.slot_id.in_(slot_ids),
                Assignment.student_id == current_user.student_id,
                Schedule.status == "committed")
        .all()
    )
    return {a.slot_id for a in rows}


def _requested_leave_slots(slot_ids):
    """Slots among `slot_ids` this student already has a live (pending or
    approved) request against — so re-submitting an overlapping range doesn't
    stack duplicates in the overseer's queue. Withdrawn rows don't count; the
    student is free to ask again."""
    rows = LeaveRequest.query.filter(
        LeaveRequest.slot_id.in_(slot_ids),
        LeaveRequest.student_id == current_user.student_id,
        LeaveRequest.status.in_(LIVE_LEAVE_STATUSES),
    ).all()
    return {r.slot_id for r in rows}


def _leave_slots_from_request(data):
    """The slots one submission is asking off, as (slots, error_response).

    Two shapes, because leave is asked for in shifts but administered by the
    hour (CLAUDE.md #8):

      {"slot_id": 417}                                   - one hour
      {"date": "2026-09-24", "start_hour": 8, "end_hour": 12}  - a run

    `end_hour` is exclusive, so 8->12 reads as "08:00 to 12:00" and covers
    hours 8, 9, 10 and 11. The lunch gap is handled by SLOT_HOURS, so 8->17
    is the whole day and never invents a 12:00 slot.
    """
    slot_id = data.get("slot_id")
    if slot_id:
        return [Slot.query.get_or_404(slot_id)], None

    date_str = data.get("date")
    start_hour = data.get("start_hour")
    end_hour = data.get("end_hour")
    if not date_str or start_hour is None or end_hour is None:
        return None, (jsonify({"error": "slot_id_or_date_range_required"}), 400)
    try:
        d = date_cls.fromisoformat(date_str)
        start_hour = int(start_hour)
        end_hour = int(end_hour)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "invalid_date_or_hour"}), 400)
    if end_hour <= start_hour:
        return None, (jsonify({"error": "end_must_be_after_start"}), 400)

    hours = [h for h in SLOT_HOURS if start_hour <= h < end_hour]
    if not hours:
        return None, (jsonify({"error": "no_working_hours_in_range"}), 400)
    slots = Slot.query.filter(Slot.date == d, Slot.hour.in_(hours)).all()
    return slots, None


@bp.post("")
@login_required_api
def request_leave():
    """Ask off a single hour or a whole run of them in one go.

    The student picks a shift ("Wednesday, 08:00 to 12:00") because that is
    how they think about it, but what gets stored is still one LeaveRequest
    per hour: the overseer approves and advertises hour by hour, and an hour
    is the unit an Open Shifts claim is made in. So the range fans out here
    and nowhere downstream changes.

    Hours in the range that the student isn't scheduled for, or has already
    asked off, are skipped rather than failing the whole submission - a
    student picking "the whole day" shouldn't have to know which hours those
    are. The response reports what was created and what was skipped.
    """
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    data = request.get_json(force=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "missing_fields"}), 400

    slots, err = _leave_slots_from_request(data)
    if err:
        return err
    if not slots:
        return jsonify({"error": "no_slots_in_range"}), 400

    slot_ids = [s.id for s in slots]
    scheduled = _my_scheduled_slots(slot_ids)
    already = _requested_leave_slots(slot_ids)
    wanted = sorted((s for s in slots if s.id in scheduled and s.id not in already),
                    key=lambda s: (s.date, s.hour))
    if not wanted:
        # Distinguish the two dead ends: "you're not on that shift" and
        # "you already asked" need different things done about them.
        if already:
            return jsonify({"error": "already_requested"}), 409
        return jsonify({"error": "not_scheduled_for_slot"}), 400

    # One timestamp for the whole batch: it's the key the student's own list
    # groups on to show "08:00-12:00" as one line with one Withdraw button.
    now = local_now()
    created = []
    for slot in wanted:
        lead_hours = (_slot_start(slot) - now).total_seconds() / 3600.0
        lr = LeaveRequest(student_id=current_user.student_id, slot_id=slot.id, reason=reason,
                          requested_at=now, lead_time_hours=round(lead_hours, 2))
        db.session.add(lr)
        created.append(lr)
    db.session.commit()
    notify_leave_requested(created)
    return jsonify({
        "requests": [lr.to_dict() for lr in created],
        "created": len(created),
        "skipped_not_scheduled": len([s for s in slots if s.id not in scheduled]),
        "skipped_already_requested": len([s for s in slots if s.id in already]),
    }), 201


@bp.delete("/<int:leave_id>")
@login_required_api
def withdraw_leave(leave_id):
    """Take back your own request - the mis-click undo.

    Only while it's still pending. Once the overseer has approved it the
    assignment is gone and the hour may already be advertised or claimed by
    someone else, so un-asking is no longer the student's to do alone; they
    have to talk to the overseer, and the error says so.

    The row is marked `withdrawn`, not deleted: the overseer's pending queue
    filters on status so it drops off there immediately, while the history of
    what was asked for stays intact.
    """
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    lr = LeaveRequest.query.get_or_404(leave_id)
    if lr.student_id != current_user.student_id:
        return jsonify({"error": "not_your_request"}), 403
    if lr.status == "withdrawn":
        return jsonify({"error": "already_withdrawn"}), 409
    if lr.status != "pending":
        return jsonify({"error": "already_decided",
                        "message": "This leave was already approved — ask the overseer to put you back on the shift."}), 409

    lr.status = "withdrawn"
    lr.decided_at = local_now()
    db.session.commit()
    return jsonify(lr.to_dict())


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
    # Announcing is /tick's job (_announce_reopened_slots): approving a
    # four-hour leave is four clicks here, and the group should hear one
    # message about the whole run rather than one per hour.
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
    slot_id or a bare (month_id, date, hour) for a cell with no Slot yet.

    announce=false opens it quietly: on the Open Shifts board, claimable as
    usual, but no LINE message — for when the overseer already knows who'll
    take it (e.g. the original student is working after all) and a group
    broadcast would only invite a race for it."""
    data = request.get_json(force=True) or {}
    announce = bool(data.get("announce", True))
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
    if not announce:
        # Take the key so /tick's sweep doesn't announce what was opened
        # quietly on purpose.
        suppress_slot_open(reopened)
    return jsonify(dict(_open_slot_payload(reopened), announced=announce)), 201


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
