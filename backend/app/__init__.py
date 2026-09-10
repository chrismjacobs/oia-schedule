import logging
import os
import sys

from flask import Flask, jsonify

from app.config import Config, BASE_DIR
from app.extensions import db, migrate, login_manager

FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")


def _safe_db_identity(uri):
    """host/database only — never the password. Printed at boot so it's
    obvious which database a deployment is actually talking to; 'the app I'm
    debugging locally and the app on Render disagree' is otherwise very hard
    to see."""
    try:
        from sqlalchemy.engine.url import make_url
        u = make_url(uri)
        return f"{u.drivername}://{u.host or 'local'}/{u.database}"
    except Exception:
        return "unparseable"


def configure_logging(app):
    """Log INFO to stdout so Render's log stream actually shows the app's
    reasoning. Flask's logger inherits WARNING from the root logger when not
    in debug, which silently swallows every info-level breadcrumb — including
    everything /tick reports about what it did and why."""
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

    class TaipeiFormatter(logging.Formatter):
        """Stamp log lines in Taipei time. The default formatter uses the
        server's clock — UTC on Render — which puts every line 8 hours behind
        the app's own timestamps and behind Render's log viewer, so a message
        about a 08:15 slot appears to have been logged at 00:15."""

        def formatTime(self, record, datefmt=None):
            from app.utils.tz import local_now
            return local_now().strftime(datefmt or "%Y-%m-%d %H:%M:%S")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TaipeiFormatter(
        "[%(asctime)s TPE] %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, logging.StreamHandler)]
    root.addHandler(handler)
    root.setLevel(level)

    app.logger.handlers = []
    app.logger.propagate = True
    app.logger.setLevel(level)
    # Chatty at INFO and not worth the noise.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def create_app(config_class=Config):
    app = Flask(
        __name__,
        template_folder=os.path.join(FRONTEND_DIR, "templates"),
        static_folder=os.path.join(FRONTEND_DIR, "static"),
        static_url_path="/static",
    )
    app.config.from_object(config_class)
    configure_logging(app)

    # Boot banner: the handful of facts that explain most "why is production
    # behaving differently?" questions, answerable from the log alone.
    from app.utils.tz import local_now
    from app.notifications.backends import automatic_notifications_are_live
    live = automatic_notifications_are_live(app.config)
    app.logger.info(
        "boot | db=%s | notifications=%s | AUTOMATIC NOTIFICATIONS: %s | "
        "line_token=%s line_group=%s | tick_token=%s | debug=%s | taipei_now=%s",
        _safe_db_identity(app.config["SQLALCHEMY_DATABASE_URI"]),
        app.config.get("NOTIFICATION_BACKEND"),
        "LIVE" if live else "DRY-RUN (nothing will be sent)",
        "set" if app.config.get("LINE_TOKEN") else "MISSING",
        "set" if app.config.get("LINE_GROUP_ID") else "MISSING",
        "default-INSECURE" if app.config["TICK_TOKEN"] == "dev-tick-token-change-me" else "set",
        app.config.get("DEBUG"),
        local_now().isoformat(timespec="seconds"),
    )
    if not live:
        app.logger.warning(
            "Automatic notifications are DRY-RUN because the database is SQLite. "
            "Set ALLOW_LIVE_NOTIFICATIONS=1 to send for real. "
            "Advanced > test send is unaffected and always sends.")

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        # Only flask_login's own @login_required would land here; every route
        # in this app uses the explicit page_/api_ decorators instead (see
        # utils/decorators.py), which redirect or return JSON themselves.
        return jsonify({"error": "unauthenticated"}), 401

    from app.pages import bp as pages_bp
    from app.auth import bp as auth_bp
    from app.admin import bp as admin_bp
    from app.availability import bp as availability_bp
    from app.schedule import bp as schedule_bp
    from app.attendance import bp as attendance_bp
    from app.leave import bp as leave_bp
    from app.tasks import bp as tasks_bp
    from app.timecards import bp as timecards_bp
    from app.dashboard import bp as dashboard_bp
    from app.notifications import bp as notifications_bp

    for bp in (pages_bp, auth_bp, admin_bp, availability_bp, schedule_bp, attendance_bp,
               leave_bp, tasks_bp, timecards_bp, dashboard_bp, notifications_bp):
        app.register_blueprint(bp)

    # The canonical LINE webhook is /api/line/webhook, but the LINE console has
    # historically been pointed at /line/callback (no /api), which 404s and shows
    # up as "webhook delivery failed" on Verify. Accept both rather than making
    # the registered URL the one thing that must not drift.
    from app.notifications.routes import line_webhook
    app.add_url_rule("/line/callback", "line_callback_root", line_webhook, methods=["POST"])

    @app.get("/api/health")
    @app.get("/health")
    def health():
        """Liveness, plus when /tick last actually ran.

        This endpoint does NOT run the scheduled work — /api/tick does. Health
        reports `last_tick_at` precisely so pointing the cron at the wrong URL
        is visible instead of silent: a stale value here means nothing has been
        opening selection windows, flagging no-shows, or advertising slots.
        """
        from datetime import datetime
        from app.utils.settings import get_setting
        from app.utils.tz import local_now

        now = local_now()
        last = get_setting("last_tick_at")
        minutes = None
        if last:
            try:
                minutes = round((now - datetime.fromisoformat(last)).total_seconds() / 60, 1)
            except ValueError:
                pass
        from app.notifications.backends import automatic_notifications_are_live
        from app.utils.settings import get_attendance_notify_enabled
        return jsonify({
            "ok": True,
            "now": now.isoformat(),
            "last_tick_at": last,
            "minutes_since_tick": minutes,
            "tick_healthy": minutes is not None and minutes < 30,
            # The two switches that make notifications vanish without erroring.
            "notifications_live": automatic_notifications_are_live(app.config),
            "notification_backend": app.config.get("NOTIFICATION_BACKEND"),
            "signin_notifications_enabled": get_attendance_notify_enabled(),
        })

    @app.cli.command("seed-demo")
    def seed_demo_cli():
        """Insert demo students/availability/committed schedule (CLAUDE.md #17)."""
        from app.admin.demo import seed_demo
        result = seed_demo()
        print(f"Seeded demo data: {result}")

    @app.cli.command("reset-demo")
    def reset_demo_cli():
        """Delete only is_demo rows, leaving real data untouched."""
        from app.admin.demo import reset_demo
        result = reset_demo()
        print(f"Reset demo data: {result}")

    return app
