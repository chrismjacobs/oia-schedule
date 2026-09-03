from datetime import datetime, date as date_cls
from uuid import uuid4

from flask import jsonify, request

from app.admin import bp
from app.extensions import db
from app.models import (
    Semester, Student, User, Month, ClosedDate, SelectionWindow, Slot,
    RegularSlotTemplate, RegularSlot,
    SLOT_HOURS, MONTH_STATES, REGULAR_SLOT_STATES,
)
from app.utils.decorators import overseer_required
from app.utils.settings import get_setting, set_setting
from app.utils.periods import weekdays_in_month
from app.dashboard.routes import build_month_dashboard
from app.admin.demo import seed_demo, reset_demo
from app.notifications.tick import run_tick

MONTH_TRANSITIONS = {
    "setup": {"selection_open"},
    "selection_open": {"selection_closed"},
    "selection_closed": {"draft"},
    "draft": {"review"},
    "review": {"committed"},
    "committed": {"running"},
    "running": {"closed"},
    "closed": set(),
}


# ---------------- Semesters ----------------

@bp.get("/semesters")
@overseer_required
def list_semesters():
    semesters = Semester.query.order_by(Semester.starts_on.desc()).all()
    return jsonify([
        {"id": s.id, "name": s.name, "starts_on": s.starts_on.isoformat(),
         "ends_on": s.ends_on.isoformat(), "is_active": s.is_active}
        for s in semesters
    ])


@bp.post("/semesters")
@overseer_required
def create_semester():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    starts_on = data.get("starts_on")
    ends_on = data.get("ends_on")
    if not name or not starts_on or not ends_on:
        return jsonify({"error": "missing_fields"}), 400

    if data.get("is_active", True):
        Semester.query.update({Semester.is_active: False})

    sem = Semester(
        name=name,
        starts_on=date_cls.fromisoformat(starts_on),
        ends_on=date_cls.fromisoformat(ends_on),
        is_active=data.get("is_active", True),
    )
    db.session.add(sem)
    db.session.commit()
    return jsonify({"id": sem.id, "name": sem.name}), 201


# ---------------- Students / invites ----------------

@bp.get("/students")
@overseer_required
def list_students():
    semester_id = request.args.get("semester_id", type=int)
    q = Student.query
    if semester_id:
        q = q.filter_by(semester_id=semester_id)
    students = q.order_by(Student.english_name).all()
    out = []
    for s in students:
        d = s.to_dict()
        d["has_account"] = s.user is not None
        out.append(d)
    return jsonify(out)


@bp.post("/invites")
@overseer_required
def create_invite():
    """Overseer sends an invite link to a prospective student's email. The
    student fills in names/ID/password when they accept (see auth.register)."""
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email_required"}), 400
    if User.query.filter(db.func.lower(User.email) == email).first():
        return jsonify({"error": "email_taken"}), 409

    token = str(uuid4())
    user = User(email=email, role="student", invite_token=token)
    user.set_password(str(uuid4()))  # placeholder until registration sets a real one
    db.session.add(user)
    db.session.commit()
    return jsonify({"email": email, "invite_token": token}), 201


@bp.patch("/students/<int:student_id>")
@overseer_required
def update_student(student_id):
    student = Student.query.get_or_404(student_id)
    data = request.get_json(force=True) or {}
    if "is_active" in data:
        student.is_active = bool(data["is_active"])
    if "colour" in data:
        student.colour = data["colour"]
    if "shape" in data:
        student.shape = data["shape"]
    db.session.commit()
    return jsonify(student.to_dict())


# ---------------- Months ----------------

@bp.get("/months")
@overseer_required
def list_months():
    months = Month.query.order_by(Month.year_month.desc()).all()
    return jsonify([m.to_dict() for m in months])


@bp.post("/months")
@overseer_required
def create_month():
    data = request.get_json(force=True) or {}
    year_month = data.get("year_month")
    if not year_month:
        return jsonify({"error": "year_month_required"}), 400
    if Month.query.filter_by(year_month=year_month).first():
        return jsonify({"error": "month_exists"}), 409
    month = Month(year_month=year_month, state="setup")
    db.session.add(month)
    db.session.commit()
    return jsonify(month.to_dict()), 201


@bp.patch("/months/<int:month_id>")
@overseer_required
def update_month_state(month_id):
    month = Month.query.get_or_404(month_id)
    data = request.get_json(force=True) or {}
    new_state = data.get("state")
    if new_state not in MONTH_STATES:
        return jsonify({"error": "invalid_state"}), 400
    if new_state != month.state and new_state not in MONTH_TRANSITIONS.get(month.state, set()):
        return jsonify({"error": "illegal_transition", "from": month.state, "to": new_state}), 400
    month.state = new_state
    db.session.commit()
    return jsonify(month.to_dict())


# ---------------- Closed dates ----------------

