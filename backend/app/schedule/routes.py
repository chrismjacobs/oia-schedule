from flask import jsonify, request

from app.schedule import bp
from app.extensions import db
from app.models import Month, Schedule, Assignment, Slot, Availability, Student
from app.schedule.solver import solve_month
from app.utils.decorators import overseer_required, login_required_api
from app.utils.settings import get_solver_weights, get_floor_hours
from app.utils.tz import local_now
from app.notifications.service import notify_committed


@bp.post("/months/<int:month_id>/generate-draft")
@overseer_required
def generate_draft(month_id):
    month = Month.query.get_or_404(month_id)
    if month.state not in ("selection_closed", "draft", "review"):
        return jsonify({"error": "wrong_month_state", "state": month.state}), 409

    weights = get_solver_weights()
    floor_hours = get_floor_hours()
    result, meta = solve_month(month.id, weights, floor_hours)

    schedule = Schedule(month_id=month.id, status="draft", generated_at=local_now(),
                         solver_weights={"weights": weights, "floor_hours": floor_hours, "meta": meta})
    db.session.add(schedule)
    db.session.flush()

    for slot_id, student_id in result.items():
        db.session.add(Assignment(schedule_id=schedule.id, slot_id=slot_id, student_id=student_id, source="solver"))

    assigned_slot_ids = set(result.keys())
    for slot in Slot.query.filter_by(month_id=month.id).all():
        slot.state = "assigned" if slot.id in assigned_slot_ids else "open"

    month.state = "review"
    db.session.commit()
    return jsonify({"schedule": schedule.to_dict(), "meta": meta}), 201


def _schedule_payload(schedule):
    assignments = Assignment.query.filter_by(schedule_id=schedule.id).all()
    slots = Slot.query.filter_by(month_id=schedule.month_id).order_by(Slot.date, Slot.hour).all()
    avail = Availability.query.join(Slot).filter(Slot.month_id == schedule.month_id).all()

    eligible_by_slot = {}
    for a in avail:
        eligible_by_slot.setdefault(a.slot_id, []).append(a.student_id)

    assignment_by_slot = {a.slot_id: a for a in assignments}
    students = {s.id: s.to_dict() for s in Student.query.all()}

    slot_rows = []
    for slot in slots:
        a = assignment_by_slot.get(slot.id)
        slot_rows.append({
            "slot": slot.to_dict(),
            "assignment": a.to_dict() if a else None,
            "eligible_student_ids": eligible_by_slot.get(slot.id, []),
        })

    return {
        "schedule": schedule.to_dict(),
        "students": students,
        "slots": slot_rows,
    }


@bp.get("/mine")
@login_required_api
def my_assignments():
    from flask_login import current_user
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    rows = (
        Assignment.query.join(Slot, Assignment.slot_id == Slot.id)
        .join(Schedule, Assignment.schedule_id == Schedule.id)
        .filter(Assignment.student_id == current_user.student_id, Schedule.status == "committed")
        .order_by(Slot.date, Slot.hour)
        .all()
    )
    out = []
    for a in rows:
        d = a.to_dict()
        d["slot"] = a.slot.to_dict()
        out.append(d)
    return jsonify(out)


@bp.get("/months/<int:month_id>")
@login_required_api
def get_schedule(month_id):
    Month.query.get_or_404(month_id)
    schedule = (Schedule.query.filter_by(month_id=month_id)
                .order_by(Schedule.generated_at.desc()).first())
    if not schedule:
        return jsonify(None)
    return jsonify(_schedule_payload(schedule))


@bp.patch("/assignments/<int:assignment_id>")
@overseer_required
def edit_assignment(assignment_id):
    """Exactly one change — no re-solve, no ripple (CLAUDE.md #6)."""
    assignment = Assignment.query.get_or_404(assignment_id)
    data = request.get_json(force=True) or {}
    new_student_id = data.get("student_id")

    if new_student_id is not None:
        eligible = Availability.query.filter_by(slot_id=assignment.slot_id, student_id=new_student_id).first()
        if not eligible:
            return jsonify({"error": "student_not_available_for_slot"}), 400
        assignment.student_id = new_student_id
        assignment.source = "manual_edit"
        assignment.slot.state = "assigned"
        db.session.commit()
        return jsonify(assignment.to_dict())
    else:
        slot = assignment.slot
        db.session.delete(assignment)
        slot.state = "open"
        db.session.commit()
        return jsonify({"ok": True})


@bp.post("/months/<int:month_id>/assignments")
@overseer_required
def create_assignment(month_id):
    """Manually fill a currently-uncovered slot with an eligible student."""
    Month.query.get_or_404(month_id)
    data = request.get_json(force=True) or {}
    slot_id = data.get("slot_id")
    student_id = data.get("student_id")
    schedule = (Schedule.query.filter_by(month_id=month_id)
                .order_by(Schedule.generated_at.desc()).first())
    if not schedule:
        return jsonify({"error": "no_draft_yet"}), 409
    if Assignment.query.filter_by(schedule_id=schedule.id, slot_id=slot_id).first():
        return jsonify({"error": "slot_already_assigned"}), 409
    if not Availability.query.filter_by(slot_id=slot_id, student_id=student_id).first():
        return jsonify({"error": "student_not_available_for_slot"}), 400

    a = Assignment(schedule_id=schedule.id, slot_id=slot_id, student_id=student_id, source="manual_edit")
    db.session.add(a)
    slot = Slot.query.get(slot_id)
    slot.state = "assigned"
    db.session.commit()
    return jsonify(a.to_dict()), 201


@bp.post("/months/<int:month_id>/commit")
@overseer_required
def commit_schedule(month_id):
    month = Month.query.get_or_404(month_id)
    schedule = (Schedule.query.filter_by(month_id=month_id, status="draft")
                .order_by(Schedule.generated_at.desc()).first())
    if not schedule:
        return jsonify({"error": "no_draft_to_commit"}), 409

    schedule.status = "committed"
    schedule.committed_at = local_now()
    month.state = "committed"
    db.session.commit()

    notify_committed(month)
    return jsonify(schedule.to_dict())
