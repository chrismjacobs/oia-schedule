from flask import jsonify, request
from flask_login import current_user

from app.availability import bp
from app.extensions import db
from app.models import (
    Month, Slot, Availability, AvailabilityOptOut, SelectionWindow, ClosedDate, RegularSlot, SLOT_HOURS,
)
from app.utils.decorators import login_required_api
from app.utils.periods import weekdays_in_month
from app.utils.tracks import track_for, DEFAULT_TRACK
from app.utils.tz import local_now


def _window_is_open(month):
    """One gate, not two: the month's state alone says whether students can
    select. The SelectionWindow only schedules when /tick moves the state.

    It used to also require the clock to be inside the window, which meant
    "is selection open?" had two answers that could disagree — a month could
    sit in selection_open with students still locked out, with nothing on
    screen explaining why. If the overseer sets the state by hand, that is
    what holds."""
    return month.state == "selection_open"


@bp.get("/current")
@login_required_api
def current_selection_month():
    """Students don't have list-months access — this hands them whichever
    month is presently open for selection, if any."""
    month = Month.query.filter_by(state="selection_open").order_by(Month.year_month.desc()).first()
    if not month:
        return jsonify(None)
    return jsonify(month.to_dict())


@bp.get("/<int:month_id>")
@login_required_api
def get_availability(month_id):
    """Full calendar shape for the month — every weekday, every hour — not
    just the hours that happen to have a Slot. Closed dates and regular-
    template 'unavailable' hours come back with slot_id=null so the grid can
    still show them (greyed out) instead of silently omitting the column."""
    month = Month.query.get_or_404(month_id)
    # One lane's worth of grid. A student works either the paid or the unpaid
    # lane, never both, so they only ever see and tick their own lane's slots.
    # That also keeps this grid one cell per (date, hour) — the two lanes are
    # merged only on the overseer's views, never here.
    my_track = track_for(current_user.student) if current_user.student_id else DEFAULT_TRACK
    slot_by_date_hour = {
        (s.date, s.hour): s for s in Slot.query.filter_by(
            month_id=month.id, track=my_track).all()
    }

    year, mon = (int(x) for x in month.year_month.split("-"))
    closed_by_date = {
        c.date: c.reason for c in ClosedDate.query.filter(
            db.extract("year", ClosedDate.date) == year,
            db.extract("month", ClosedDate.date) == mon,
        ).all()
    }

    mine = set()
    has_existing = False
    regular_mine = set()
    if current_user.student_id:
        mine = {
            a.slot_id for a in Availability.query
            .join(Slot, Availability.slot_id == Slot.id)
            .filter(Availability.student_id == current_user.student_id, Slot.month_id == month.id)
        }
        has_existing = len(mine) > 0
        regular_mine = {
            (r.date, r.hour) for r in RegularSlot.query.filter_by(
                month_id=month.id, state="assigned", student_id=current_user.student_id,
                track=my_track
            ).all()
        }

    # Regular coverage for EVERY student (not just the viewer) — this is what
    # lets the grid flag "that regular slot's usual student already saved
    # availability this month without it", i.e. confirmed uncovered, not just
    # not-yet-decided. A student who hasn't saved anything yet this month
    # isn't "declined" — they just haven't gotten to it.
    regular_rows = RegularSlot.query.filter_by(
        month_id=month.id, state="assigned", track=my_track).all()
    regular_by_date_hour = {(r.date, r.hour): r.student_id for r in regular_rows if r.student_id}
    regular_student_ids = set(regular_by_date_hour.values())
    saved_student_ids, avail_pairs = set(), set()
    if regular_student_ids:
        for a in (Availability.query.join(Slot, Availability.slot_id == Slot.id)
                  .filter(Slot.month_id == month.id, Availability.student_id.in_(regular_student_ids)).all()):
            saved_student_ids.add(a.student_id)
            avail_pairs.add((a.student_id, a.slot_id))
        # "No hours this month" is an answer too: every regular hour of theirs
        # is confirmed free for someone else.
        saved_student_ids |= {o.student_id for o in AvailabilityOptOut.query.filter(
            AvailabilityOptOut.month_id == month.id,
            AvailabilityOptOut.student_id.in_(regular_student_ids)).all()}

    no_hours = bool(current_user.student_id and AvailabilityOptOut.query.filter_by(
        month_id=month.id, student_id=current_user.student_id).first())

    dates, cells = [], []
    for d in weekdays_in_month(month.year_month):
        dates.append({"date": d.isoformat(), "closed": d in closed_by_date, "reason": closed_by_date.get(d)})
        for hour in SLOT_HOURS:
            slot = slot_by_date_hour.get((d, hour))
            is_regular = (d, hour) in regular_mine
            # First time visiting this month (nothing saved yet): suggest the
            # student's own regular hours as pre-checked. Once they've saved
            # anything — or said "no hours this month" — honour exactly that,
            # never re-inject a default over a deliberate uncheck.
            selected = slot is not None and (slot.id in mine or (not has_existing and not no_hours and is_regular))

            reg_student_id = regular_by_date_hour.get((d, hour))
            regular_declined = bool(
                slot and reg_student_id and reg_student_id in saved_student_ids
                and (reg_student_id, slot.id) not in avail_pairs
            )
            cells.append({
                "date": d.isoformat(), "hour": hour,
                "slot_id": slot.id if slot else None,
                "selected": selected,
                "is_regular": is_regular,
                "regular_student_id": reg_student_id,
                "regular_declined": regular_declined,
            })

    return jsonify({
        "month": month.to_dict(),
        "window_open": _window_is_open(month),
        "no_hours": no_hours,
        "dates": dates,
        "cells": cells,
    })


