"""Pluggable notification backends (CLAUDE.md #11). Email works for v1;
LINE Messaging API switches on once the Official Account is set up. Never
LINE Notify — discontinued March 2025."""
import smtplib
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

    def send(self, message: str, to: str = None):
        cfg = current_app.config
        token = cfg.get("LINE_TOKEN")
        target = to or cfg.get("LINE_GROUP_ID")
        if not token or not target:
            current_app.logger.info("[notify:line:noop, not configured] %s", message)
            return
        resp = requests.post(
            self.PUSH_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": target, "messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        resp.raise_for_status()


def get_backend() -> NotificationBackend:
    backend = current_app.config.get("NOTIFICATION_BACKEND", "email")
    if backend == "line":
        return LineBackend()
    return EmailBackend()
