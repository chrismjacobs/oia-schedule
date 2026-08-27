from datetime import datetime

from flask import jsonify, request
from flask_login import current_user

from app.leave import bp
from app.extensions import db
from app.models import LeaveRequest, Slot, Assignment, Schedule, ReopenedSlot, Student
from app.utils.decorators import login_required_api, overseer_required
from app.attendance.routes import _slot_start
from app.notifications.service import notify_slot_open


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

    now = datetime.utcnow()
    lead_hours = (_slot_start(slot) - now).total_seconds() / 3600.0

    lr = LeaveRequest(student_id=current_user.student_id, slot_id=slot_id, reason=reason,
                       requested_at=now, lead_time_hours=round(lead_hours, 2))
    db.session.add(lr)
    db.session.commit()
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
    lr = LeaveRequest.query.get_or_404(leave_id)
    data = request.get_json(force=True) or {}
    decision = data.get("status")
    if decision not in ("approved", "denied"):
        return jsonify({"error": "invalid_status"}), 400
    if lr.status != "pending":
        return jsonify({"error": "already_decided"}), 409

    lr.status = decision
    lr.decided_by = current_user.id
    lr.decided_at = datetime.utcnow()

    if decision == "approved":
        assignment = (
            Assignment.query.join(Schedule, Assignment.schedule_id == Schedule.id)
            .filter(Assignment.slot_id == lr.slot_id, Assignment.student_id == lr.student_id,
                    Schedule.status == "committed")
            .first()
        )
        if assignment:
            db.session.delete(assignment)
        lr.slot.state = "reopened"
        reopened = ReopenedSlot(slot_id=lr.slot_id, leave_request_id=lr.id, opened_at=datetime.utcnow())
        db.session.add(reopened)
        db.session.flush()
        db.session.commit()
        notify_slot_open(reopened)
        return jsonify(lr.to_dict())

    db.session.commit()
    return jsonify(lr.to_dict())


@bp.get("/reopened")
@login_required_api
def list_reopened():
    rows = ReopenedSlot.query.filter_by(claimed_by=None).order_by(ReopenedSlot.opened_at.desc()).all()
    out = []
    for r in rows:
        d = r.to_dict()
        d["slot"] = r.slot.to_dict()
        out.append(d)
    return jsonify(out)


@bp.post("/reopened/<int:reopened_id>/claim")
@login_required_api
def claim_reopened(reopened_id):
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403

    now = datetime.utcnow()
    updated = (
        db.session.query(ReopenedSlot)
        .filter(ReopenedSlot.id == reopened_id, ReopenedSlot.claimed_by.is_(None))
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
