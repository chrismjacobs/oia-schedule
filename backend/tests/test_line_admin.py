"""The second (admin) LINE account: leave requests route to it once it's
configured, and the shared webhook tells the two accounts apart by
signature. LINE itself is mocked."""
import base64
import hashlib
import hmac
import json
from datetime import date
from types import SimpleNamespace
from unittest import mock


def _leave(id_, hour):
    student = SimpleNamespace(display_name=lambda: "王小明 Ming")
    return SimpleNamespace(id=id_, reason="Exam", student=student,
                           slot=SimpleNamespace(date=date(2026, 10, 7), hour=hour))


def _log_row():
    from app.models import NotificationLog
    return NotificationLog.query.filter_by(type="leave_requested").one()


def test_leave_goes_to_group_generically_until_admin_is_set(app):
    from app.notifications.service import notify_leave_requested
    app.config.update(LINE2_TOKEN="t2", LINE_ADMIN_USER_ID=None)
    notify_leave_requested([_leave(1, 9), _leave(2, 10)])
    row = _log_row()
    assert row.target == "group"
    assert "Ming" not in row.message and "Exam" not in row.message


def test_leave_goes_to_admin_with_name_and_reason(app):
    from app.notifications.service import notify_leave_requested
    app.config.update(LINE2_TOKEN="t2", LINE_ADMIN_USER_ID="Uadmin")
    notify_leave_requested([_leave(1, 9), _leave(2, 10)])
    row = _log_row()
    assert row.target == "admin"
    assert "王小明 Ming" in row.message and "Reason: Exam" in row.message
    assert "9:00-11:00 (2 hours)" in row.message


def test_admin_target_never_falls_back_to_the_group(app):
    from app.notifications.backends import get_backend
    app.config.update(ALLOW_LIVE_NOTIFICATIONS=True, NOTIFICATION_BACKEND="line",
                      LINE_TOKEN="t1", LINE_GROUP_ID="Cgroup", LINE2_TOKEN=None)
    backend = get_backend("admin")
    assert backend.name == "line-admin"
    with mock.patch("app.notifications.backends.requests.post") as post:
        try:
            backend.send("private")
        except RuntimeError:
            pass
        post.assert_not_called()


def _signed_post(client, secret, text):
    body = json.dumps({"events": [{
        "type": "message", "replyToken": "rt",
        "message": {"type": "text", "text": text},
        "source": {"type": "user", "userId": "Uadmin"},
    }]}).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return client.post("/line/callback", data=body, headers={
        "X-Line-Signature": sig, "Content-Type": "application/json"})


def test_webhook_replies_with_the_account_that_signed(app):
    app.config.update(LINE_SECRET="s1", LINE_TOKEN="t1", LINE2_SECRET="s2", LINE2_TOKEN="t2")
    client = app.test_client()
    with mock.patch("app.notifications.backends.requests.post") as post:
        assert _signed_post(client, "s2", "/id").status_code == 200
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer t2"
        assert "Uadmin" in post.call_args.kwargs["json"]["messages"][0]["text"]

        post.reset_mock()
        assert _signed_post(client, "s1", "hello").status_code == 200
        post.assert_not_called()

        assert _signed_post(client, "wrong", "/id").status_code == 403
