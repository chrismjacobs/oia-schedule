from flask import Blueprint

bp = Blueprint("leave", __name__, url_prefix="/api/leave")

from app.leave import routes  # noqa: E402,F401
