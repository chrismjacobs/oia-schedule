from datetime import datetime, timedelta

from flask import jsonify, request, current_app
from flask_login import current_user

from app.attendance import bp
from app.extensions import db
from app.models import (
    Slot, Assignment, Schedule, AttendanceSession, SessionHour,
    RegularTask, TaskCompletion, CustomTask, Month, Availability, AvailabilityOptOut, SelectionWindow,
)
from app.notifications.service import notify_signed_in, notify_signed_out
from app.utils.decorators import login_required_api
from app.utils.periods import period_key_for
from app.utils.runs import contiguous_runs
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


def _covered_slot_ids(student_id, on_date):
    """Hours already recorded by an earlier session today — never covered
    twice, or the dashboard's recorded hours would double-count."""
    return {
        sh.slot_id
        for sh in SessionHour.query.join(
            AttendanceSession, SessionHour.session_id == AttendanceSession.id
        ).filter(
            AttendanceSession.student_id == student_id,
            AttendanceSession.date == on_date,
        ).all()
    }


def _runs(assignments):
    """The day's assignments split into contiguous runs of hours. One session
    spans one run (CLAUDE.md #9) — 08:00-12:00 is one run, and a separate
    14:00-16:00 block is another, each with its own sign-in/out and report.
    The lunch break makes 11:00 and 13:00 non-contiguous on its own."""
    return contiguous_runs(assignments, lambda a: a.slot.hour)


def _run_for_session(assignments, session):
    """Which run a session is covering: the one its sign-in falls into
    (sign-in opens a few minutes early, so the window starts before the run
    does). A sign-in that matches no run — an unscheduled or very late one —
    falls back to the first run still missing hours."""
    opens_before = timedelta(minutes=current_app.config["SIGN_IN_OPENS_MINUTES_BEFORE"])
    covered = _covered_slot_ids(session.student_id, session.date)
    runs = _runs(assignments)

    for run in runs:
        start = _slot_start(run[0].slot) - opens_before
        end = _slot_start(run[-1].slot) + timedelta(hours=1)
        if start <= session.signed_in_at <= end:
            return run
    for run in runs:
        if any(a.slot_id not in covered for a in run):
            return run
    return []


def _open_sessions_before(student_id, on_date):
    """Sessions left open on an earlier day — a forgotten sign-out. Until one
    is closed it records nothing, so the whole run reads as a
    scheduled-vs-recorded gap on the dashboard even though the student worked
    it (CLAUDE.md #1). Surfaced to the student so they can close it late,
    rather than the overseer's flag being the only thing that ever happens."""
    return (
        AttendanceSession.query.filter(
            AttendanceSession.student_id == student_id,
            AttendanceSession.signed_out_at.is_(None),
            AttendanceSession.date < on_date,
        )
        .order_by(AttendanceSession.date)
        .all()
    )


def _session_run_hours(session):
    """The hours a still-open session would record, on its own day."""
    assignments = _todays_assignments(session.student_id, session.date)
    covered = _covered_slot_ids(session.student_id, session.date)
    return [a.slot for a in _run_for_session(assignments, session)
            if a.slot_id not in covered]


def _default_late_signout(session):
    """The end time a late sign-out is pre-filled with: the end of the run the
    session covers. It is a suggestion the student can change, not a guess the
    app makes for them — CLAUDE.md #9 forbids auto-closing at a guessed time,
    and the difference is that a human confirms this one."""
    hours = _session_run_hours(session)
    if hours:
        return _slot_start(hours[-1]) + timedelta(hours=1)
    # Unscheduled session: nothing to derive an end from, so offer the hour
    # after sign-in and let them correct it.
    return session.signed_in_at + timedelta(hours=1)


