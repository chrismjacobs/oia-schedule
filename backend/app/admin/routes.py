import csv
import io
from collections import defaultdict
from datetime import datetime, date as date_cls
from uuid import uuid4

from flask import jsonify, request, current_app, session, Response
from flask_login import current_user, login_user

from app.admin import bp
from app.extensions import db
from app.models import (
    Semester, Student, User, Month, ClosedDate, SelectionWindow,
    RegularSlotTemplate, RegularSlot, Slot, Availability, AvailabilityOptOut,
    Assignment, Schedule,
    SLOT_HOURS, MONTH_STATES, LEGACY_MONTH_STATES, REGULAR_SLOT_STATES,
    STUDENT_ID_RE, STUDENT_ID_MAX, STUDENT_PALETTE, STUDENT_SHAPES, WORKER_TYPES,
    FUNDING_CATEGORIES, PROJECT_NAME_MAX,
    USERNAME_RE, normalize_username,
)
from app.utils.decorators import overseer_required
from app.utils.settings import get_setting, set_setting, get_solver_weights, get_session_rules
from app.utils.periods import weekdays_in_month
from app.utils.slot_sync import (
    sync_month_slots, month_has_slots, AvailabilityLossRefused, MAX_LOST_DEFAULT,
)
from app.utils.tracks import TRACKS, DEFAULT_TRACK, TRACK_DEFAULT_ON, track_for
from app.dashboard.routes import build_month_dashboard
from app.admin.demo import seed_demo, reset_demo
from app.notifications.tick import run_tick
from app.notifications.service import reset_notification, notify_selection_open, notify_month_report

# The forward path through the cycle (CLAUDE.md #6). Used to label which move
# is the "normal next step" in the UI — NOT to forbid anything. Going
# backwards is a routine correction: selection closes and a student asks for
# one more day, a month gets committed too early, a closed month needs
# reopening. Blocking that left months permanently stuck with no way out, so
# the overseer's dropdown is an explicit override and may set any state.
# Minimum for a password the overseer sets on a student's behalf. Lower than
# registration's 8 because the passwords already handed out (name + a few
# digits) are shorter than that, and the overseer must be able to re-set one.
OVERSEER_SET_PASSWORD_MIN = 6


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


@bp.errorhandler(AvailabilityLossRefused)
def _availability_loss_refused(err):
    """A plan change that would take selections off a lot of students stops
    here, with nothing committed.

    Every route that changes the plan syncs the month's slots as part of the
    same request, so any of them can reach this. Refusing by exception rather
    than per-route means the whole operation is undone together — the closed
    date or regular-slot edit that triggered it isn't saved either, so the
    overseer is never left with a half-applied change to reason about.
    """
    current_app.logger.warning(
        "slot sync REFUSED — would drop selections for %d student(s): %s",
        len(err.lost_picks), err.lost_picks)
    return jsonify({
        "error": "would_lose_availability",
        "message": ("This would delete saved hours for "
                    f"{len(err.lost_picks)} students. Nothing has been changed."),
        "lost_picks": err.lost_picks,
        "slots": err.slots,
        "confirm_with": "confirm=true",
    }), 409


def _sync_confirmed():
    """Has the overseer already been shown what a sync would drop, and said
    yes? Sent back as confirm=true on the retry of a refused request."""
    if (request.args.get("confirm") or "").lower() in ("1", "true", "yes"):
        return True
    return bool((request.get_json(silent=True) or {}).get("confirm"))


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
        d["project_name"] = s.project_name
        d["funding_category"] = s.funding_category
        d["username"] = s.user.username if s.user else None
        out.append(d)
    return jsonify(out)


