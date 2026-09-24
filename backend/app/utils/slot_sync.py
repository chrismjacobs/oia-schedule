"""Keep a month's Slot rows in step with its plan — without wiping anything
that doesn't need to go.

The plan is: every weekday of the month, minus closed dates, minus the hours
the month's regular schedule marks unavailable — in each worker lane. A Slot
is what students tick availability against and what the solver assigns, so
whenever the plan changes after slots exist, the slots have to follow —
otherwise students go on picking hours the office won't staff (and the solver
fills them), or can never pick hours that were opened up.

This replaced "Regenerate", which rebuilt the whole month and deleted every
student's selections to fix a single hour. Sync touches only the hours that
changed: an hour that joins the plan gets a slot; an hour that leaves it loses
its slot and just the selections on that one hour.

A slot is never removed from under a live roster: anything a committed
schedule, a leave request, an open-shift offer, or a recorded session hangs
off it is kept and reported instead, for the overseer to deal with. And a sync
that would take selections off more than a handful of students refuses
outright rather than doing it quietly — see MAX_LOST_DEFAULT.
"""
from collections import defaultdict

from app.extensions import db
from app.models import (
    Slot, ClosedDate, RegularSlot, Availability, Assignment, Schedule,
    ReopenedSlot, LeaveRequest, SessionHour, SLOT_HOURS,
)
from app.utils.periods import weekdays_in_month
from app.utils.tracks import TRACK_DEFAULT_ON

# How many students may lose selections in one sync before it refuses.
#
# Dropping an hour or two off one student is routine — the overseer closed a
# date, or moved an hour out of the plan. A sync that empties half the roster
# is not routine, it's a bug in the plan, and by the time the "lost picks"
# banner says so the rows are already gone. The caller passes max_lost=None
# once the overseer has seen what it wants to do and said yes.
MAX_LOST_DEFAULT = 2


class AvailabilityLossRefused(Exception):
    """Raised instead of dropping more students' selections than allowed.

    Carries the same shape the sync summary uses, so the route can show the
    overseer exactly what it declined to do and offer to confirm it.
    """

    def __init__(self, lost_picks, slots):
        super().__init__(f"would drop selections for {len(lost_picks)} student(s)")
        self.lost_picks = lost_picks
        self.slots = slots


def planned_hours(month):
    """The (date, hour, track) triples this month should have a Slot for.

    The two lanes have opposite defaults, on purpose. OW keeps the original
    rule — every weekday hour is staffed unless the regular grid marks it
    unavailable — so this returns exactly the set it always did for that lane.
    SW is opt-in: a slot exists only where a regular_slot row says so, because
    "no row means staff it" applied to a second lane would double every hour
    in the month.
    """
    year, mon = (int(x) for x in month.year_month.split("-"))
    closed = {c.date for c in ClosedDate.query.filter(
        db.extract("year", ClosedDate.date) == year,
        db.extract("month", ClosedDate.date) == mon,
    ).all()}
    regular = {(r.date, r.hour, r.track): r.state
               for r in RegularSlot.query.filter_by(month_id=month.id).all()}

    planned = set()
    for d in weekdays_in_month(month.year_month):
        if d in closed:
            continue
        for hour in SLOT_HOURS:
            for track, default_on in TRACK_DEFAULT_ON.items():
                state = regular.get((d, hour, track))
                if state == "unavailable":
                    continue
                if state is None and not default_on:
                    continue
                planned.add((d, hour, track))
    return planned


def sync_month_slots(month, max_lost=MAX_LOST_DEFAULT):
    """Add missing slots and remove unplanned ones. Stages changes on the
    session; the caller commits. Returns a summary for the overseer:

      created, removed    — counts
      lost_picks          — [{student, hours}] whose selections were dropped
      kept                — [{date, hour, track, student, reason}] slots left
                            in place because something live depends on them

    Raises AvailabilityLossRefused when the removals would take selections off
    more than `max_lost` students. Pass max_lost=None to go ahead anyway.
    """
    planned = planned_hours(month)
    existing = Slot.query.filter_by(month_id=month.id).all()
    have = {(s.date, s.hour, s.track) for s in existing}

    created = 0
    for d, hour, track in sorted(planned - have):
        db.session.add(Slot(month_id=month.id, date=d, hour=hour, track=track,
                            period="morning" if hour < 12 else "afternoon", state="open"))
        created += 1

    unplanned = [s for s in existing if (s.date, s.hour, s.track) not in planned]
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

        # Work out what would go before anything goes, so the refusal below
        # can fire with nothing staged and nothing to unpick.
        removable, picks_by_slot = [], {}
        for slot in unplanned:
            a = committed_assignment.get(slot.id)
            reason = "scheduled on the committed roster" if a else next(
                (why for why, slot_ids in busy.items() if slot.id in slot_ids), None)
            if reason:
                kept.append({
                    "date": slot.date.isoformat(), "hour": slot.hour, "track": slot.track,
                    "reason": reason,
                    "student": a.student.short_name if a and a.student else None,
                })
                continue
            removable.append(slot)
            picks_by_slot[slot.id] = Availability.query.filter_by(slot_id=slot.id).all()
            for av in picks_by_slot[slot.id]:
                lost[av.student.short_name if av.student else f"#{av.student_id}"] += 1

        lost_picks = [{"student": name, "hours": n} for name, n in sorted(lost.items())]
        if max_lost is not None and len(lost_picks) > max_lost:
            raise AvailabilityLossRefused(lost_picks, [
                {"date": s.date.isoformat(), "hour": s.hour, "track": s.track} for s in removable
            ])

        for slot in removable:
            for av in picks_by_slot[slot.id]:
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
        "kept": sorted(kept, key=lambda k: (k["date"], k["hour"], k["track"])),
    }


def month_has_slots(month):
    return Slot.query.filter_by(month_id=month.id).first() is not None
