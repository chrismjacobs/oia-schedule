import base64
import hashlib
import hmac

from flask import jsonify, request, current_app

from app.notifications import bp
from app.notifications.tick import run_tick
from app.notifications.backends import LineBackend, EmailBackend
from app.utils.decorators import tick_token_required, overseer_required


@bp.post("/tick")
@tick_token_required
def tick():
    result = run_tick()
    return jsonify(result)


def _verify_line_signature(body: bytes, signature: str) -> bool:
    secret = current_app.config.get("LINE_SECRET")
    if not secret or not signature:
        return False
    expected = base64.b64encode(hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()).decode("utf-8")
    return hmac.compare_digest(expected, signature)


@bp.post("/line/webhook")
def line_webhook():
    """LINE calls this on every message/join/etc. Its only real job right now
    is telling whoever messages the bot their own user/group/room ID, since
    there's no other way to get one — LINE never exposes it in the app UI.
    Copy the ID it replies with into Setup > Notification test send, or into
    LINE_GROUP_ID in .env / Render's env vars."""
    signature = request.headers.get("X-Line-Signature", "")
    if not _verify_line_signature(request.get_data(), signature):
        return jsonify({"error": "invalid_signature"}), 403

    payload = request.get_json(silent=True) or {}
    line = LineBackend()
    for event in payload.get("events", []):
        reply_token = event.get("replyToken")
        source = event.get("source", {})
        source_type = source.get("type")  # user | group | room
        source_id = source.get(f"{source_type}Id") if source_type else None
        if reply_token and source_id:
            try:
                line.reply(reply_token, f"{source_type} ID:\n{source_id}")
            except Exception:
                current_app.logger.exception("LINE webhook reply failed")

    return jsonify({"ok": True})


@bp.post("/test-send")
@overseer_required
def test_send():
    """Manual send for initial LINE/email wiring checks (CLAUDE.md #12) — bypasses
    the notification_log dedup since this is a one-off admin test, not an
    automated notification."""
    data = request.get_json(force=True) or {}
    message = (data.get("message") or "").strip()
    target = (data.get("target") or "").strip() or None
    backend_name = data.get("backend") or current_app.config["NOTIFICATION_BACKEND"]
    if not message:
        return jsonify({"error": "message_required"}), 400

    try:
        if backend_name == "line":
            LineBackend().send(message, to=target)
        else:
            EmailBackend().send(message)
    except Exception as e:
        return jsonify({"error": "send_failed", "message": str(e)}), 502

    return jsonify({"ok": True, "backend": backend_name})
