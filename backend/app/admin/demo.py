"""Demo/seed data (CLAUDE.md #17, SCHEMA.md 'Demo / seed data').

`flask seed-demo` (and the Setup page's "Seed demo data" button) populate the
app so the grid renders like `roster-mockup.html` on first load. Every row is
tagged `is_demo` (student, schedule) so "Reset demo data" can remove exactly
this and nothing else — real students and schedules are never touched.

6 students, matching the real roster's actual scale (under 10) rather than
the mockup's illustrative 9 — small enough that no colour ever needs to
repeat (8-colour palette, exhausted only past 8 students; see identity.py).
The weekly template below is a hand-built 6-person analogue of the mockup's
own wkData, preserving its shape: everyone gets a plausible spread of hours,
a couple of cells stay open (hatched), and the Monday "showcase day" tells
the same story as the mockup's day view (a no-show, an open slot).
"""
import calendar
from datetime import date as date_cls, datetime

from app.extensions import db
from app.utils.tz import local_now, local_today
from app.models import (
    Semester, Student, Month, Slot, Availability, Schedule, Assignment,
    AttendanceSession, HourlyReport, SLOT_HOURS,
)
from app.schedule.solver import solve_month
from app.utils.settings import get_solver_weights, get_floor_hours

DEMO_SEMESTER_NAME = "Demo"

# (english_name, chinese_name, colour, shape, student_id)
DEMO_STUDENTS = [
    ("Wei-Chen", "陳威辰", "#0072B2", "circle",   "90000001"),
    ("Sandy",    "林思妤", "#E69F00", "triangle", "90000002"),
    ("Kevin",    "黃冠宇", "#009E73", "square",   "90000003"),
    ("Amy",      "張書瑋", "#D55E00", "diamond",  "90000004"),
    ("Grace",    "李佳穎", "#7E57C2", "circle",   "90000005"),
    ("Ethan",    "吳承恩", "#0EA5A5", "triangle", "90000006"),
]

# Weekly template: [hour_index][weekday Mon-Fri] -> student index into DEMO_STUDENTS, or None (open).
WEEK_TEMPLATE = [
    [0, 0, 3, 1, 2],
    [0, 5, 3, 1, 2],
    [1, 5, 4, None, 0],
    [1, 3, 4, None, 0],
    [2, 4, 5, 3, 1],
    [2, 4, 5, 3, 1],
    [None, 0, 2, 5, None],
    [4, 2, 0, 5, 3],
]


def _first_free_year_month():
    """Never touch a real month — pick the first calendar month (starting this
    one) that has no Month row yet."""
    today = local_today()
    year, month = today.year, today.month
    while True:
        ym = f"{year:04d}-{month:02d}"
        if not Month.query.filter_by(year_month=ym).first():
            return ym
        month += 1
        if month > 12:
            month = 1
            year += 1


def _weekdays_in_month(year, month):
    _, days_in_month = calendar.monthrange(year, month)
    for day in range(1, days_in_month + 1):
        d = date_cls(year, month, day)
        if d.weekday() < 5:
            yield d


def reset_demo():
    """Delete every is_demo row, FK-safe order, leaving real data untouched."""
    demo_student_ids = [s.id for s in Student.query.filter_by(is_demo=True).all()]
    demo_schedule_ids = [s.id for s in Schedule.query.filter_by(is_demo=True).all()]

    if demo_student_ids:
        sessions = AttendanceSession.query.filter(AttendanceSession.student_id.in_(demo_student_ids)).all()
        for sess in sessions:
            HourlyReport.query.filter_by(session_id=sess.id).delete(synchronize_session=False)
        AttendanceSession.query.filter(AttendanceSession.student_id.in_(demo_student_ids)).delete(synchronize_session=False)
        Availability.query.filter(Availability.student_id.in_(demo_student_ids)).delete(synchronize_session=False)
        Assignment.query.filter(Assignment.student_id.in_(demo_student_ids)).delete(synchronize_session=False)

        from app.models import LeaveRequest, ReopenedSlot, TaskCompletion, CustomTask, TimecardUpload
        ReopenedSlot.query.filter(ReopenedSlot.claimed_by.in_(demo_student_ids)).update(
            {"claimed_by": None, "claimed_at": None}, synchronize_session=False)
        LeaveRequest.query.filter(LeaveRequest.student_id.in_(demo_student_ids)).delete(synchronize_session=False)
        TaskCompletion.query.filter(TaskCompletion.student_id.in_(demo_student_ids)).delete(synchronize_session=False)
        CustomTask.query.filter(CustomTask.claimed_by.in_(demo_student_ids)).update(
            {"claimed_by": None, "claimed_at": None}, synchronize_session=False)
        TimecardUpload.query.filter(TimecardUpload.student_id.in_(demo_student_ids)).delete(synchronize_session=False)

    if demo_schedule_ids:
        Assignment.query.filter(Assignment.schedule_id.in_(demo_schedule_ids)).delete(synchronize_session=False)
        Schedule.query.filter(Schedule.id.in_(demo_schedule_ids)).delete(synchronize_session=False)

    if demo_student_ids:
        Student.query.filter(Student.id.in_(demo_student_ids)).delete(synchronize_session=False)

    db.session.commit()
    return {"students_removed": len(demo_student_ids), "schedules_removed": len(demo_schedule_ids)}


