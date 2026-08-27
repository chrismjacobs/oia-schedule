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
