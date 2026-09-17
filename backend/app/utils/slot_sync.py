"""Keep a month's Slot rows in step with its plan — without wiping anything
that doesn't need to go.

The plan is: every weekday of the month, minus closed dates, minus the hours
the month's regular schedule marks unavailable. A Slot is what students tick
availability against and what the solver assigns, so whenever the plan
changes after slots exist, the slots have to follow — otherwise students go
on picking hours the office won't staff (and the solver fills them), or can
never pick hours that were opened up.

This replaced "Regenerate", which rebuilt the whole month and deleted every
student's selections to fix a single hour. Sync touches only the hours that
changed: an hour that joins the plan gets a slot; an hour that leaves it loses
its slot and just the selections on that one hour.

A slot is never removed from under a live roster: anything a committed
schedule, a leave request, an open-shift offer, or a recorded session hangs
off it is kept and reported instead, for the overseer to deal with.
"""
from collections import defaultdict

from app.extensions import db
from app.models import (
    Slot, ClosedDate, RegularSlot, Availability, Assignment, Schedule,
    ReopenedSlot, LeaveRequest, SessionHour, SLOT_HOURS,
)
from app.utils.periods import weekdays_in_month


def planned_hours(month):
    """The (date, hour) pairs this month should have a Slot for."""
    year, mon = (int(x) for x in month.year_month.split("-"))
    closed = {c.date for c in ClosedDate.query.filter(
        db.extract("year", ClosedDate.date) == year,
        db.extract("month", ClosedDate.date) == mon,
    ).all()}
    unavailable = {(r.date, r.hour) for r in RegularSlot.query.filter_by(
        month_id=month.id, state="unavailable").all()}
    return {
        (d, hour)
        for d in weekdays_in_month(month.year_month) if d not in closed
        for hour in SLOT_HOURS if (d, hour) not in unavailable
    }


def sync_month_slots(month):
    """Add missing slots and remove unplanned ones. Stages changes on the
    session; the caller commits. Returns a summary for the overseer:

      created, removed    — counts
      lost_picks          — [{student, hours}] whose selections were dropped
      kept                — [{date, hour, student, reason}] slots left in
                            place because something live depends on them
    """
    planned = planned_hours(month)
    existing = Slot.query.filter_by(month_id=month.id).all()
    have = {(s.date, s.hour) for s in existing}

    created = 0
    for d, hour in sorted(planned - have):
        db.session.add(Slot(month_id=month.id, date=d, hour=hour,
                            period="morning" if hour < 12 else "afternoon", state="open"))
        created += 1

    unplanned = [s for s in existing if (s.date, s.hour) not in planned]
    removed = 0
    lost = defaultdict(int)   # student -> hours of selection dropped
    kept = []
    if unplanned:
        ids = [s.id for s in unplanned]
        committed_ids = {s.id for s in Schedule.query.filter_by(
            month_id=month.id, status="committed").all()}
        committed_assignment = {
            a.slot_id: a for a in Assignment.query.filter(
                Assignment.slot_id.in_(ids), Assignment.schedule_id.in_(committed_ids)).all()
        } if committed_ids else {}
        busy = {
            # Withdrawn requests excluded: a mis-click the student took back
            # shouldn't keep pinning an hour that nothing else needs.
            "leave requested": {r.slot_id for r in LeaveRequest.query.filter(
                LeaveRequest.slot_id.in_(ids), LeaveRequest.status != "withdrawn")},
            "offered on Open Shifts": {r.slot_id for r in ReopenedSlot.query.filter(ReopenedSlot.slot_id.in_(ids))},
            "hours already recorded": {r.slot_id for r in SessionHour.query.filter(SessionHour.slot_id.in_(ids))},
        }

        for slot in unplanned:
            a = committed_assignment.get(slot.id)
            reason = "scheduled on the committed roster" if a else next(
                (why for why, slot_ids in busy.items() if slot.id in slot_ids), None)
            if reason:
                kept.append({
                    "date": slot.date.isoformat(), "hour": slot.hour, "reason": reason,
                    "student": a.student.short_name if a and a.student else None,
                })
                continue
            for av in Availability.query.filter_by(slot_id=slot.id).all():
                lost[av.student.short_name if av.student else f"#{av.student_id}"] += 1
                db.session.delete(av)
            # Only draft assignments can reach here (committed ones are kept
            # above) — a draft is a proposal, losing one cell is fine.
            for draft in Assignment.query.filter_by(slot_id=slot.id).all():
                db.session.delete(draft)
            db.session.delete(slot)
            removed += 1

    return {
        "created": created,
        "removed": removed,
        "lost_picks": [{"student": name, "hours": n} for name, n in sorted(lost.items())],
        "kept": sorted(kept, key=lambda k: (k["date"], k["hour"])),
    }


def month_has_slots(month):
    return Slot.query.filter_by(month_id=month.id).first() is not None
