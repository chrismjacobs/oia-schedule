from datetime import datetime

from flask import jsonify, request
from flask_login import current_user

from app.availability import bp
from app.extensions import db
from app.models import Month, Slot, Availability, SelectionWindow
from app.utils.decorators import login_required_api


def _window_is_open(month):
    if month.state != "selection_open":
        return False
    sw = SelectionWindow.query.filter_by(month_id=month.id).first()
    if not sw:
        return True  # no configured window: state alone gates it
    now = datetime.utcnow()
    return sw.opens_at <= now <= sw.closes_at


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
    month = Month.query.get_or_404(month_id)
    slots = Slot.query.filter_by(month_id=month.id).order_by(Slot.date, Slot.hour).all()
    mine = set()
    if current_user.student_id:
        mine = {
            a.slot_id for a in Availability.query
            .join(Slot, Availability.slot_id == Slot.id)
            .filter(Availability.student_id == current_user.student_id, Slot.month_id == month.id)
        }
    return jsonify({
        "month": month.to_dict(),
        "window_open": _window_is_open(month),
        "slots": [{**s.to_dict(), "selected": s.id in mine} for s in slots],
    })


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

    valid_slot_ids = {s.id for s in Slot.query.filter_by(month_id=month.id).all()}
    if not slot_ids.issubset(valid_slot_ids):
        return jsonify({"error": "invalid_slot_ids"}), 400

    Availability.query.filter(
        Availability.student_id == current_user.student_id,
        Availability.slot_id.in_(valid_slot_ids),
    ).delete(synchronize_session=False)

    for sid in slot_ids:
        db.session.add(Availability(student_id=current_user.student_id, slot_id=sid))
    db.session.commit()
    return jsonify({"ok": True, "count": len(slot_ids)})