@bp.post("/students/<int:student_id>/login-as")
@overseer_required
def login_as_student(student_id):
    """Switch this browser session to the student's account, no password —
    so the overseer can see exactly what that student sees. The overseer's
    own id is kept in the (signed) session, which is what the banner's
    "Return to overseer" uses to switch back; nothing else can set it.

    Not a remembered login: if the session ends, the browser falls back to
    the overseer's own remember-me login, never to the student's."""
    student = Student.query.get_or_404(student_id)
    account = student.user
    if not account:
        return jsonify({"error": "no_account",
                        "message": "This student has no login account"}), 400
    overseer_id = current_user.id
    current_app.logger.info("login-as START | overseer %s -> student %s (%s, user %s)",
                            overseer_id, student.id, student.short_name, account.id)
    login_user(account, remember=False)
    session["impersonator_id"] = overseer_id
    return jsonify({"ok": True, "redirect": "/"})


def _check_username(raw, exclude_user_id=None):
    """Normalise a username and check it's usable. Returns (username, None)
    or (None, error response)."""
    username = normalize_username(raw)
    if not USERNAME_RE.match(username):
        return None, (jsonify({"error": "invalid_username",
                               "message": "Username: letters, numbers, dot, dash or underscore, "
                                          "up to 64 characters"}), 400)
    q = User.query.filter(db.func.lower(User.username) == username)
    if exclude_user_id is not None:
        q = q.filter(User.id != exclude_user_id)
    if q.first():
        return None, (jsonify({"error": "username_taken",
                               "message": "That username is already in use"}), 409)
    return username, None


@bp.post("/invites")
@overseer_required
def create_invite():
    """Overseer creates an invite for a username of their choosing and passes
    the link on. The student fills in names/ID/password when they accept
    (see auth.register), then logs in with that username."""
    data = request.get_json(force=True) or {}
    username, err = _check_username(data.get("username"))
    if err:
        return err

    token = str(uuid4())
    user = User(username=username, role="student", invite_token=token)
    user.set_password(str(uuid4()))  # placeholder until registration sets a real one
    db.session.add(user)
    db.session.commit()
    return jsonify({"username": username, "invite_token": token}), 201


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

    # Login details live on the student's account (app_user), not the student
    # row — a student with no account (e.g. demo data) has none to edit.
    account = student.user
    new_username = None
    if "username" in data:
        if not account:
            return jsonify({"error": "no_account",
                            "message": "This student has no login account"}), 400
        new_username, err = _check_username(data["username"], exclude_user_id=account.id)
        if err:
            return err
    new_password = None
    if data.get("password"):
        if not account:
            return jsonify({"error": "no_account",
                            "message": "This student has no login account"}), 400
        new_password = data["password"]
        if len(new_password) < OVERSEER_SET_PASSWORD_MIN:
            return jsonify({"error": "weak_password",
                            "message": f"Password must be at least {OVERSEER_SET_PASSWORD_MIN} characters"}), 400

    new_worker_type = None
    if "worker_type" in data:
        # Blank clears it back to "not set".
        new_worker_type = (data["worker_type"] or "").strip().upper() or None
        if new_worker_type is not None and new_worker_type not in WORKER_TYPES:
            return jsonify({"error": "invalid_worker_type",
                            "message": "Worker type must be one of " + ", ".join(WORKER_TYPES)}), 400

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

    new_project_name = None
    if "project_name" in data:
        # Free text — the portal's own field is free text too, and a guessed
        # pattern would only block a legitimate project title. Blank clears it.
        raw = (data["project_name"] or "").strip()
        if len(raw) > PROJECT_NAME_MAX:
            return jsonify({"error": "project_name_too_long",
                            "message": f"Project name is limited to {PROJECT_NAME_MAX} characters"}), 400
        new_project_name = raw or None

    new_funding = None
    if "funding_category" in data:
        # "0" (College) is a real choice, not "unset" — only an empty string
        # clears it, so don't fold this into a falsiness check.
        raw = (data["funding_category"] or "").strip()
        if raw and raw not in FUNDING_CATEGORIES:
            return jsonify({"error": "invalid_funding_category",
                            "message": "Pick a funding source from the list"}), 400
        new_funding = raw or None

    for field, value in new_names.items():
        setattr(student, field, value)
    if new_student_id is not None:
        student.student_id = new_student_id
    if "insurance_number" in data:
        student.insurance_number = new_insurance
    if "worker_type" in data:
        student.worker_type = new_worker_type
    if "project_name" in data:
        student.project_name = new_project_name
    if "funding_category" in data:
        student.funding_category = new_funding
    if new_username is not None:
        account.username = new_username
    if new_password is not None:
        account.set_password(new_password)
    if "is_active" in data:
        student.is_active = bool(data["is_active"])
    student.colour, student.shape = colour, shape

    db.session.commit()
    out = student.to_dict()
    out["insurance_number"] = student.insurance_number
    out["project_name"] = student.project_name
    out["funding_category"] = student.funding_category
    out["username"] = account.username if account else None
    return jsonify(out)


