"""Pluggable notification backends (CLAUDE.md #11). Email works for v1;
LINE Messaging API switches on once the Official Account is set up. Never
LINE Notify — discontinued March 2025."""
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import requests
from flask import current_app


class NotificationBackend:
    name = "?"

    def send(self, message: str):
        raise NotImplementedError


class EmailBackend(NotificationBackend):
    name = "email"

    def send(self, message: str):
        cfg = current_app.config
        to_addr = cfg.get("NOTIFICATION_TO_EMAIL")
        if not to_addr or not cfg.get("SMTP_HOST"):
            # Raise, like LINE does. This used to log at info and return, so
            # the caller marked the notification sent — a slot got advertised,
            # the log said "notify OK", and nothing reached anyone. That is
            # exactly what happens when NOTIFICATION_BACKEND is left at its
            # email default on a deployment that only has LINE set up.
            current_app.logger.error(
                "EMAIL send SKIPPED — %s not configured. Message dropped: %r "
                "(automatic notifications use NOTIFICATION_BACKEND=%s; set it to "
                "'line' to send via LINE)",
                "NOTIFICATION_TO_EMAIL" if not to_addr else "SMTP_HOST", message,
                cfg.get("NOTIFICATION_BACKEND"))
            raise RuntimeError("email_not_configured")
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = "OIA Duty Roster"
        msg["From"] = cfg["NOTIFICATION_FROM_EMAIL"]
        msg["To"] = to_addr
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=10) as server:
            server.starttls()
            if cfg.get("SMTP_USER"):
                server.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            server.sendmail(cfg["NOTIFICATION_FROM_EMAIL"], [to_addr], msg.as_string())


