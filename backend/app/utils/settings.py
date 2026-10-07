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


def get_auto_advertise_enabled():
    """Whether /tick offers uncovered hours around on its own.

    Off by default, which is a deliberate reversal. Auto-advertising every
    uncovered hour inside the lookahead window announces each one to the
    group, and a group push costs one message per member — so the quietest
    month of ordinary gaps could spend the whole LINE free-tier allowance on
    offers nobody asked for. Reopening a shift is rare in practice and the
    overseer already has Advertise for when they mean it.

    Turning this on restores the old behaviour; uncovered hours are reported
    on the dashboard either way, so nothing is hidden while it is off."""
    return get_setting("auto_advertise_enabled", False)


# Where each attendance message goes, set per message from Advanced:
# "group" (the student group bot), "admin" (the admin bot, to the overseer
# alone) or "off". Sign-in/out are off by default — two group pushes a shift
# is more than the free 200/month quota can carry.
ATTENDANCE_MESSAGES = ("signed_in", "signed_out", "no_show", "forgot_sign_out")
ROUTE_CHOICES = ("group", "admin", "off")


def get_notification_routes():
    # notify_attendance_events was the single on/off tick box for sign-in/out
    # that this replaced; a saved value still sets their starting point.
    legacy = "group" if get_setting("notify_attendance_events", False) else "off"
    defaults = {"signed_in": legacy, "signed_out": legacy,
                "no_show": "group", "forgot_sign_out": "group"}
    saved = get_setting("notification_routes") or {}
    return {k: saved[k] if saved.get(k) in ROUTE_CHOICES else v for k, v in defaults.items()}


# Attendance timings in minutes, editable from Advanced. Config only supplies
# the defaults.
TIMING_KEYS = {
    "no_show_grace_minutes": "NO_SHOW_GRACE_MINUTES",
    "forgot_signout_minutes_after_end": "FORGOT_SIGNOUT_MINUTES_AFTER_END",
}


def get_timing(key):
    return int(get_setting(key, current_app.config[TIMING_KEYS[key]]))