# ---------------- Insurance ----------------

def _insurance_days(student_id, month_id):
    """The student's committed hours for the month, collapsed into one entry
    per contiguous run — which is what the portal's "add times" stage asks
    for: a day, a start and an end. Two runs on one day (a morning and an
    afternoon) stay two entries, because that's two rows in the portal."""
    rows = (
        db.session.query(Slot.date, Slot.hour)
        .join(Assignment, Assignment.slot_id == Slot.id)
        .join(Schedule, Assignment.schedule_id == Schedule.id)
        .filter(
            Assignment.student_id == student_id,
            Slot.month_id == month_id,
            Schedule.status == "committed",
        )
        .order_by(Slot.date, Slot.hour)
        .all()
    )

    by_date = defaultdict(list)
    for d, hour in rows:
        by_date[d].append(hour)

    days = []
    for d in sorted(by_date):
        run = []
        for hour in sorted(by_date[d]):
            # The lunch break makes 11:00 and 13:00 non-contiguous on its own,
            # so a plain +1 check is all the split this needs.
            if run and hour == run[-1] + 1:
                run.append(hour)
            else:
                if run:
                    days.append(_day_entry(d, run))
                run = [hour]
        if run:
            days.append(_day_entry(d, run))
    return days


def _day_entry(d, run):
    return {
        "date": d.isoformat(),
        "weekday": d.isoweekday(),
        "start": f"{run[0]:02d}:00",
        "end": f"{run[-1] + 1:02d}:00",
        "hours": len(run),
    }


@bp.get("/students/<int:student_id>/insurance")
@overseer_required
def student_insurance(student_id):
    """Everything the insurance portal asks about one student for one month,
    in one payload — so the console script the dashboard generates is filled
    from here rather than scraped back out of the page.

    Overseer-only, like the insurance number itself. Warnings are advisory:
    a missing field is reported, never a reason to refuse the payload, since
    the overseer may be filling the rest by hand."""
    student = Student.query.get_or_404(student_id)

    month_id = request.args.get("month_id", type=int)
    month = Month.query.get(month_id) if month_id else None
    if month_id and not month:
        return jsonify({"error": "month_not_found"}), 404
    if month is None:
        # Default to the next month that has a committed schedule — insurance
        # is applied for once the schedule is confirmed (CLAUDE.md §6).
        month = (
            Month.query.join(Schedule, Schedule.month_id == Month.id)
            .filter(Schedule.status == "committed")
            .order_by(Month.year_month.desc())
            .first()
        )
    if month is None:
        return jsonify({"error": "no_committed_month",
                        "message": "No month has a committed schedule yet"}), 400

    days = _insurance_days(student.id, month.id)

    warnings = []
    if not student.insurance_number:
        warnings.append("No insurance number on record — the query step will need it typed in.")
    if not student.project_name:
        warnings.append("No project name set.")
    if student.funding_category is None:
        warnings.append("No funding source set.")
    if not days:
        warnings.append(f"No committed hours for {month.year_month}.")
    # situation.txt: the 1st of a month can't be insured through the normal
    # flow — it goes to Ms Betty by the 20th as a special request.
    if any(d["date"].endswith("-01") for d in days):
        warnings.append("Includes the 1st of the month — that day can't be applied for here; "
                        "send name, ID and hours to Ms Betty by the 20th instead.")

    return jsonify({
        "student": {
            "id": student.id,
            "chinese_name": student.chinese_name,
            "english_name": student.english_name,
            "student_id": student.student_id,
            "insurance_number": student.insurance_number,
            "project_name": student.project_name,
            "funding_category": student.funding_category,
            "funding_label": FUNDING_CATEGORIES.get(student.funding_category),
            "worker_type": student.worker_type,
            "worker_type_label": WORKER_TYPES.get(student.worker_type),
        },
        "month": month.to_dict(),
        "days": days,
        "total_hours": sum(d["hours"] for d in days),
        "warnings": warnings,
    })


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

    # Opening selection on a month with no slots would show students an
    # all-grey grid right after announcing it — build them from the plan first.
    slots_built = None
    if new_state == "selection_open" and not month_has_slots(month):
        slots_built = sync_month_slots(month)

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
    out["slots_built"] = slots_built
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
    db.session.flush()
    # Closing a day takes its slots with it straight away, so students can't
    # go on ticking it and the solver can't staff it (see utils/slot_sync).
    sync = _sync_if_built(month)
    db.session.commit()
    return jsonify(dict(cd.to_dict(), slot_sync=sync)), 201


