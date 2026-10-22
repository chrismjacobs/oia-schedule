"""Closing a date that already has a committed roster.

A holiday added after commit used to leave every committed student on it:
the slot sync keeps anything a committed assignment hangs off, so the only way
off was filing leave for a day the office wasn't even open.
"""
from datetime import date, datetime

from app.extensions import db
from app.models import Slot, ClosedDate, Availability, Schedule, Assignment, SessionHour, AttendanceSession
from app.utils.slot_sync import sync_month_slots

from tests.test_tracks import make_month, make_student

HOLIDAY = date(2026, 10, 26)


def _committed_day(month, student):
    sync_month_slots(month)
    schedule = Schedule(month_id=month.id, status="committed")
    db.session.add(schedule)
    db.session.flush()
    slots = Slot.query.filter_by(month_id=month.id, date=HOLIDAY, track="OW").filter(Slot.hour < 12).all()
    for slot in slots:
        db.session.add(Availability(student_id=student.id, slot_id=slot.id))
        db.session.add(Assignment(schedule_id=schedule.id, slot_id=slot.id, student_id=student.id,
                                  source="solver"))
    db.session.commit()
    return slots


def test_closing_a_committed_day_releases_the_student(app):
    month = make_month()
    doris = make_student(1, "OW")
    _committed_day(month, doris)

    db.session.add(ClosedDate(month_id=month.id, date=HOLIDAY, reason="National Holiday"))
    db.session.flush()
    summary = sync_month_slots(month)
    db.session.commit()

    assert summary["released"] == [{"student": doris.short_name, "hours": 4, "dates": [HOLIDAY.isoformat()]}]
    assert summary["kept"] == []
    assert summary["lost_picks"] == [], "a released student isn't reported twice"
    assert Slot.query.filter_by(month_id=month.id, date=HOLIDAY).count() == 0
    assert Assignment.query.count() == 0


def test_recorded_hours_on_a_closed_day_are_kept(app):
    month = make_month()
    doris = make_student(1, "OW")
    slots = _committed_day(month, doris)
    session = AttendanceSession(student_id=doris.id, date=HOLIDAY,
                                signed_in_at=datetime(2026, 10, 26, 8))
    db.session.add(session)
    db.session.flush()
    db.session.add(SessionHour(session_id=session.id, slot_id=slots[0].id))
    db.session.add(ClosedDate(month_id=month.id, date=HOLIDAY, reason="National Holiday"))
    db.session.flush()

    summary = sync_month_slots(month)
    db.session.commit()

    # The hour she actually worked stays on the roster; the other three go.
    assert summary["released"][0]["hours"] == 3
    assert Assignment.query.filter_by(slot_id=slots[0].id).count() == 1