@bp.get("/months/<int:month_id>/closed-dates")
@overseer_required
def list_closed_dates(month_id):
    Month.query.get_or_404(month_id)
    rows = ClosedDate.query.filter_by(month_id=month_id).order_by(ClosedDate.date).all()
    return jsonify([r.to_dict() for r in rows])


@bp.post("/months/<int:month_id>/closed-dates")
@overseer_required
def add_closed_date(month_id):
    from flask_login import current_user
    month = Month.query.get_or_404(month_id)
    data = request.get_json(force=True) or {}
    date_str = data.get("date")
    if not date_str:
        return jsonify({"error": "date_required"}), 400
    d = date_cls.fromisoformat(date_str)
    if ClosedDate.query.filter_by(date=d).first():
        return jsonify({"error": "date_already_closed"}), 409
    cd = ClosedDate(month_id=month.id, date=d, reason=data.get("reason"), set_by=current_user.id)
    db.session.add(cd)
    db.session.commit()
    return jsonify(cd.to_dict()), 201


@bp.delete("/closed-dates/<int:closed_date_id>")
@overseer_required
def delete_closed_date(closed_date_id):
    cd = ClosedDate.query.get_or_404(closed_date_id)
    db.session.delete(cd)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------- Selection window ----------------

@bp.get("/months/<int:month_id>/selection-window")
@overseer_required
def get_selection_window(month_id):
    Month.query.get_or_404(month_id)
    sw = SelectionWindow.query.filter_by(month_id=month_id).first()
    return jsonify(sw.to_dict() if sw else None)


@bp.put("/months/<int:month_id>/selection-window")
@overseer_required
def set_selection_window(month_id):
    Month.query.get_or_404(month_id)
    data = request.get_json(force=True) or {}
    opens_at = data.get("opens_at")
    closes_at = data.get("closes_at")
    if not opens_at or not closes_at:
        return jsonify({"error": "missing_fields"}), 400
    sw = SelectionWindow.query.filter_by(month_id=month_id).first()
    if not sw:
        sw = SelectionWindow(month_id=month_id)
        db.session.add(sw)
    sw.opens_at = datetime.fromisoformat(opens_at)
    sw.closes_at = datetime.fromisoformat(closes_at)
    db.session.commit()
    return jsonify(sw.to_dict())


# ---------------- Regular schedule (standing slots) ----------------
# Master weekly template (persists across months) + per-month instances the
# overseer hand-edits week by week. See RegularSlotTemplate / RegularSlot in
# models.py for the full rationale.

@bp.get("/regular-template")
@overseer_required
def get_regular_template():
    rows = RegularSlotTemplate.query.all()
    return jsonify([r.to_dict() for r in rows])


@bp.put("/regular-template/cell")
@overseer_required
def set_regular_template_cell():
    data = request.get_json(force=True) or {}
    weekday = data.get("weekday")
    hour = data.get("hour")
    state = data.get("state")
    if weekday is None or hour is None or state not in REGULAR_SLOT_STATES:
        return jsonify({"error": "invalid_cell"}), 400
    student_id = data.get("student_id") if state == "assigned" else None
    if state == "assigned" and not student_id:
        return jsonify({"error": "student_id_required"}), 400

    row = RegularSlotTemplate.query.filter_by(weekday=weekday, hour=hour).first()
    if not row:
        row = RegularSlotTemplate(weekday=weekday, hour=hour)
        db.session.add(row)
    row.state = state
    row.student_id = student_id
    db.session.commit()
    return jsonify(row.to_dict())


def _month_weekdays_minus_closed(month):
    year, mon = (int(x) for x in month.year_month.split("-"))
    closed = {c.date for c in ClosedDate.query.filter(
        db.extract("year", ClosedDate.date) == year,
        db.extract("month", ClosedDate.date) == mon,
    ).all()}
    for d in weekdays_in_month(month.year_month):
        if d not in closed:
            yield d


@bp.post("/months/<int:month_id>/regular-slots/populate")
@overseer_required
def populate_regular_slots(month_id):
    """Copy the master template into this month's regular_slot rows, filling
    in only what's missing — safe to re-run any time during setup (e.g. after
    adding a closed date) without disturbing cells the overseer already
    hand-edited for this month."""
    month = Month.query.get_or_404(month_id)
    template_by_key = {(t.weekday, t.hour): t for t in RegularSlotTemplate.query.all()}
    existing = {(r.date, r.hour) for r in RegularSlot.query.filter_by(month_id=month.id).all()}

    created = 0
    for d in _month_weekdays_minus_closed(month):
        for hour in SLOT_HOURS:
            if (d, hour) in existing:
                continue
            tmpl = template_by_key.get((d.weekday(), hour))
            state = tmpl.state if tmpl else "unassigned"
            student_id = tmpl.student_id if tmpl and tmpl.state == "assigned" else None
            db.session.add(RegularSlot(month_id=month.id, date=d, hour=hour, state=state, student_id=student_id))
            created += 1
    db.session.commit()
    return jsonify({"created": created}), 201