@bp.delete("/closed-dates/<int:closed_date_id>")
@overseer_required
def delete_closed_date(closed_date_id):
    cd = ClosedDate.query.get_or_404(closed_date_id)
    month = cd.month
    db.session.delete(cd)
    db.session.flush()
    sync = _sync_if_built(month)  # reopening the day gives it its slots back
    db.session.commit()
    return jsonify({"ok": True, "slot_sync": sync})


def _sync_if_built(month):
    """After a plan change: bring the slots in line — but only once the month
    has slots at all. Before that, the plan is still being drawn up and
    building slots is its own deliberate step."""
    if month is None or not month_has_slots(month):
        return None
    return sync_month_slots(month, max_lost=None if _sync_confirmed() else MAX_LOST_DEFAULT)


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


def _cell_track(data):
    """The lane a regular-schedule edit applies to. Absent means the paid
    lane, which is what every row was before lanes existed."""
    track = (data.get("track") or DEFAULT_TRACK).upper()
    return track if track in TRACKS else None


def _student_in_track(student_id, track):
    """A lane is the worker type, strictly — an OW can't hold an SW hour.
    Checked here so a bad request can't write a cross-lane standing claim the
    solver would then try to honour."""
    student = Student.query.get(student_id)
    if student is None:
        return False
    return track_for(student) == track


@bp.put("/regular-template/cell")
@overseer_required
def set_regular_template_cell():
    data = request.get_json(force=True) or {}
    weekday = data.get("weekday")
    hour = data.get("hour")
    state = data.get("state")
    track = _cell_track(data)
    if weekday is None or hour is None or state not in REGULAR_SLOT_STATES or track is None:
        return jsonify({"error": "invalid_cell"}), 400
    student_id = data.get("student_id") if state == "assigned" else None
    if state == "assigned" and not student_id:
        return jsonify({"error": "student_id_required"}), 400
    if student_id and not _student_in_track(student_id, track):
        return jsonify({"error": "wrong_track",
                        "message": f"That student doesn't work the {TRACKS[track]} lane"}), 400

    row = RegularSlotTemplate.query.filter_by(weekday=weekday, hour=hour, track=track).first()
    if not row:
        row = RegularSlotTemplate(weekday=weekday, hour=hour, track=track)
        db.session.add(row)
    row.state = state
    row.student_id = student_id
    db.session.commit()
    return jsonify(row.to_dict())