class LineBackend(NotificationBackend):
    """LINE Messaging API push to the student group (not LINE Notify)."""
    name = "line"
    PUSH_URL = "https://api.line.me/v2/bot/message/push"
    REPLY_URL = "https://api.line.me/v2/bot/message/reply"

    def send(self, message: str, to: str = None):
        cfg = current_app.config
        token = cfg.get("LINE_TOKEN")
        target = to or cfg.get("LINE_GROUP_ID")
        if not token or not target:
            # Loud, not info: this silently drops every notification, and the
            # caller marks it sent, so it looks like success forever after.
            current_app.logger.error(
                "LINE push SKIPPED — %s not configured. Message dropped: %r",
                "LINE_TOKEN" if not token else "LINE_GROUP_ID", message)
            raise RuntimeError("line_not_configured")

        started = time.monotonic()
        resp = requests.post(
            self.PUSH_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": target, "messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        # LINE's own request id is the only handle their support can trace.
        request_id = resp.headers.get("x-line-request-id")
        if resp.status_code >= 400:
            current_app.logger.error(
                "LINE push FAILED %s in %sms | to=%s | request_id=%s | body=%s",
                resp.status_code, elapsed_ms, target, request_id, resp.text[:500])
        else:
            current_app.logger.info(
                "LINE push OK %s in %sms | to=%s | request_id=%s",
                resp.status_code, elapsed_ms, target, request_id)
        resp.raise_for_status()

    def reply(self, reply_token: str, message: str):
        """Reply API — free (no push quota used), only usable within the
        webhook request/response window via a reply_token."""
        token = current_app.config.get("LINE_TOKEN")
        if not token:
            return
        resp = requests.post(
            self.REPLY_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        resp.raise_for_status()


class DryRunBackend(NotificationBackend):
    """Logs instead of sending. Used for automatic notifications whenever the
    app is in debug, so a local run can't push to the live student group."""
    name = "dry-run"

    def send(self, message: str, to: str = None):
        current_app.logger.warning("[notify:DRY-RUN, not sent] %s", message)


def automatic_notifications_are_live(config) -> bool:
    """Whether automatic notifications really send.

    The hazard is running against a *throwaway database* while holding the
    production credentials from .env — a test script, a seeded demo month, a
    manual tick. That has happened: a demo seed's no-show sweep pushed a burst
    of messages naming long-deleted students to actual students.

    So the signal is the database, not the debug flag. Every scratch/test run
    uses SQLite; this deployment is Postgres. Keying on DEBUG was wrong — this
    project ships FLASK_DEBUG=1 in .env to relax the session cookie for local
    http, so it is set in production too, and using it silenced every real
    notification while test-send kept working.

    ALLOW_LIVE_NOTIFICATIONS=1 forces live sending — needed if this is ever
    deployed on SQLite for real (CLAUDE.md #3 keeps that option open).
    """
    if config.get("ALLOW_LIVE_NOTIFICATIONS"):
        return True
    return not str(config.get("SQLALCHEMY_DATABASE_URI", "")).startswith("sqlite")


def get_backend() -> NotificationBackend:
    """Backend for *automatic* notifications (/tick, commits, leave, no-shows).

    The overseer's Advanced > test send builds LineBackend/EmailBackend
    directly and is deliberately NOT routed through here, so checking the
    wiring by hand always really sends.
    """
    if not automatic_notifications_are_live(current_app.config):
        return DryRunBackend()
    backend = current_app.config.get("NOTIFICATION_BACKEND", "email")
    if backend == "line":
        return LineBackend()
    return EmailBackend()


def delivery_problem(config):
    """Why automatic notifications won't reach anyone, or None if they should.

    Test send picks its backend from a dropdown (default LINE); automatic
    notifications use NOTIFICATION_BACKEND. When those disagree, test sends
    arrive and real ones don't, which looks like a LINE fault but isn't."""
    if not automatic_notifications_are_live(config):
        return "DRY-RUN: database is SQLite, so nothing is sent (ALLOW_LIVE_NOTIFICATIONS=1 overrides)"
    backend = config.get("NOTIFICATION_BACKEND", "email")
    needed = ("LINE_TOKEN", "LINE_GROUP_ID") if backend == "line" else ("SMTP_HOST", "NOTIFICATION_TO_EMAIL")
    missing = [k for k in needed if not config.get(k)]
    if missing:
        return f"NOTIFICATION_BACKEND={backend} but {', '.join(missing)} not set"
    return None


LINE_API = "https://api.line.me/v2/bot"


def _line_get(label, path, token):
    try:
        resp = requests.get(LINE_API + path, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except requests.RequestException as e:
        current_app.logger.error("LINE diag | %s | request failed: %s", label, e)
        return {"label": label, "ok": False, "status": None, "error": str(e)}
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:300]
    request_id = resp.headers.get("x-line-request-id")
    log = current_app.logger.info if resp.ok else current_app.logger.error
    log("LINE diag | %s | %s | request_id=%s | %s", label, resp.status_code, request_id, body)
    return {"label": label, "ok": resp.ok, "status": resp.status_code,
            "request_id": request_id, ("data" if resp.ok else "error"): body}


def line_diagnostics():
    """Ask LINE's side what it knows, since its console has no per-message
    log for pushes. Read-only: sends nothing and uses no quota.

    The most telling check is quota consumption — it counts pushes LINE
    actually accepted this month, so if advertising a slot doesn't move it,
    the app never reached LINE at all."""
    cfg = current_app.config
    token = cfg.get("LINE_TOKEN")
    group = cfg.get("LINE_GROUP_ID")
    report = {
        "automatic_backend": cfg.get("NOTIFICATION_BACKEND"),
        "automatic_live": automatic_notifications_are_live(cfg),
        "automatic_problem": delivery_problem(cfg),
        "line_token_set": bool(token),
        "line_group_id": group,
        "checks": [],
    }
    if not token:
        return report

    checks = report["checks"]
    checks.append(_line_get("Token valid (bot info)", "/info", token))
    if group:
        checks.append(_line_get("Bot is a member of LINE_GROUP_ID", f"/group/{group}/summary", token))
        checks.append(_line_get("Group member count", f"/group/{group}/members/count", token))
    checks.append(_line_get("Monthly message quota", "/message/quota", token))
    checks.append(_line_get("Messages used this month", "/message/quota/consumption", token))
    # LINE buckets delivery stats by Japan date and fills them in with a lag,
    # so today usually reads "unready"; yesterday is the reliable one.
    today = datetime.now(timezone(timedelta(hours=9))).date()
    for d in (today, today - timedelta(days=1)):
        checks.append(_line_get(f"Pushes delivered {d.isoformat()} (JST)",
                                f"/message/delivery/push?date={d.strftime('%Y%m%d')}", token))
    return report