@bp.get("/today")
@login_required_api
def today():
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    today_date = local_today()
    assignments = _todays_assignments(current_user.student_id, today_date)
    now = local_now()
    opens_before = timedelta(minutes=current_app.config["SIGN_IN_OPENS_MINUTES_BEFORE"])
    covered = _covered_slot_ids(current_user.student_id, today_date)

    slot_rows = []
    for a in sorted(assignments, key=lambda a: a.slot.hour):
        slot = a.slot
        start = _slot_start(slot)
        slot_rows.append({
            **slot.to_dict(),
            "sign_in_open": start - opens_before <= now <= start + timedelta(hours=1),
            "covered": slot.id in covered,
        })

    open_session = AttendanceSession.query.filter_by(
        student_id=current_user.student_id, date=today_date, signed_out_at=None
    ).first()

    # The hours this session will record at sign-out — shown up front so the
    # student writes one report knowing exactly what it covers.
    session_hours = []
    if open_session:
        session_hours = [
            a.slot.to_dict() for a in _run_for_session(assignments, open_session)
            if a.slot_id not in covered
        ]

    # Sessions already closed today, with the report each one carried — so a
    # second block in the same day doesn't look like the first went missing.
    earlier = (
        AttendanceSession.query.filter(
            AttendanceSession.student_id == current_user.student_id,
            AttendanceSession.date == today_date,
            AttendanceSession.signed_out_at.isnot(None),
        )
        .order_by(AttendanceSession.signed_in_at)
        .all()
    )
    earlier_rows = [
        dict(s.to_dict(), hours=[sh.slot.to_dict() for sh in
                                 sorted(s.hours, key=lambda sh: sh.slot.hour)])
        for s in earlier
    ]

    # Due-or-overdue event-dated custom tasks — banners front-and-centre on
    # sign-in for every student, not just whoever eventually claims it
    # (CLAUDE.md #10). Stays visible (not hidden) once the date passes and
    # it's still open, so it can't silently fall through the cracks.
    due_tasks = (
        CustomTask.query.filter(
            CustomTask.event_date.isnot(None),
            CustomTask.event_date <= today_date,
            CustomTask.status.in_(("open", "claimed")),
        )
        .order_by(CustomTask.event_date)
        .all()
    )

    return jsonify({
        "date": today_date.isoformat(),
        "scheduled_slots": slot_rows,
        "open_session": open_session.to_dict() if open_session else None,
        "session_hours": session_hours,
        "earlier_sessions": earlier_rows,
        "due_tasks": [
            dict(t.to_dict(), overdue=t.event_date < today_date) for t in due_tasks
        ],
        "availability_reminders": _availability_reminders(current_user.student_id),
        # Forgotten sign-outs from earlier days, still closable (CLAUDE.md #9).
        "stale_sessions": [
            dict(
                s.to_dict(),
                hours=[slot.to_dict() for slot in _session_run_hours(s)],
                default_signed_out_at=_default_late_signout(s).isoformat(),
            )
            for s in _open_sessions_before(current_user.student_id, today_date)
        ],
    })


def _availability_reminders(student_id):
    """Months open for selection that this student hasn't answered yet —
    no hours picked and no "no hours this month". Shown on the sign-in/out
    page, the one screen every working student is guaranteed to open, so an
    unanswered month can't slip past someone who never reads the group chat."""
    out = []
    for month in Month.query.filter_by(state="selection_open").order_by(Month.year_month).all():
        if AvailabilityOptOut.query.filter_by(student_id=student_id, month_id=month.id).first():
            continue
        picked = (Availability.query.join(Slot, Availability.slot_id == Slot.id)
                  .filter(Availability.student_id == student_id, Slot.month_id == month.id)
                  .first())
        if picked:
            continue
        window = SelectionWindow.query.filter_by(month_id=month.id).first()
        out.append({
            "month_id": month.id,
            "year_month": month.year_month,
            "closes_at": window.closes_at.isoformat() if window and window.closes_at else None,
        })
    return out