@bp.delete("/regular-template/cell")
@overseer_required
def clear_regular_template_cell():
    """Remove a cell from the pattern entirely.

    Not the same as marking it unavailable. In the SW lane an absent row means
    "the office wants no service worker this hour", which is the normal state
    for most of the week — without a way to delete, an SW cell could only ever
    be added, never taken back out."""
    data = request.get_json(force=True) or {}
    track = _cell_track(data)
    row = RegularSlotTemplate.query.filter_by(
        weekday=data.get("weekday"), hour=data.get("hour"), track=track).first()
    if not row:
        return jsonify({"ok": True, "deleted": False})
    db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True, "deleted": True})


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
    template_by_key = {(t.weekday, t.hour, t.track): t for t in RegularSlotTemplate.query.all()}
    existing = {(r.date, r.hour, r.track): r for r in RegularSlot.query.filter_by(month_id=month.id).all()}

    created = updated = deleted = 0
    wanted = set()
    for d in _month_weekdays_minus_closed(month):
        for hour in SLOT_HOURS:
            for track, default_on in TRACK_DEFAULT_ON.items():
                tmpl = template_by_key.get((d.weekday(), hour, track))
                if tmpl is None and not default_on:
                    # The SW lane is opt-in: no template cell means the office
                    # wants no service worker that hour. Writing an
                    # "unassigned" row instead would ask the solver to staff
                    # every hour of the month in both lanes.
                    continue
                wanted.add((d, hour, track))
                state = tmpl.state if tmpl else "unassigned"
                student_id = tmpl.student_id if tmpl and tmpl.state == "assigned" else None
                row = existing.get((d, hour, track))
                if row is None:
                    db.session.add(RegularSlot(month_id=month.id, date=d, hour=hour, track=track,
                                                state=state, student_id=student_id))
                    created += 1
                elif resync and (row.state, row.student_id) != (state, student_id):
                    row.state, row.student_id = state, student_id
                    updated += 1

    if resync:
        # A resync is "make this month match the template". An opt-in lane can
        # lose a cell, and leaving the month's copy behind would keep staffing
        # an hour the pattern no longer asks for.
        for key, row in existing.items():
            if key not in wanted:
                db.session.delete(row)
                deleted += 1

    db.session.flush()
    sync = _sync_if_built(month)
    db.session.commit()
    return jsonify({"created": created, "updated": updated, "deleted": deleted,
                    "resync": resync, "slot_sync": sync}), 201


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
    if student_id and not _student_in_track(student_id, row.track):
        return jsonify({"error": "wrong_track",
                        "message": f"That student doesn't work the {TRACKS[row.track]} lane"}), 400
    row.state = state
    row.student_id = student_id
    db.session.flush()
    sync = _sync_if_built(row.month)
    db.session.commit()
    return jsonify(dict(row.to_dict(), slot_sync=sync))


@bp.delete("/regular-slots/<int:regular_slot_id>")
@overseer_required
def delete_regular_slot(regular_slot_id):
    """Take an hour out of a lane for this month — the "no service worker
    this hour" action. In the SW lane an absent row is the normal state, so
    marking it unavailable isn't the same thing and isn't enough."""
    row = RegularSlot.query.get_or_404(regular_slot_id)
    month = row.month
    db.session.delete(row)
    db.session.flush()
    sync = _sync_if_built(month)
    db.session.commit()
    return jsonify({"ok": True, "slot_sync": sync})


# ---------------- Selection progress (who has answered for a month) ----------------