def seed_demo():
    """Idempotent — safe to call repeatedly; clears any previous demo data first."""
    reset_demo()

    semester = Semester.query.filter_by(name=DEMO_SEMESTER_NAME).first()
    if not semester:
        today = local_today()
        semester = Semester(
            name=DEMO_SEMESTER_NAME,
            starts_on=today.replace(month=1, day=1),
            ends_on=today.replace(month=12, day=31),
            is_active=False,  # never hijacks real student registration
        )
        db.session.add(semester)
        db.session.flush()

    students = []
    for english, chinese, colour, shape, student_id in DEMO_STUDENTS:
        s = Student(
            semester_id=semester.id, chinese_name=chinese, english_name=english,
            student_id=student_id, colour=colour, shape=shape, is_demo=True,
        )
        db.session.add(s)
        students.append(s)
    db.session.flush()

    year_month = _first_free_year_month()
    year, month_num = (int(x) for x in year_month.split("-"))
    month = Month(year_month=year_month, state="running")
    db.session.add(month)
    db.session.flush()

    slots_by_date_hour = {}
    for d in _weekdays_in_month(year, month_num):
        for hour in SLOT_HOURS:
            period = "morning" if hour < 12 else "afternoon"
            slot = Slot(month_id=month.id, date=d, hour=hour, period=period, state="open")
            db.session.add(slot)
            slots_by_date_hour[(d, hour)] = slot
    db.session.flush()

    now = local_now()
    for (d, hour), slot in slots_by_date_hour.items():
        hour_idx = SLOT_HOURS.index(hour)
        student_idx = WEEK_TEMPLATE[hour_idx][d.weekday()]
        if student_idx is not None:
            db.session.add(Availability(student_id=students[student_idx].id, slot_id=slot.id, submitted_at=now))
    db.session.commit()

    weights = get_solver_weights()
    floor_hours = get_floor_hours()
    result, meta = solve_month(month.id, weights, floor_hours)

    schedule = Schedule(
        month_id=month.id, status="committed", generated_at=now, committed_at=now,
        solver_weights={"weights": weights, "floor_hours": floor_hours, "meta": meta},
        is_demo=True,
    )
    db.session.add(schedule)
    db.session.flush()

    assignment_by_slot = {}
    for slot_id, student_id in result.items():
        a = Assignment(schedule_id=schedule.id, slot_id=slot_id, student_id=student_id, source="solver")
        db.session.add(a)
        assignment_by_slot[slot_id] = a

    for (d, hour), slot in slots_by_date_hour.items():
        slot.state = "assigned" if slot.id in assignment_by_slot else "open"
    db.session.commit()

    # --- Showcase day: the first Monday, seeded with attendance so the Day
    # view tells the same story as the mockup (a no-show, an open slot). ---
    showcase = next((d for d in _weekdays_in_month(year, month_num) if d.weekday() == 0), None)
    if showcase:
        _seed_showcase_attendance(showcase, slots_by_date_hour, students)

    return {"month": month.to_dict(), "students": len(students), "meta": meta, "showcase_date": showcase.isoformat() if showcase else None}


def _seed_showcase_attendance(showcase_date, slots_by_date_hour, students):
    """Matches WEEK_TEMPLATE's Monday column: Wei-Chen 8-10, Sandy 10-12 with
    an 11:00 no-show, Kevin 13-15, an open 15:00, Grace 16-17."""
    def dt(hour):
        return datetime.combine(showcase_date, datetime.min.time()).replace(hour=hour)

    # Wei-Chen: 08:00-10:00, both hours recorded.
    _record_session(students[0], showcase_date, dt(8), dt(10), [slots_by_date_hour[(showcase_date, 8)], slots_by_date_hour[(showcase_date, 9)]])
    # Sandy: signs in for 10:00 only — 11:00 stays a no-show (assigned, never reported).
    _record_session(students[1], showcase_date, dt(10), dt(11), [slots_by_date_hour[(showcase_date, 10)]])
    # Kevin: 13:00-15:00, both hours recorded.
    _record_session(students[2], showcase_date, dt(13), dt(15), [slots_by_date_hour[(showcase_date, 13)], slots_by_date_hour[(showcase_date, 14)]])
    # 15:00 stays open/uncovered (no assignment was made — see WEEK_TEMPLATE).
    # Grace: 16:00-17:00.
    _record_session(students[4], showcase_date, dt(16), dt(17), [slots_by_date_hour[(showcase_date, 16)]])
    db.session.commit()


def _record_session(student, date, signed_in_at, signed_out_at, slots):
    session = AttendanceSession(
        student_id=student.id, date=date, signed_in_at=signed_in_at, signed_out_at=signed_out_at, flagged=False,
    )
    db.session.add(session)
    db.session.flush()
    for slot in slots:
        db.session.add(HourlyReport(session_id=session.id, slot_id=slot.id, note="Demo data — front desk coverage."))
