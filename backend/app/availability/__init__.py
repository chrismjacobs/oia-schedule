from flask import Blueprint

bp = Blueprint("availability", __name__, url_prefix="/api/availability")

from app.availability import routes  # noqa: E402,F401