@bp.get("/months/<int:month_id>/selection-progress")
@overseer_required
def selection_progress(month_id):
    """Per student, while availability is open (and after): have they
    answered, how many of their regular hours they confirmed, how many extra
    hours they offered. Shown at the top of Draft review — the page the
    overseer builds the schedule from once everyone has answered.

    "Regular hours" counts only regular hours that exist as slots this month
    (a closed day or an hour made unavailable isn't one). A regular hour left
    unticked by someone who HAS answered is confirmed free — it needs cover;
    one belonging to someone who hasn't answered yet is still undecided."""
    month = Month.query.get_or_404(month_id)
    slots = Slot.query.filter_by(month_id=month.id).all()
    slot_id_by_key = {(s.date, s.hour, s.track): s.id for s in slots}
    month_slot_ids = list(slot_id_by_key.values())

    regular = defaultdict(set)   # student -> {slot_id} of their regular hours
    for r in RegularSlot.query.filter_by(month_id=month.id, state="assigned").all():
        slot_id = slot_id_by_key.get((r.date, r.hour, r.track))
        if r.student_id and slot_id:
            regular[r.student_id].add(slot_id)

    offered = defaultdict(set)
    last_saved = {}
    if month_slot_ids:
        for a in Availability.query.filter(Availability.slot_id.in_(month_slot_ids)).all():
            offered[a.student_id].add(a.slot_id)
            if a.student_id not in last_saved or a.submitted_at > last_saved[a.student_id]:
                last_saved[a.student_id] = a.submitted_at
    opted_out = {}
    for o in AvailabilityOptOut.query.filter_by(month_id=month.id).all():
        opted_out[o.student_id] = o.created_at

    # The live roster, plus anyone this month already involves (e.g. someone
    # deactivated after answering) — never silently drop an answer.
    involved = set(regular) | set(offered) | set(opted_out)
    students = Student.query.filter(
        db.or_(db.and_(Student.is_active.is_(True), Student.is_demo.is_(False)),
               Student.id.in_(involved) if involved else db.false())
    ).all()

    rows = []
    for s in students:
        mine_regular, mine_offered = regular.get(s.id, set()), offered.get(s.id, set())
        if mine_offered:
            answer = "answered"
        elif s.id in opted_out:
            answer = "no_hours"
        else:
            answer = "not_answered"
        confirmed = len(mine_regular & mine_offered)
        saved_at = last_saved.get(s.id) or opted_out.get(s.id)
        rows.append({
            "student": s.to_dict(),
            "answer": answer,
            "regular_hours": len(mine_regular),
            "regular_confirmed": confirmed if answer != "not_answered" else None,
            "regular_need_cover": len(mine_regular) - confirmed if answer != "not_answered" else None,
            "extra_offered": len(mine_offered - mine_regular),
            "total_offered": len(mine_offered),
            "last_saved": saved_at.isoformat() if saved_at else None,
        })
    order = {"not_answered": 0, "no_hours": 1, "answered": 2}
    rows.sort(key=lambda r: (order[r["answer"]], (r["student"]["english_name"] or r["student"]["chinese_name"]).lower()))

    window = SelectionWindow.query.filter_by(month_id=month.id).first()
    return jsonify({
        "month": month.to_dict(),
        "closes_at": window.closes_at.isoformat() if window and window.closes_at else None,
        "summary": {
            "students": len(rows),
            "answered": sum(r["answer"] == "answered" for r in rows),
            "no_hours": sum(r["answer"] == "no_hours" for r in rows),
            "not_answered": sum(r["answer"] == "not_answered" for r in rows),
            "regular_need_cover": sum(r["regular_need_cover"] or 0 for r in rows),
        },
        "rows": rows,
    })


# ---------------- Slot generation ----------------

@bp.post("/months/<int:month_id>/generate-slots")
@overseer_required
def generate_slots(month_id):
    """Build this month's slots from its plan, or bring existing ones back in
    line with it. The same call does both — see utils/slot_sync.

    Every hour of every open weekday gets a slot, except hours the month's
    regular schedule marks unavailable (not every hour needs staffing); cells
    with no regular_slot row (populate never run) just get a slot.

    There used to be a separate "Regenerate" that deleted the whole month's
    slots and every student's selections to apply any change. Sync replaces
    it: it only touches hours whose place in the plan changed."""
    month = Month.query.get_or_404(month_id)
    summary = sync_month_slots(month, max_lost=None if _sync_confirmed() else MAX_LOST_DEFAULT)
    db.session.commit()
    current_app.logger.info("slot sync for %s: %s", month.year_month, summary)
    return jsonify(summary), 201


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


