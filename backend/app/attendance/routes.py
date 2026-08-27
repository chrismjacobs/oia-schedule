from datetime import datetime, timedelta

from flask import jsonify, request, current_app
from flask_login import current_user

from app.attendance import bp
from app.extensions import db
from app.models import (
    Slot, Assignment, Schedule, Month, AttendanceSession, HourlyReport,
    RegularTask, TaskCompletion, CustomTask,
)
from app.utils.decorators import login_required_api
from app.utils.periods import period_key_for
from app.utils.s3 import upload_object
from app.utils.tz import local_now, local_today


def _slot_start(slot):
    return datetime.combine(slot.date, datetime.min.time()).replace(hour=slot.hour)


def _todays_assignments(student_id, on_date):
    return (
        Assignment.query.join(Slot, Assignment.slot_id == Slot.id)
        .join(Schedule, Assignment.schedule_id == Schedule.id)
        .filter(
            Assignment.student_id == student_id,
            Schedule.status == "committed",
            Slot.date == on_date,
        )
        .all()
    )


@bp.get("/today")
@login_required_api
def today():
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    today_date = local_today()
    assignments = _todays_assignments(current_user.student_id, today_date)
    now = local_now()
    opens_before = timedelta(minutes=current_app.config["SIGN_IN_OPENS_MINUTES_BEFORE"])

    slot_rows = []
    for a in sorted(assignments, key=lambda a: a.slot.hour):
        slot = a.slot
        start = _slot_start(slot)
        slot_rows.append({
            **slot.to_dict(),
            "sign_in_open": start - opens_before <= now <= start + timedelta(hours=1),
        })

    open_session = AttendanceSession.query.filter_by(
        student_id=current_user.student_id, date=today_date, signed_out_at=None
    ).first()

    return jsonify({
        "date": today_date.isoformat(),
        "scheduled_slots": slot_rows,
        "open_session": open_session.to_dict() if open_session else None,
    })


@bp.post("/sign-in")
@login_required_api
def sign_in():
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    today_date = local_today()

    existing = AttendanceSession.query.filter_by(
        student_id=current_user.student_id, date=today_date, signed_out_at=None
    ).first()
    if existing:
        return jsonify({"error": "session_already_open", "session": existing.to_dict()}), 409

    assignments = _todays_assignments(current_user.student_id, today_date)
    now = local_now()

    session = AttendanceSession(student_id=current_user.student_id, date=today_date, signed_in_at=now)
    if not assignments:
        session.flagged = True
        session.flag_reason = "not_scheduled"
    db.session.add(session)
    db.session.commit()
    return jsonify(session.to_dict()), 201


@bp.get("/tasks")
@login_required_api
def available_tasks():
    """Regular tasks not yet done this period + open custom tasks — shown at
    sign-out (CLAUDE.md #9)."""
    today_date = local_today()
    regular = RegularTask.query.filter_by(is_active=True).all()
    available_regular = []
    for t in regular:
        pk = period_key_for(t.frequency, today_date)
        done = TaskCompletion.query.filter_by(regular_task_id=t.id, period_key=pk).first()
        if not done:
            available_regular.append(t.to_dict())

    custom = CustomTask.query.filter(CustomTask.status.in_(("open", "claimed"))).all()
    return jsonify({
        "regular_tasks": available_regular,
        "custom_tasks": [t.to_dict() for t in custom],
    })


def _task_entry(item):
    """Task entries may be a bare id, or {id, proof_s3_key} when a photo was
    staged first via /upload-proof-photo."""
    if isinstance(item, dict):
        return item.get("id"), item.get("proof_s3_key")
    return item, None


@bp.post("/upload-proof-photo")
@login_required_api
def upload_proof_photo():
    """Stage a completion proof photo before sign-out submits the JSON body —
    returns an s3_key to attach to a regular_task_ids/custom_task_ids entry."""
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    if "file" not in request.files:
        return jsonify({"error": "file_required"}), 400
    key = upload_object(request.files["file"], f"tasks/proof/{current_user.student_id}")
    return jsonify({"s3_key": key})


@bp.post("/sign-out")
@login_required_api
def sign_out():
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    data = request.get_json(force=True) or {}
    session_id = data.get("session_id")
    reports = data.get("reports") or []  # [{slot_id, note, regular_task_ids:[], custom_task_ids:[]}]
    # regular_task_ids / custom_task_ids entries: either a bare id, or
    # {id, proof_s3_key} when photo_required and a photo was staged first.

    session = AttendanceSession.query.filter_by(
        id=session_id, student_id=current_user.student_id, signed_out_at=None
    ).first()
    if not session:
        return jsonify({"error": "no_open_session"}), 404

    todays_slot_ids = {a.slot_id for a in _todays_assignments(current_user.student_id, session.date)}
    now = local_now()
    skipped = []

    for r in reports:
        slot_id = r.get("slot_id")
        if slot_id not in todays_slot_ids:
            return jsonify({"error": "slot_not_scheduled_for_student", "slot_id": slot_id}), 400

        hr = HourlyReport(session_id=session.id, slot_id=slot_id, note=(r.get("note") or "").strip() or None)
        db.session.add(hr)
        db.session.flush()

        slot = Slot.query.get(slot_id)
        for item in r.get("regular_task_ids") or []:
            task_id, proof_key = _task_entry(item)
            task = RegularTask.query.get(task_id)
            if not task:
                continue
            if task.photo_required and not proof_key:
                skipped.append({"type": "regular", "id": task_id, "reason": "photo_required"})
                continue
            pk = period_key_for(task.frequency, slot.date)
            if TaskCompletion.query.filter_by(regular_task_id=task.id, period_key=pk).first():
                continue  # already done this period elsewhere — silently skip, don't error the whole sign-out
            db.session.add(TaskCompletion(
                regular_task_id=task.id, student_id=current_user.student_id, session_id=session.id,
                hourly_report_id=hr.id, slot_id=slot_id, completed_at=now, period_key=pk,
                proof_s3_key=proof_key,
            ))

        for item in r.get("custom_task_ids") or []:
            custom_id, proof_key = _task_entry(item)
            ct = CustomTask.query.get(custom_id)
            if not ct or ct.status == "done":
                continue
            if ct.photo_required and not proof_key:
                skipped.append({"type": "custom", "id": custom_id, "reason": "photo_required"})
                continue
            ct.status = "done"
            ct.claimed_by = current_user.student_id
            ct.claimed_at = ct.claimed_at or now
            ct.hourly_report_id = hr.id
            if proof_key:
                ct.proof_s3_key = proof_key

    session.signed_out_at = now
    db.session.commit()
    result = session.to_dict()
    result["skipped"] = skipped
    return jsonify(result)
