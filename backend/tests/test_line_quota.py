"""The Advanced page's LINE quota panel reads two of LINE's endpoints and,
when a group is configured, its member count. LINE itself is mocked."""
from unittest import mock


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.ok = body, status, status < 400
        self.headers, self.text = {}, str(body)

    def json(self):
        return self._body


def _fake_line(responses):
    def get(url, **_):
        for path, resp in responses.items():
            if url.endswith(path):
                return resp
        raise AssertionError("unexpected LINE call: " + url)
    return get


def test_no_token_reports_unconfigured(app):
    from app.notifications.backends import line_quota
    app.config["LINE_TOKEN"] = None
    assert line_quota() == {"configured": False}


def test_limited_quota_with_group(app):
    from app.notifications.backends import line_quota
    app.config.update(LINE_TOKEN="t", LINE_GROUP_ID="Cabc")
    fake = _fake_line({
        "/message/quota": _Resp({"type": "limited", "value": 200}),
        "/message/quota/consumption": _Resp({"totalUsage": 57}),
        "/group/Cabc/members/count": _Resp({"count": 11}),
    })
    with mock.patch("app.notifications.backends.requests.get", side_effect=fake):
        assert line_quota() == {"configured": True, "limit": 200, "used": 57, "group_members": 11}


def test_uncapped_quota_and_line_error(app):
    from app.notifications.backends import line_quota
    app.config.update(LINE_TOKEN="t", LINE_GROUP_ID=None)
    fake = _fake_line({
        "/message/quota": _Resp({"type": "none"}),
        "/message/quota/consumption": _Resp({"totalUsage": 3}),
    })
    with mock.patch("app.notifications.backends.requests.get", side_effect=fake):
        assert line_quota() == {"configured": True, "limit": None, "used": 3, "group_members": None}

    fake = _fake_line({
        "/message/quota": _Resp({"message": "Authentication failed"}, 401),
        "/message/quota/consumption": _Resp({"totalUsage": 3}),
    })
    with mock.patch("app.notifications.backends.requests.get", side_effect=fake):
        assert line_quota() == {"configured": True, "error": {"message": "Authentication failed"}}
