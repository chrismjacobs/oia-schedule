"""Pluggable notification backends (CLAUDE.md #11). Email works for v1;
LINE Messaging API switches on once the Official Account is set up. Never
LINE Notify — discontinued March 2025."""
import smtplib
import time
from email.mime.text import MIMEText

import requests
from flask import current_app


class NotificationBackend:
    def send(self, message: str):
        raise NotImplementedError


class EmailBackend(NotificationBackend):
    def send(self, message: str):
        cfg = current_app.config
        to_addr = cfg.get("NOTIFICATION_TO_EMAIL")
        if not to_addr or not cfg.get("SMTP_HOST"):
            current_app.logger.info("[notify:email:noop, not configured] %s", message)
            return
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


def get_backend() -> NotificationBackend:
    backend = current_app.config.get("NOTIFICATION_BACKEND", "email")
    if backend == "line":
        return LineBackend()
    return EmailBackend()
