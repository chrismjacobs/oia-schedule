from flask import current_app
from app.extensions import db
from app.models import AppSetting


def get_setting(key, default=None):
    row = AppSetting.query.get(key)
    if row is None:
        return default
    return row.value


def set_setting(key, value):
    row = AppSetting.query.get(key)
    if row is None:
        row = AppSetting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()
    return row.value


def get_solver_weights():
    return get_setting("solver_weights", dict(current_app.config["SOLVER_WEIGHTS"]))


def get_floor_hours():
    return get_setting("solver_floor_hours", current_app.config["SOLVER_FLOOR_HOURS"])


def get_timecard_cadence():
    return get_setting("timecard_cadence", current_app.config["TIMECARD_CADENCE_DEFAULT"])
