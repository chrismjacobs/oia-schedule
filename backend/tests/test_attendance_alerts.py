"""Attendance alerts: the forgot-to-sign-out reminder fires an hour after the
run's scheduled end, and both it and the no-show reminder overflow to the
admin bot once the group bot's monthly quota is spent. LINE is mocked."""
from datetime import date, datetime
from unittest import mock

import requests

from app.extensions import db
from app.models import (
    Semester, Student, Month, Slot, Schedule, Assignment, AttendanceSession, NotificationLog,
)

DAY = date(2026, 10, 7)


def _morning_session(signed_in_hour=8):
    db.session.add(Semester(id=1, name="Test", starts_on=date(2026, 9, 1),
                            ends_on=date(2027, 1, 31), is_active=True))
    month = Month(year_month="2026-10", state="running")
    db.session.add(month)
    db.session.flush()
    student = Student(semester_id=1, chinese_name="學生", english_name="Stu",
                      student_id="T000001", colour="#0072B2", shape="circle")
    schedule = Schedule(month_id=month.id, status="committed")
    db.session.add_all([student, schedule])
    db.session.flush()
    for hour in (8, 9, 10, 11):
        slot = Slot(month_id=month.id, date=DAY, hour=hour, period="morning", state="assigned")
        db.session.add(slot)
        db.session.flush()
        db.session.add(Assignment(schedule_id=schedule.id, slot_id=slot.id,
                                  student_id=student.id, source="solver"))
    sess = AttendanceSession(student_id=student.id, date=DAY,
                             signed_in_at=datetime(2026, 10, 7, signed_in_hour, 0))
    db.session.add(sess)
    db.session.commit()
    return sess


def test_forgot_sign_out_waits_an_hour_past_the_runs_end(app):
    from app.notifications.tick import _flag_forgotten_signouts
    sess = _morning_session()

    # The run ends at 12:00; 12:59 is still inside the hour's grace.
    assert _flag_forgotten_signouts(datetime(2026, 10, 7, 12, 59)) == {"forgot_signout_flagged": 0}
    assert not sess.flagged

    assert _flag_forgotten_signouts(datetime(2026, 10, 7, 13, 0)) == {"forgot_signout_flagged": 1}
    assert sess.flag_reason == "forgot_sign_out"
    row = NotificationLog.query.filter_by(type="forgot_sign_out").one()
    assert row.message == ("[OIA] Reminder: Stu signed in for 2026-10-07 8:00-12:00 "
                           "and hasn't signed out yet.")

    # Flagged once, reminded once.
    assert _flag_forgotten_signouts(datetime(2026, 10, 7, 14, 0)) == {"forgot_signout_flagged": 0}


def test_old_open_sessions_are_flagged_without_a_reminder(app):
    from app.notifications.tick import _flag_forgotten_signouts
    sess = _morning_session()
    assert _flag_forgotten_signouts(datetime(2026, 10, 9, 9, 0)) == {"forgot_signout_flagged": 1}
    assert sess.flag_reason == "forgot_sign_out"
    assert NotificationLog.query.filter_by(type="forgot_sign_out").count() == 0


def _quota_spent():
    resp = requests.Response()
    resp.status_code = 429
    return requests.HTTPError("429 monthly limit", response=resp)


def _live_line(app, admin_ready=True):
    app.config.update(ALLOW_LIVE_NOTIFICATIONS=True, NOTIFICATION_BACKEND="line",
                      LINE_TOKEN="t1", LINE_GROUP_ID="Cgroup",
                      LINE2_TOKEN="t2", LINE_ADMIN_USER_ID="Uadmin" if admin_ready else None)


def _send_via_backend(sent):
    """Patch LineBackend.send: the main bot is out of quota, the admin's isn't."""
    def send(self, message, to=None):
        if self.account == "main":
            raise _quota_spent()
        sent.append((self.account, message))
    return mock.patch("app.notifications.backends.LineBackend.send", send)


def test_no_show_overflows_to_admin_when_group_quota_is_spent(app):
    from app.notifications.service import notify_once
    _live_line(app)
    sent = []
    with _send_via_backend(sent):
        assert notify_once("no_show", "group", "slot", 1, "[OIA] Reminder: Stu ...")
    assert sent == [("admin", "(Group bot is out of messages this month — sent here instead.)\n"
                              "[OIA] Reminder: Stu ...")]
    assert NotificationLog.query.one().sent_flag


def test_other_group_messages_do_not_overflow(app):
    from app.notifications.service import notify_once
    _live_line(app)
    sent = []
    with _send_via_backend(sent):
        assert not notify_once("slot_open", "group", "reopened_slot", 1, "[OIA] Slot open")
    assert sent == []
    assert not NotificationLog.query.one().sent_flag


def test_no_overflow_until_the_admin_bot_is_configured(app):
    from app.notifications.service import notify_once
    _live_line(app, admin_ready=False)
    sent = []
    with _send_via_backend(sent):
        assert not notify_once("no_show", "group", "slot", 1, "[OIA] Reminder")
    assert sent == []
