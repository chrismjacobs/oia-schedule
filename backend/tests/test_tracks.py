"""Worker lanes (OW / SW).

The regressions these guard against are the quiet kind — a schedule that still
builds, still commits and still looks plausible, but is wrong. Chiefly: half
day bucketing that ignores the lane, which shreds every shift into one-hour
pieces, and a slot sync that mistakes every existing slot for an unplanned one
and deletes a month of student selections on the way past.
"""
from datetime import date, timedelta

import pytest

from app.extensions import db
from app.models import (
    Semester, Student, Month, Slot, RegularSlot, ClosedDate, Availability,
    Schedule, Assignment, SLOT_HOURS,
)
from app.utils.tracks import track_for, TRACKS, TRACK_DEFAULT_ON
from app.utils.slot_sync import (
    planned_hours, sync_month_slots, AvailabilityLossRefused,
)

YM = "2026-10"
PALETTE = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#7E57C2", "#0EA5A5"]


def make_student(n, worker_type=None):
    s = Student(semester_id=1, chinese_name=f"學生{n}", english_name=f"Stu{n}",
                student_id=f"T{n:06d}", colour=PALETTE[n % len(PALETTE)],
                shape="circle", worker_type=worker_type)
    db.session.add(s)
    db.session.flush()
    return s


def make_month():
    db.session.add(Semester(id=1, name="Test", starts_on=date(2026, 9, 1),
                            ends_on=date(2027, 1, 31), is_active=True))
    m = Month(year_month=YM, state="selection_open")
    db.session.add(m)
    db.session.flush()
    return m


# --------------------------------------------------------------- track_for

@pytest.mark.parametrize("worker_type,expected", [
    ("OW", "OW"), ("SW", "SW"), ("TA", "SW"), (None, "OW"), ("", "OW"),
])
def test_track_for_maps_ta_into_the_unpaid_lane(app, worker_type, expected):
    assert track_for(make_student(1, worker_type)) == expected


def test_only_two_lanes_exist(app):
    assert set(TRACKS) == {"OW", "SW"}
    assert TRACK_DEFAULT_ON == {"OW": True, "SW": False}


# ---------------------------------------------------------- slot generation

def _ow_reference(month):
    """What planned_hours produced for the paid lane before lanes existed:
    every weekday hour, minus closed dates, minus hours the regular grid
    marks unavailable."""
    from app.utils.periods import weekdays_in_month
    year, mon = (int(x) for x in month.year_month.split("-"))
    closed = {c.date for c in ClosedDate.query.filter(
        db.extract("year", ClosedDate.date) == year,
        db.extract("month", ClosedDate.date) == mon).all()}
    unavailable = {(r.date, r.hour) for r in RegularSlot.query.filter_by(
        month_id=month.id, state="unavailable", track="OW").all()}
    return {(d, h) for d in weekdays_in_month(month.year_month) if d not in closed
            for h in SLOT_HOURS if (d, h) not in unavailable}


def test_ow_plan_is_unchanged_by_the_lane_split(app):
    """The parity guarantee. If the paid lane's plan drifts by even one hour,
    a sync sees existing slots as unplanned and deletes the selections on
    them — which is exactly the way a month of student answers gets lost."""
    month = make_month()
    d = date(2026, 10, 5)
    db.session.add(ClosedDate(month_id=month.id, date=date(2026, 10, 9), reason="holiday"))
    db.session.add(RegularSlot(month_id=month.id, date=d, hour=16,
                               state="unavailable", track="OW"))
    db.session.flush()

    ow_plan = {(dd, hh) for dd, hh, tt in planned_hours(month) if tt == "OW"}
    assert ow_plan == _ow_reference(month)


def test_sw_lane_is_opt_in(app):
    month = make_month()
    db.session.flush()
    assert not [t for _, _, t in planned_hours(month) if t == "SW"]

    d = date(2026, 10, 5)
    db.session.add(RegularSlot(month_id=month.id, date=d, hour=13,
                               state="unassigned", track="SW"))
    db.session.flush()
    sw = {(dd, hh) for dd, hh, tt in planned_hours(month) if tt == "SW"}
    assert sw == {(d, 13)}, "an SW slot appears only where the pattern asks for one"


def test_sync_creates_one_slot_per_lane(app):
    month = make_month()
    d = date(2026, 10, 5)
    db.session.add(RegularSlot(month_id=month.id, date=d, hour=13,
                               state="unassigned", track="SW"))
    db.session.flush()
    sync_month_slots(month)
    db.session.commit()

    at_1300 = Slot.query.filter_by(month_id=month.id, date=d, hour=13).all()
    assert sorted(s.track for s in at_1300) == ["OW", "SW"]
    at_1400 = Slot.query.filter_by(month_id=month.id, date=d, hour=14).all()
    assert [s.track for s in at_1400] == ["OW"]


# ------------------------------------------------- the availability safety valve