@bp.get("/history")
@login_required_api
def history():
    """Past sign-in/out sessions with their report and completed tasks —
    students see their own; the overseer can pass ?student_id= to review
    anyone's (this is the answer to "where can I see my/their past reports",
    not just today's live session). One report per session, covering the whole
    run of hours it recorded."""
    days = min(request.args.get("days", 30, type=int), 90)
    since = local_today() - timedelta(days=days)

    if current_user.role == "overseer":
        student_id = request.args.get("student_id", type=int)
        if not student_id:
            return jsonify({"error": "student_id_required"}), 400
    else:
        if not current_user.student_id:
            return jsonify({"error": "students_only"}), 403
        student_id = current_user.student_id

    sessions = (
        AttendanceSession.query.filter(
            AttendanceSession.student_id == student_id, AttendanceSession.date >= since
        )
        .order_by(AttendanceSession.date.desc(), AttendanceSession.signed_in_at.desc())
        .all()
    )

    out = []
    for s in sessions:
        hours = sorted(s.hours, key=lambda sh: sh.slot.hour)
        regular_done = TaskCompletion.query.filter_by(session_id=s.id).all()
        custom_done = CustomTask.query.filter_by(session_id=s.id).all()
        d = s.to_dict()
        d["hours"] = [sh.slot.to_dict() for sh in hours]
        d["regular_tasks_done"] = [t.regular_task.to_dict() for t in regular_done]
        d["custom_tasks_done"] = [t.to_dict() for t in custom_done]
        out.append(d)

    return jsonify(out)


@bp.post("/sign-in")
@login_required_api
def sign_in():
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    today_date = local_today()

    # Every outcome is logged: a sign-in that never records looks exactly like
    # a broken notification, and until now the difference was invisible.
    current_app.logger.info(
        "sign-in attempt | student=%s | date=%s", current_user.student_id, today_date)

    existing = AttendanceSession.query.filter_by(
        student_id=current_user.student_id, date=today_date, signed_out_at=None
    ).first()
    if existing:
        current_app.logger.info(
            "sign-in REFUSED session_already_open | student=%s | session #%s open since %s",
            current_user.student_id, existing.id, existing.signed_in_at)
        return jsonify({"error": "session_already_open", "session": existing.to_dict()}), 409

    assignments = _todays_assignments(current_user.student_id, today_date)

    # Multiple separate shift blocks in one day are legitimate (e.g. 8-10 and
    # 14-16, each its own sign-in/out) — but a repeat sign-in with nothing new
    # to cover isn't, and would double-count recorded hours on the dashboard.
    if assignments:
        covered = _covered_slot_ids(current_user.student_id, today_date)
        if all(a.slot_id in covered for a in assignments):
            current_app.logger.info(
                "sign-in REFUSED all_scheduled_hours_reported | student=%s | %d assignment(s) all covered",
                current_user.student_id, len(assignments))
            return jsonify({"error": "all_scheduled_hours_reported",
                             "message": "You've already reported all your scheduled hours today."}), 409

    now = local_now()

    session = AttendanceSession(student_id=current_user.student_id, date=today_date, signed_in_at=now)
    if not assignments:
        session.flagged = True
        session.flag_reason = "not_scheduled"
    db.session.add(session)
    db.session.commit()
    current_app.logger.info(
        "sign-in OK | student=%s | session #%s at %s | scheduled hours today=%d%s",
        current_user.student_id, session.id, now, len(assignments),
        " (FLAGGED not_scheduled)" if not assignments else "")
    notify_signed_in(session)
    return jsonify(session.to_dict()), 201


@bp.get("/tasks")
@login_required_api
def available_tasks():
    """Regular tasks not yet done this period + open custom tasks — shown at
    sign-out (CLAUDE.md #9).

    ?session_id= asks the question for that session's own day instead of
    today: a late sign-out for last Tuesday must offer the tasks that were
    still outstanding in Tuesday's cadence period, since that is the period
    the completion will be filed against."""
    today_date = local_today()
    session_id = request.args.get("session_id", type=int)
    if session_id and current_user.student_id:
        s = AttendanceSession.query.filter_by(
            id=session_id, student_id=current_user.student_id).first()
        if s:
            today_date = s.date
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


