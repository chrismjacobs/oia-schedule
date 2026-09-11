from datetime import datetime, date as date_cls
from uuid import uuid4

from flask import jsonify, request, current_app

from app.admin import bp
from app.extensions import db
from app.models import (
    Semester, Student, User, Month, ClosedDate, SelectionWindow, Slot,
    RegularSlotTemplate, RegularSlot, Schedule, Assignment, Availability, ReopenedSlot,
    SLOT_HOURS, MONTH_STATES, LEGACY_MONTH_STATES, REGULAR_SLOT_STATES,
    STUDENT_ID_RE, STUDENT_ID_MAX, STUDENT_PALETTE, STUDENT_SHAPES,
)
from app.utils.decorators import overseer_required
from app.utils.settings import get_setting, set_setting
from app.utils.periods import weekdays_in_month
from app.dashboard.routes import build_month_dashboard
from app.admin.demo import seed_demo, reset_demo
from app.notifications.tick import run_tick
from app.notifications.service import reset_notification, notify_selection_open

# The forward path through the cycle (CLAUDE.md #6). Used to label which move
# is the "normal next step" in the UI — NOT to forbid anything. Going
# backwards is a routine correction: selection closes and a student asks for
# one more day, a month gets committed too early, a closed month needs
# reopening. Blocking that left months permanently stuck with no way out, so
# the overseer's dropdown is an explicit override and may set any state.
MONTH_FORWARD = {
    "setup": "selection_open",
    "selection_open": "selection_closed",
    "selection_closed": "review",   # generating the draft lands here directly
    "draft": "review",              # legacy rows only
    "review": "committed",
    "committed": "running",
    "running": "closed",
    "closed": None,
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
        # Overseer-only, so it's added here rather than in Student.to_dict(),
        # which is also what students receive in the roster/team views.
        d["insurance_number"] = s.insurance_number
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

    # Validate everything before touching the object, so a rejected field
    # can't leave a half-applied edit behind.
    new_student_id = None
    if "student_id" in data:
        raw = (data["student_id"] or "").strip()
        if not STUDENT_ID_RE.match(raw) or len(raw) > STUDENT_ID_MAX:
            return jsonify({"error": "invalid_student_id",
                            "message": f"Student ID must be letters and numbers only, "
                                       f"up to {STUDENT_ID_MAX} characters"}), 400
        clash = Student.query.filter(
            db.func.lower(Student.student_id) == raw.lower(), Student.id != student.id
        ).first()
        if clash:
            return jsonify({"error": "student_id_taken",
                            "message": "Another student already has that ID"}), 409
        new_student_id = raw

    # Names are safe to correct: every history row (assignments, sessions,
    # leave, task completions) points at the student's internal id, never at
    # the name or the student ID, and reports are built live — so a rename
    # simply shows everywhere, past months included.
    new_names = {}
    for field in ("chinese_name", "english_name"):
        if field in data:
            raw = (data[field] or "").strip()
            if len(raw) > 64:
                return jsonify({"error": "name_too_long",
                                "message": "Names are limited to 64 characters"}), 400
            new_names[field] = raw
    if new_names:
        zh = new_names.get("chinese_name", student.chinese_name)
        en = new_names.get("english_name", student.english_name)
        if not zh and not en:
            return jsonify({"error": "name_required",
                            "message": "Provide at least one of Chinese/English name"}), 400

    # Token override (CLAUDE.md §15: the overseer can change a colour if two
    # look close). Kept to the managed palette and shapes, and unique within
    # the semester — the DB constraint would reject a clash anyway, but as a
    # 500 rather than a message.
    colour = data.get("colour", student.colour)
    shape = data.get("shape", student.shape)
    if "colour" in data or "shape" in data:
        if ("colour" in data and colour not in STUDENT_PALETTE) or ("shape" in data and shape not in STUDENT_SHAPES):
            return jsonify({"error": "invalid_token",
                            "message": "Pick a colour and shape from the palette"}), 400
        clash = Student.query.filter(
            Student.semester_id == student.semester_id, Student.colour == colour,
            Student.shape == shape, Student.id != student.id,
        ).first()
        if clash:
            return jsonify({"error": "token_taken",
                            "message": f"{clash.short_name} already has that colour and shape"}), 409

    new_insurance = None
    if "insurance_number" in data:
        # Free text: the insurer's format isn't ours to police, and a wrong
        # guess at a pattern would just block a legitimate number. Blank
        # clears it back to "not on record".
        raw = (data["insurance_number"] or "").strip()
        if len(raw) > 32:
            return jsonify({"error": "insurance_number_too_long",
                            "message": "Insurance number is limited to 32 characters"}), 400
        new_insurance = raw or None

    for field, value in new_names.items():
        setattr(student, field, value)
    if new_student_id is not None:
        student.student_id = new_student_id
    if "insurance_number" in data:
        student.insurance_number = new_insurance
    if "is_active" in data:
        student.is_active = bool(data["is_active"])
    student.colour, student.shape = colour, shape

    db.session.commit()
    out = student.to_dict()
    out["insurance_number"] = student.insurance_number
    return jsonify(out)


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
    """Set the month's state. Any state to any state — this is the overseer's
    manual override, and the cycle in MONTH_FORWARD is guidance, not a rail.
    The response says whether the move was the normal next step so the UI can
    warn about the unusual ones without refusing them."""
    month = Month.query.get_or_404(month_id)
    data = request.get_json(force=True) or {}
    new_state = data.get("state")
    if new_state not in MONTH_STATES and new_state not in LEGACY_MONTH_STATES:
        return jsonify({"error": "invalid_state", "valid": MONTH_STATES}), 400

    was = month.state
    # Closing is the one move that produces something. Route it through the
    # close-out so the report is always generated — picking "closed" from the
    # dropdown used to just relabel the month and silently skip it.
    report = None
    if new_state == "closed" and was != "closed":
        report = build_month_dashboard(month)

    month.state = new_state
    db.session.commit()

    # Opening selection by hand used to tell nobody — only /tick's automatic
    # path announced it, so a manual open left students with no idea the month
    # was live. Announce on entry, clearing the previous key first so a
    # deliberate reopen isn't deduped against the first open.
    announced = False
    if new_state == "selection_open" and was != "selection_open":
        reset_notification("selection_open", "group", "month", month.id)
        announced = notify_selection_open(month)

    out = month.to_dict()
    out["previous_state"] = was
    out["was_forward_step"] = (new_state == MONTH_FORWARD.get(was))
    out["closeout_report"] = report
    out["announced"] = announced
    return jsonify(out)


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
    new_opens = datetime.fromisoformat(opens_at)
    new_closes = datetime.fromisoformat(closes_at)
    if new_closes <= new_opens:
        return jsonify({"error": "closes_before_opens",
                        "message": "The close time must be after the open time"}), 400

    # Rescheduling re-arms the boundary: a stamp only means "this exact time
    # has already been acted on". Move the time and it's due again.
    opens_changed = sw.opens_at != new_opens
    closes_changed = sw.closes_at != new_closes
    if opens_changed:
        sw.opened_applied_at = None
    if closes_changed:
        sw.closed_applied_at = None
    sw.opens_at, sw.closes_at = new_opens, new_closes
    db.session.commit()

    # After the commit, never before: on a brand-new window these queries would
    # otherwise autoflush a half-built row whose opens_at is still NULL.
    # Re-arm the announcements too, or a reopen happens silently — the
    # notification key is per-month for all time, so a month's second open is
    # deduped against its first.
    if opens_changed:
        reset_notification("selection_open", "group", "month", month_id)
    if closes_changed:
        reset_notification("closing_warning", "group", "month", month_id)
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
    """Copy the master template into this month's regular_slot rows.

    Two modes, because both are genuinely wanted:
      * default (fill) — add only missing rows, leaving this month's
        hand-edits alone. Safe after adding a closed date.
      * resync=true — overwrite every row from the template. This is the one
        that was missing: once a month had its rows, editing the template
        could never reach it again, so a corrected pattern silently applied
        to nothing. Discards per-month exceptions, hence not the default.
    """
    month = Month.query.get_or_404(month_id)
    resync = bool((request.get_json(silent=True) or {}).get("resync"))
    template_by_key = {(t.weekday, t.hour): t for t in RegularSlotTemplate.query.all()}
    existing = {(r.date, r.hour): r for r in RegularSlot.query.filter_by(month_id=month.id).all()}

    created = updated = 0
    for d in _month_weekdays_minus_closed(month):
        for hour in SLOT_HOURS:
            tmpl = template_by_key.get((d.weekday(), hour))
            state = tmpl.state if tmpl else "unassigned"
            student_id = tmpl.student_id if tmpl and tmpl.state == "assigned" else None
            row = existing.get((d, hour))
            if row is None:
                db.session.add(RegularSlot(month_id=month.id, date=d, hour=hour,
                                            state=state, student_id=student_id))
                created += 1
            elif resync and (row.state, row.student_id) != (state, student_id):
                row.state, row.student_id = state, student_id
                updated += 1
    db.session.commit()
    return jsonify({"created": created, "updated": updated, "resync": resync}), 201


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
    """Turn this month's regular_slot plan into the Slot rows students
    actually select against.

    Re-runnable (regenerate=true): the pattern legitimately changes during
    setup, and being one-shot meant a month generated from a wrong pattern
    was stuck with it forever. Regenerating throws away the month's slots and
    everything hanging off them — availability included — so it is refused
    once a schedule is committed, where that would delete a live roster."""
    month = Month.query.get_or_404(month_id)
    regenerate = bool((request.get_json(silent=True) or {}).get("regenerate"))
    existing = Slot.query.filter_by(month_id=month.id).all()

    if existing and not regenerate:
        return jsonify({"error": "slots_already_generated",
                        "message": f"{len(existing)} slots already exist. "
                                   "Regenerate to rebuild them from the pattern."}), 409

    if existing:
        committed = Schedule.query.filter_by(month_id=month.id, status="committed").first()
        if committed:
            return jsonify({"error": "schedule_committed",
                            "message": "This month's schedule is committed — regenerating "
                                       "would delete the published roster."}), 409
        slot_ids = [s.id for s in existing]
        n_avail = Availability.query.filter(Availability.slot_id.in_(slot_ids)).delete(
            synchronize_session=False)
        Assignment.query.filter(Assignment.slot_id.in_(slot_ids)).delete(synchronize_session=False)
        ReopenedSlot.query.filter(ReopenedSlot.slot_id.in_(slot_ids)).delete(synchronize_session=False)
        Schedule.query.filter_by(month_id=month.id).delete(synchronize_session=False)
        Slot.query.filter(Slot.id.in_(slot_ids)).delete(synchronize_session=False)
        year_month = month.year_month
        # Settle the deletes before rebuilding, and drop the deleted rows from
        # the identity map: on SQLite the new slots reuse the freed primary
        # keys, which otherwise collide with the stale objects still mapped.
        db.session.commit()
        db.session.expunge_all()
        month = Month.query.get_or_404(month_id)  # re-attach after expunge
        current_app.logger.info("regenerate slots for %s: dropped %d slots, %d availability",
                                year_month, len(slot_ids), n_avail)

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