def test_sync_refuses_to_wipe_everyones_selections(app):
    month = make_month()
    db.session.flush()
    sync_month_slots(month)
    db.session.commit()

    slots = Slot.query.filter_by(month_id=month.id).all()
    for n in range(1, 5):
        s = make_student(n, "OW")
        for slot in slots[:6]:
            db.session.add(Availability(student_id=s.id, slot_id=slot.id))
    db.session.commit()
    before = Availability.query.count()

    # Close every day of the month: the whole plan empties out.
    for d in {s.date for s in slots}:
        db.session.add(ClosedDate(month_id=month.id, date=d, reason="all shut"))
    db.session.flush()

    with pytest.raises(AvailabilityLossRefused) as err:
        sync_month_slots(month)
    assert len(err.value.lost_picks) == 4
    db.session.rollback()
    assert Availability.query.count() == before, "nothing was deleted on the way to refusing"


def test_sync_proceeds_once_confirmed(app):
    month = make_month()
    db.session.flush()
    sync_month_slots(month)
    db.session.commit()
    slots = Slot.query.filter_by(month_id=month.id).all()
    for n in range(1, 5):
        s = make_student(n, "OW")
        for slot in slots[:6]:
            db.session.add(Availability(student_id=s.id, slot_id=slot.id))
    for d in {s.date for s in slots}:
        db.session.add(ClosedDate(month_id=month.id, date=d, reason="all shut"))
    db.session.commit()

    summary = sync_month_slots(month, max_lost=None)
    db.session.commit()
    assert summary["removed"] > 0
    assert len(summary["lost_picks"]) == 4
    assert Availability.query.count() == 0


def _as_overseer(app):
    from app.models import User
    admin = User(username="boss", role="overseer")
    admin.set_password("x")
    db.session.add(admin)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True
    return client


def test_generate_slots_refuses_then_obeys_confirmation(app):
    """The October safeguard, end to end.

    A plan change that would empty the month must come back as a refusal with
    the selections still there, and go through only when the overseer sends
    the confirmation back.
    """
    month = make_month()
    db.session.flush()
    sync_month_slots(month)
    db.session.commit()
    slots = Slot.query.filter_by(month_id=month.id).all()
    for n in range(1, 5):
        s = make_student(n, "OW")
        for slot in slots[:6]:
            db.session.add(Availability(student_id=s.id, slot_id=slot.id))
    for d in {s.date for s in slots}:
        db.session.add(ClosedDate(month_id=month.id, date=d, reason="all shut"))
    db.session.commit()
    before = Availability.query.count()
    assert before > 0

    client = _as_overseer(app)

    res = client.post(f"/api/admin/months/{month.id}/generate-slots")
    assert res.status_code == 409
    body = res.get_json()
    assert body["error"] == "would_lose_availability"
    assert len(body["lost_picks"]) == 4
    assert Availability.query.count() == before, "the refusal changed nothing"

    res = client.post(f"/api/admin/months/{month.id}/generate-slots?confirm=true")
    assert res.status_code == 201
    assert Availability.query.count() == 0


# ------------------------------------------------------------------ solver

def test_half_days_keep_runs_intact_across_lanes(app):
    """The regression that matters most.

    _half_days buckets slots and the run builder walks each bucket testing
    `hour == previous + 1`. Put both lanes in one bucket and that test fails
    at every step, so a four hour morning comes out as four one-hour sessions
    for four different students — the hour-by-hour allocation CLAUDE.md #7
    says never to go back to.
    """
    from app.schedule.solver import _half_days, _candidate_sessions

    month = make_month()
    d = date(2026, 10, 5)
    for hour in (8, 9, 10, 11):
        db.session.add(RegularSlot(month_id=month.id, date=d, hour=hour,
                                   state="unassigned", track="SW"))
    db.session.flush()
    sync_month_slots(month)
    db.session.commit()

    morning = [s for s in Slot.query.filter_by(month_id=month.id, date=d).all()
               if s.period == "morning"]
    assert len(morning) == 8, "four hours in each of two lanes"

    buckets = _half_days(morning)
    assert len(buckets) == 2, "one bucket per lane"
    for key, slots in buckets.items():
        assert len({s.track for s in slots}) == 1
        assert [s.hour for s in slots] == [8, 9, 10, 11]

    student = make_student(1, "OW")
    offered = {student.id: {s.id for s in morning if s.track == "OW"}}
    rules = {"min_hours": 2, "max_hours": 4, "short_as_last_resort": True,
             "allow_same_day_double": True}
    lengths = {len(slot_ids) for _, _, slot_ids in
               _candidate_sessions(morning, offered, rules)}
    assert 4 in lengths, "a whole offered morning is still a candidate session"


