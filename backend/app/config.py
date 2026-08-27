import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _bool(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    # Jinja-rendered, cookie-session pages — SameSite=Lax blocks the cookie
    # from being attached to cross-site POST/PATCH/DELETE, which is our CSRF
    # mitigation. SECURE is on by default in production (set FLASK_DEBUG=1
    # locally over http to disable it).
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = not _bool("FLASK_DEBUG", False)
    SESSION_COOKIE_HTTPONLY = True

    _db_url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "backend", "oia.db"))
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _db_url
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
    AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET")
    AWS_S3_REGION = os.environ.get("AWS_S3_REGION", "ap-northeast-1")

    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

    # /tick endpoint auth (external cron pings this)
    TICK_TOKEN = os.environ.get("TICK_TOKEN", "dev-tick-token-change-me")

    # --- Config over hard-coding (CLAUDE.md #17) ---
    TIMEZONE = os.environ.get("TIMEZONE", "Asia/Taipei")

    # Sign-in opens N minutes before a scheduled slot (CLAUDE.md #8)
    SIGN_IN_OPENS_MINUTES_BEFORE = int(os.environ.get("SIGN_IN_OPENS_MINUTES_BEFORE", 10))

    # No-show grace period after slot start before flagging (CLAUDE.md #11)
    NO_SHOW_GRACE_MINUTES = int(os.environ.get("NO_SHOW_GRACE_MINUTES", 15))

    # If a session is still open this long after sign-in, flag forgot-to-sign-out
    # instead of guessing an end time (CLAUDE.md #8).
    FORGOT_SIGNOUT_AFTER_HOURS = int(os.environ.get("FORGOT_SIGNOUT_AFTER_HOURS", 9))

    # Closing warning fires this many hours before selection closes (CLAUDE.md #11)
    CLOSING_WARNING_HOURS_BEFORE = int(os.environ.get("CLOSING_WARNING_HOURS_BEFORE", 24))

    # Leave-pattern thresholds the overseer's dashboard flags (CLAUDE.md #7)
    LEAVE_TOO_OFTEN_COUNT = int(os.environ.get("LEAVE_TOO_OFTEN_COUNT", 3))
    LEAVE_TOO_LATE_HOURS = int(os.environ.get("LEAVE_TOO_LATE_HOURS", 24))

    # Floor guarantee: minimum hours before anyone gets extra (CLAUDE.md #6)
    SOLVER_FLOOR_HOURS = int(os.environ.get("SOLVER_FLOOR_HOURS", 4))

    # Solver objective weights, labelled & tunable (CLAUDE.md #6). Higher priority
    # items use larger weights so they dominate lower-priority ones.
    SOLVER_WEIGHTS = {
        "coverage": int(os.environ.get("SOLVER_W_COVERAGE", 1000)),
        "floor_guarantee": int(os.environ.get("SOLVER_W_FLOOR", 500)),
        "contiguity": int(os.environ.get("SOLVER_W_CONTIGUITY", 20)),
        "low_churn": int(os.environ.get("SOLVER_W_LOW_CHURN", 10)),
        "equalise_hours": int(os.environ.get("SOLVER_W_EQUALISE", 1)),
    }

    # Timecard upload cadence default (CLAUDE.md #10)
    TIMECARD_CADENCE_DEFAULT = os.environ.get("TIMECARD_CADENCE_DEFAULT", "monthly")

    # Notification backend: "email" (v1 default) or "line" (CLAUDE.md #12).
    # LINE Messaging API (not LINE Notify — discontinued March 2025).
    NOTIFICATION_BACKEND = os.environ.get("NOTIFICATION_BACKEND", "email")
    LINE_CHANNEL = os.environ.get("LINE_CHANNEL")            # channel ID
    LINE_SECRET = os.environ.get("LINE_SECRET")              # channel secret (webhook signature verification)
    LINE_TOKEN = os.environ.get("LINE_TOKEN")                # channel access token (push messages)
    LINE_GROUP_ID = os.environ.get("LINE_GROUP_ID")          # not yet known — set once the bot is added to the group
    SMTP_HOST = os.environ.get("SMTP_HOST")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    NOTIFICATION_FROM_EMAIL = os.environ.get("NOTIFICATION_FROM_EMAIL", "oia-noreply@example.com")
    NOTIFICATION_TO_EMAIL = os.environ.get("NOTIFICATION_TO_EMAIL")  # v1: single group inbox

    # Cron window guidance only (actual gating is done by the external cron config,
    # not enforced in-app) — see CLAUDE.md #12.
    BUSINESS_HOURS_START = int(os.environ.get("BUSINESS_HOURS_START", 7))
    BUSINESS_HOURS_END = int(os.environ.get("BUSINESS_HOURS_END", 18))

    DEBUG = _bool("FLASK_DEBUG", False)
