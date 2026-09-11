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


def _merged(key, defaults):
    """Saved overrides on top of the config defaults — so a weight added
    later still has a value, and one retired later is dropped rather than
    lingering on the settings page."""
    saved = get_setting(key) or {}
    return {k: saved.get(k, v) for k, v in defaults.items()}


def get_solver_weights():
    return _merged("solver_weights", current_app.config["SOLVER_WEIGHTS"])


def get_session_rules():
    return _merged("solver_session_rules", current_app.config["SOLVER_SESSION_RULES"])


def get_floor_hours():
    return get_setting("solver_floor_hours", current_app.config["SOLVER_FLOOR_HOURS"])


def get_timecard_cadence():
    return get_setting("timecard_cadence", current_app.config["TIMECARD_CADENCE_DEFAULT"])


def get_attendance_notify_enabled():
    """Sign-in/out LINE notifications — on by default, but every sign-in and
    sign-out fires one, which can add up fast on a busy day. Toggle from
    Advanced if it turns out to be too much (CLAUDE.md #12)."""
    return get_setting("notify_attendance_events", True)