@bp.get("/months/<int:month_id>/regular-slots")
@overseer_required
def list_regular_slots(month_id):
    Month.query.get_or_404(month_id)
    rows = RegularSlot.query.filter_by(month_id=month_id).order_by(RegularSlot.date, RegularSlot.hour).all()
    return jsonify([r.to_dict() for r in rows])


@bp.patch("/regular-slots/<int:regular_slot_id>")
@overseer_required
def update_regular_slot(regular_slot_id):
    row = RegularSlot.query.get_or_404(regular_slot_id)
    data = request.get_json(force=True) or {}
    state = data.get("state")
    if state not in REGULAR_SLOT_STATES:
        return jsonify({"error": "invalid_state"}), 400
    student_id = data.get("student_id") if state == "assigned" else None
    if state == "assigned" and not student_id:
        return jsonify({"error": "student_id_required"}), 400
    row.state = state
    row.student_id = student_id
    db.session.commit()
    return jsonify(row.to_dict())


# ---------------- Slot generation ----------------

@bp.post("/months/<int:month_id>/generate-slots")
@overseer_required
def generate_slots(month_id):
    month = Month.query.get_or_404(month_id)
    if Slot.query.filter_by(month_id=month.id).first():
        return jsonify({"error": "slots_already_generated"}), 409

    # Any (date, hour) already marked unavailable in this month's regular
    # schedule never gets a Slot at all — coverage need varies month to
    # month, not every hour needs staffing (CLAUDE.md discussion). Cells with
    # no regular_slot row (feature unused, or populate never run) fall back
    # to plain slot generation exactly as before.
    regular_by_key = {(r.date, r.hour): r for r in RegularSlot.query.filter_by(month_id=month.id).all()}

    created = 0
    for d in _month_weekdays_minus_closed(month):
        for hour in SLOT_HOURS:
            reg = regular_by_key.get((d, hour))
            if reg and reg.state == "unavailable":
                continue
            period = "morning" if hour < 12 else "afternoon"
            db.session.add(Slot(month_id=month.id, date=d, hour=hour, period=period, state="open"))
            created += 1
    db.session.commit()
    return jsonify({"created": created}), 201


# ---------------- Monthly close-out (CLAUDE.md #5 state 8, #18 build step 13) ----------------

@bp.post("/months/<int:month_id>/close")
@overseer_required
def close_month(month_id):
    month = Month.query.get_or_404(month_id)
    if month.state != "running":
        return jsonify({"error": "wrong_month_state", "state": month.state}), 409
    report = build_month_dashboard(month)
    month.state = "closed"
    db.session.commit()
    report["month"] = month.to_dict()
    return jsonify(report)


# ---------------- Manual tick (session-authenticated, for testing —
# the real /api/tick is the token-protected one the external cron hits) ----------------

@bp.post("/run-tick")
@overseer_required
def run_tick_route():
    return jsonify(run_tick())


# ---------------- Demo data (CLAUDE.md #17) ----------------

@bp.post("/demo/seed")
@overseer_required
def seed_demo_route():
    result = seed_demo()
    return jsonify(result), 201


@bp.post("/demo/reset")
@overseer_required
def reset_demo_route():
    result = reset_demo()
    return jsonify(result)


# ---------------- Settings (solver weights, floor, cadence) ----------------

@bp.get("/settings")
@overseer_required
def get_settings():
    from flask import current_app
    return jsonify({
        "solver_weights": get_setting("solver_weights", dict(current_app.config["SOLVER_WEIGHTS"])),
        "solver_floor_hours": get_setting("solver_floor_hours", current_app.config["SOLVER_FLOOR_HOURS"]),
        "timecard_cadence": get_setting("timecard_cadence", current_app.config["TIMECARD_CADENCE_DEFAULT"]),
        "notify_attendance_events": get_setting("notify_attendance_events", True),
        "sign_in_opens_minutes_before": current_app.config["SIGN_IN_OPENS_MINUTES_BEFORE"],
        "no_show_grace_minutes": current_app.config["NO_SHOW_GRACE_MINUTES"],
        "closing_warning_hours_before": current_app.config["CLOSING_WARNING_HOURS_BEFORE"],
    })


@bp.put("/settings")
@overseer_required
def put_settings():
    data = request.get_json(force=True) or {}
    if "solver_weights" in data:
        set_setting("solver_weights", data["solver_weights"])
    if "solver_floor_hours" in data:
        set_setting("solver_floor_hours", int(data["solver_floor_hours"]))
    if "timecard_cadence" in data:
        set_setting("timecard_cadence", data["timecard_cadence"])
    if "notify_attendance_events" in data:
        set_setting("notify_attendance_events", bool(data["notify_attendance_events"]))
    return get_settings()