def _parse_late_signout(session, raw):
    """The end time a student gives for a forgotten sign-out. Accepts "16:00"
    or a full ISO timestamp; either way it is pinned to the session's own
    date, so a late sign-out can never claim a shift that ran for three days.
    Must be after sign-in and still inside that day. Returns None if it isn't
    usable — the caller asks again rather than substituting a guess."""
    if not raw:
        return None
    text = str(raw).strip()
    try:
        when = datetime.fromisoformat(text) if len(text) > 5 else datetime.combine(
            session.date, datetime.strptime(text, "%H:%M").time())
    except ValueError:
        return None
    when = datetime.combine(session.date, when.time())
    if when <= session.signed_in_at:
        return None
    return when


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
    """One report closes the session: a single write-up plus the tasks done
    anywhere across the run (CLAUDE.md #9). The hours recorded are derived
    from the run the session signed in against — the student isn't asked to
    account for them one by one, since a task begun at 09:40 and finished at
    10:10 belongs to neither hour on its own.

    A session left open on an earlier day can still be closed here — that is
    the late sign-out. It carries an explicit `signed_out_at` (the student
    says when they actually left) and is flagged `late_sign_out` so the
    overseer can see the report was written after the fact. The alternative
    was what happened before: the run recorded nothing at all and read as a
    full no-show on the dashboard."""
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    data = request.get_json(force=True) or {}
    session_id = data.get("session_id")
    note = (data.get("note") or "").strip() or None
    # regular_task_ids / custom_task_ids entries: either a bare id, or
    # {id, proof_s3_key} when photo_required and a photo was staged first.
    regular_entries = data.get("regular_task_ids") or []
    custom_entries = data.get("custom_task_ids") or []

    session = AttendanceSession.query.filter_by(
        id=session_id, student_id=current_user.student_id, signed_out_at=None
    ).first()
    if not session:
        return jsonify({"error": "no_open_session"}), 404

    now = local_now()
    late = session.date < local_today()
    if late:
        now = _parse_late_signout(session, data.get("signed_out_at"))
        if now is None:
            return jsonify({
                "error": "invalid_signed_out_at",
                "message": "Give the time you actually left, on the day of the shift.",
            }), 400
    skipped = []

    assignments = _todays_assignments(current_user.student_id, session.date)
    covered = _covered_slot_ids(current_user.student_id, session.date)
    run = _run_for_session(assignments, session)
    for a in run:
        if a.slot_id in covered:
            continue  # already recorded by an earlier session today
        db.session.add(SessionHour(session_id=session.id, slot_id=a.slot_id))

    for item in regular_entries:
        task_id, proof_key = _task_entry(item)
        task = RegularTask.query.get(task_id)
        if not task:
            continue
        if task.photo_required and not proof_key:
            skipped.append({"type": "regular", "id": task_id, "reason": "photo_required"})
            continue
        pk = period_key_for(task.frequency, session.date)
        if TaskCompletion.query.filter_by(regular_task_id=task.id, period_key=pk).first():
            continue  # already done this period elsewhere — silently skip, don't error the whole sign-out
        db.session.add(TaskCompletion(
            regular_task_id=task.id, student_id=current_user.student_id, session_id=session.id,
            completed_at=now, period_key=pk, proof_s3_key=proof_key,
        ))

    for item in custom_entries:
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
        ct.session_id = session.id
        if proof_key:
            ct.proof_s3_key = proof_key

    session.note = note
    session.signed_out_at = now
    if late:
        # Keep the flag — the overseer should still see that this report was
        # written days later, not while the student was in the office. It
        # just stops meaning "nothing was ever recorded" (CLAUDE.md #9).
        session.flagged = True
        session.flag_reason = "late_sign_out"
        current_app.logger.info(
            "late sign-out | student=%s | session #%s (%s) closed at %s",
            current_user.student_id, session.id, session.date, now)
    db.session.commit()
    notify_signed_out(session)
    result = session.to_dict()
    result["skipped"] = skipped
    return jsonify(result)
