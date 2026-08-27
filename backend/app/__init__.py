import os

from flask import Flask, jsonify

from app.config import Config, BASE_DIR
from app.extensions import db, migrate, login_manager

FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")


def create_app(config_class=Config):
    app = Flask(
        __name__,
        template_folder=os.path.join(FRONTEND_DIR, "templates"),
        static_folder=os.path.join(FRONTEND_DIR, "static"),
        static_url_path="/static",
    )
    app.config.from_object(config_class)

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

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True})

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
