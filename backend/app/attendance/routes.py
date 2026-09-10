from datetime import datetime, timedelta

from flask import jsonify, request, current_app
from flask_login import current_user

from app.attendance import bp
from app.extensions import db
from app.models import (
    Slot, Assignment, Schedule, AttendanceSession, SessionHour,
    RegularTask, TaskCompletion, CustomTask,
)
from app.notifications.service import notify_signed_in, notify_signed_out
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
    runs = []
    for a in sorted(assignments, key=lambda a: a.slot.hour):
        if runs and a.slot.hour == runs[-1][-1].slot.hour + 1:
            runs[-1].append(a)
        else:
            runs.append([a])
    return runs


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
    })


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
    """One report closes the session: a single write-up plus the tasks done
    anywhere across the run (CLAUDE.md #9). The hours recorded are derived
    from the run the session signed in against — the student isn't asked to
    account for them one by one, since a task begun at 09:40 and finished at
    10:10 belongs to neither hour on its own."""
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
    db.session.commit()
    notify_signed_out(session)
    result = session.to_dict()
    result["skipped"] = skipped
    return jsonify(result)