@bp.get("/<int:month_id>/previous-nonregular")
@login_required_api
def previous_nonregular(month_id):
    """Weekday/hour pairs from the student's own most recent prior saved
    month, minus whatever's their regular pattern this month — the raw
    material for the availability page's "Add last month's hours" button.
    Deliberately generalised to a weekly pattern rather than literal dates
    (same as the regular-hours button), since a different-length month has
    no exact date-for-date equivalent."""
    month = Month.query.get_or_404(month_id)
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403

    prev_month = (Month.query.filter(Month.year_month < month.year_month)
                  .order_by(Month.year_month.desc()).first())
    if not prev_month:
        return jsonify({"weekday_hours": []})

    my_regular_this_month = {
        (r.date.weekday(), r.hour) for r in RegularSlot.query.filter_by(
            month_id=month.id, state="assigned", student_id=current_user.student_id
        ).all()
    }

    prev_rows = (Availability.query.join(Slot, Availability.slot_id == Slot.id)
                 .filter(Availability.student_id == current_user.student_id, Slot.month_id == prev_month.id).all())
    pairs = {(a.slot.date.weekday(), a.slot.hour) for a in prev_rows}
    pairs -= my_regular_this_month

    return jsonify({"weekday_hours": [{"weekday": wd, "hour": h} for wd, h in sorted(pairs)]})


@bp.put("/<int:month_id>")
@login_required_api
def set_availability(month_id):
    month = Month.query.get_or_404(month_id)
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    if not _window_is_open(month):
        return jsonify({"error": "selection_closed"}), 409

    data = request.get_json(force=True) or {}
    slot_ids = set(data.get("slot_ids") or [])

    # Their own lane only — a student cannot offer hours in the lane they
    # don't work, whatever the request body says.
    my_track = track_for(current_user.student)
    valid_slot_ids = {s.id for s in Slot.query.filter_by(
        month_id=month.id, track=my_track).all()}
    if not slot_ids.issubset(valid_slot_ids):
        return jsonify({"error": "invalid_slot_ids"}), 400

    # Cleared across both lanes, not just theirs: if a student is moved
    # between lanes mid-month, the rows they left behind in the old one would
    # otherwise linger and still count as offered hours.
    month_slot_ids = {s.id for s in Slot.query.filter_by(month_id=month.id).all()}
    Availability.query.filter(
        Availability.student_id == current_user.student_id,
        Availability.slot_id.in_(month_slot_ids),
    ).delete(synchronize_session=False)

    for sid in slot_ids:
        db.session.add(Availability(student_id=current_user.student_id, slot_id=sid))
    if slot_ids:
        # Picking hours replaces an earlier "no hours this month".
        AvailabilityOptOut.query.filter_by(
            student_id=current_user.student_id, month_id=month.id).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"ok": True, "count": len(slot_ids)})


@bp.post("/<int:month_id>/no-hours")
@login_required_api
def set_no_hours(month_id):
    """The student's answer "I'm not available this month": clears any hours
    they'd picked and records the answer (see AvailabilityOptOut). Undone by
    simply picking hours and saving."""
    month = Month.query.get_or_404(month_id)
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    if not _window_is_open(month):
        return jsonify({"error": "selection_closed"}), 409

    month_slot_ids = [s.id for s in Slot.query.filter_by(month_id=month.id).all()]
    if month_slot_ids:
        Availability.query.filter(
            Availability.student_id == current_user.student_id,
            Availability.slot_id.in_(month_slot_ids),
        ).delete(synchronize_session=False)
    if not AvailabilityOptOut.query.filter_by(student_id=current_user.student_id, month_id=month.id).first():
        db.session.add(AvailabilityOptOut(student_id=current_user.student_id, month_id=month.id))
    db.session.commit()
    return jsonify({"ok": True, "no_hours": True})
