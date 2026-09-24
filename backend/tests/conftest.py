"""Test fixtures — and the guard that keeps the suite off the real database.

DATABASE_URL is set here at import time, not inside a fixture, and that
ordering is the whole point. pytest imports conftest before it imports the
test modules, and those import app.models, which imports app.config, which
resolves the database URL once and keeps it. Set it any later and the suite
quietly runs against whatever .env points at — which for this project is the
live Neon database.

_require_throwaway_database is the second line of defence, because the first
one is a matter of import order and import order is easy to break.
"""
import os
import sys
import tempfile

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

# A file rather than :memory: — Flask-SQLAlchemy hands out a connection per
# session, and an in-memory database is private to the connection that made
# it, so the tables would vanish between the fixture and the test.
_TMP = tempfile.mkdtemp(prefix="oia-tests-")
_DB_PATH = os.path.join(_TMP, "test.db").replace("\\", "/")
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH
os.environ["FLASK_DEBUG"] = "1"
# Belt and braces: even pointed at a real database, nothing may be pushed.
os.environ["ALLOW_LIVE_NOTIFICATIONS"] = "0"
os.environ["NOTIFICATION_BACKEND"] = "email"


def _require_throwaway_database():
    from app.config import Config
    uri = Config.SQLALCHEMY_DATABASE_URI
    if not uri.startswith("sqlite:///") or _DB_PATH not in uri:
        pytest.exit(
            "Refusing to run: the tests resolved to a database that is not "
            f"this run's throwaway SQLite file.\n  got: {uri}\n"
            "Something imported app.config before conftest set DATABASE_URL.",
            returncode=2,
        )


@pytest.fixture()
def app():
    _require_throwaway_database()

    from app import create_app
    from app.extensions import db

    application = create_app()
    with application.app_context():
        db.drop_all()
        db.create_all()
        yield application
        db.session.remove()


@pytest.fixture()
def db_session(app):
    from app.extensions import db
    return db.session