def _report_rows(month):
    """The close-out report's per-student rows, joined to the overseer-only
    student fields (insurance number, worker type) the reimbursement paperwork
    needs. Demo students are left out — this file goes to the office as a
    record of money owed, and a seeded name has no business on it."""
    report = build_month_dashboard(month)
    students = {s.id: s for s in Student.query.all()}
    rows = []
    for row in report["gap"]:
        s = students.get(row["student_id"])
        if s is None or s.is_demo:
            continue
        rows.append((s, row))
    rows.sort(key=lambda pair: (pair[0].short_name or "").lower())
    return rows


@bp.get("/months/<int:month_id>/report.csv")
@overseer_required
def month_report_csv(month_id):
    """The close-out report as a file, for the salary/insurance paperwork.

    Built from the same build_month_dashboard() call the on-screen report
    uses, so the file and the screen cannot disagree. The report itself is
    derived, never stored (SCHEMA.md 'Derived values') — this download is the
    only way a month's numbers leave the database.
    """
    month = Month.query.get_or_404(month_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Chinese name", "English name", "Student ID", "Insurance number",
                "Worker type", "Scheduled hours", "Recorded hours", "Gap",
                "No-shows", "Approved leave"])
    for s, row in _report_rows(month):
        w.writerow([
            s.chinese_name, s.english_name, s.student_id,
            s.insurance_number or "", s.worker_type or "",
            row["scheduled_hours"], row["recorded_hours"], row["gap"],
            row.get("no_shows", 0), row.get("leave_approved", 0),
        ])

    # utf-8-sig, not utf-8: Excel on Windows reads a bare UTF-8 CSV as the
    # system codepage and turns every Chinese name into mojibake. The BOM is
    # what makes the file open cleanly by double-click.
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="oia-hours-{month.year_month}.csv"'},
    )


@bp.post("/months/<int:month_id>/report/notify")
@overseer_required
def month_report_notify(month_id):
    """Post the month's hours summary to the student group, so students have
    their own record of what was counted for them.

    Overseer-triggered, never from /tick — a report goes out when the overseer
    has finished checking it, not when a clock says so. notify_once dedups on
    the month, so a second click is a no-op rather than a second push; a
    previous *failed* attempt does retry.
    """
    month = Month.query.get_or_404(month_id)
    sent = notify_month_report(month, _report_rows(month))
    return jsonify({"ok": True, "sent": sent,
                    "reason": None if sent else "already_sent"})


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
        "solver_weights": get_solver_weights(),
        "solver_session_rules": get_session_rules(),
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
        set_setting("solver_weights", {k: int(v) for k, v in data["solver_weights"].items()})
    if "solver_session_rules" in data:
        rules = data["solver_session_rules"]
        lo, hi = int(rules.get("min_hours", 2)), int(rules.get("max_hours", 4))
        if not 1 <= lo <= hi:
            return jsonify({"error": "invalid_session_rules",
                            "message": "Session hours: 1 ≤ minimum ≤ maximum"}), 400
        set_setting("solver_session_rules", {
            "min_hours": lo, "max_hours": hi,
            "short_as_last_resort": bool(rules.get("short_as_last_resort", True)),
            "allow_same_day_double": bool(rules.get("allow_same_day_double", True)),
        })
    if "solver_floor_hours" in data:
        set_setting("solver_floor_hours", int(data["solver_floor_hours"]))
    if "timecard_cadence" in data:
        set_setting("timecard_cadence", data["timecard_cadence"])
    if "notify_attendance_events" in data:
        set_setting("notify_attendance_events", bool(data["notify_attendance_events"]))
    return get_settings()