def test_solver_fills_both_lanes_and_never_doubles_a_slot(app):
    from app.schedule.solver import solve_month

    month = make_month()
    d = date(2026, 10, 5)
    for hour in (8, 9, 10, 11):
        db.session.add(RegularSlot(month_id=month.id, date=d, hour=hour,
                                   state="unassigned", track="SW"))
    db.session.flush()
    sync_month_slots(month)
    db.session.commit()

    ow = make_student(1, "OW")
    sw = make_student(2, "SW")
    day_slots = Slot.query.filter_by(month_id=month.id, date=d).all()
    for slot in day_slots:
        holder = ow if slot.track == "OW" else sw
        db.session.add(Availability(student_id=holder.id, slot_id=slot.id))
    db.session.commit()

    weights = {"coverage": 1000, "floor_guarantee": 500, "short_session": 600,
               "per_session": 150, "same_day_double": 100, "low_churn": 10,
               "equalise_hours": 1}
    rules = {"min_hours": 2, "max_hours": 4, "short_as_last_resort": True,
             "allow_same_day_double": True}
    result, meta = solve_month(month.id, weights, floor_hours=4, rules=rules,
                               time_limit_seconds=10)

    by_id = {s.id: s for s in day_slots}
    assigned_today = {sid: stu for sid, stu in result.items() if sid in by_id}
    assert assigned_today, "the day was staffed"

    # One student per slot still holds — that is the whole point of lanes.
    assert len(assigned_today) == len(set(assigned_today)), "no slot assigned twice"
    for slot_id, student_id in assigned_today.items():
        assert track_for(db.session.get(Student, student_id)) == by_id[slot_id].track, \
            "nobody was placed in the other lane"

    lanes_used = {by_id[sid].track for sid in assigned_today}
    assert lanes_used == {"OW", "SW"}, "both lanes were staffed on the same day"

    # And the shifts are shifts, not a scatter of single hours.
    assert max(int(n) for n in meta["session_lengths"]) >= 4


# --------------------------------------------------------------- dashboard

def test_unfilled_sw_hour_counts_as_uncovered(app):
    """With lanes, a half-staffed hour needs no special counting: the empty
    seat is its own slot and falls out of the uncovered list on its own."""
    from app.dashboard.routes import build_month_dashboard

    month = make_month()
    d = date(2026, 10, 5)
    db.session.add(RegularSlot(month_id=month.id, date=d, hour=13,
                               state="unassigned", track="SW"))
    db.session.flush()
    sync_month_slots(month)
    db.session.commit()

    ow = make_student(1, "OW")
    schedule = Schedule(month_id=month.id, status="committed")
    db.session.add(schedule)
    db.session.flush()
    ow_slot = Slot.query.filter_by(month_id=month.id, date=d, hour=13, track="OW").one()
    sw_slot = Slot.query.filter_by(month_id=month.id, date=d, hour=13, track="SW").one()
    db.session.add(Assignment(schedule_id=schedule.id, slot_id=ow_slot.id,
                              student_id=ow.id, source="manual_edit"))
    db.session.commit()

    report = build_month_dashboard(month)
    uncovered = {s["id"] for s in report["uncovered_slots"]}
    assert sw_slot.id in uncovered, "the unstaffed unpaid seat is visibly uncovered"
    assert ow_slot.id not in uncovered


def test_dashboard_names_unclassified_students(app):
    """worker_type decides the lane, so an unset one must be visible rather
    than quietly resolving to the paid lane."""
    from app.dashboard.routes import build_month_dashboard

    month = make_month()
    make_student(1, "OW")
    nobody_knows = make_student(2, None)
    db.session.commit()

    flagged = build_month_dashboard(month)["unclassified_students"]
    assert [s["id"] for s in flagged] == [nobody_knows.id]


# ----------------------------------------------------------- lane strictness

def test_cross_lane_assignment_is_refused(app):
    """A paid worker cannot be put on an unpaid hour, even by hand. Without
    this the refusal would come from the availability check instead, which
    reads as a scheduling problem rather than a worker-type one."""
    month = make_month()
    d = date(2026, 10, 5)
    db.session.add(RegularSlot(month_id=month.id, date=d, hour=13,
                               state="unassigned", track="SW"))
    db.session.flush()
    sync_month_slots(month)

    ow = make_student(1, "OW")
    sw_slot = Slot.query.filter_by(month_id=month.id, date=d, hour=13, track="SW").one()
    # Give them availability on it, so only the lane check can refuse.
    db.session.add(Availability(student_id=ow.id, slot_id=sw_slot.id))
    schedule = Schedule(month_id=month.id, status="draft")
    db.session.add(schedule)
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        sess["_fresh"] = True
    from app.models import User
    admin = User(username="boss", role="overseer")
    admin.set_password("x")
    db.session.add(admin)
    db.session.commit()

    res = client.post(f"/api/schedule/months/{month.id}/assignments",
                      json={"slot_id": sw_slot.id, "student_id": ow.id})
    assert res.status_code == 400
    assert res.get_json()["error"] == "wrong_track"
    assert Assignment.query.count() == 0
