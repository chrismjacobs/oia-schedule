"""Adding students from the dashboard, now that they don't register themselves."""
from datetime import date

import pytest

from app.extensions import db
from app.models import Semester, Student, User


def _setup(app):
    db.session.add(Semester(id=1, name="Test", starts_on=date(2026, 9, 1),
                            ends_on=date(2027, 1, 31), is_active=True))
    admin = User(username="boss", role="overseer")
    admin.set_password("x")
    db.session.add(admin)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True
    return client


NEW = {"chinese_name": "阿比", "english_name": "Abhishek", "student_id": "1000000",
       "worker_type": "SW", "username": "abhishek", "password": "changeme1"}


def test_overseer_adds_a_student_and_their_login(app):
    client = _setup(app)
    res = client.post("/api/admin/students", json=NEW)
    assert res.status_code == 201, res.get_json()

    student = Student.query.one()
    assert student.english_name == "Abhishek"
    assert student.worker_type == "SW"
    assert student.colour and student.shape, "a token is assigned without being asked for"

    user = User.query.filter_by(username="abhishek").one()
    assert user.student_id == student.id
    assert user.check_password("changeme1")
    assert user.invite_token is None, "no registration link is left lying around"
    assert user.invite_accepted_at is not None, "there is nothing left to accept"


def test_adding_adopts_an_unaccepted_invite_for_the_same_username(app):
    """An invite sent before the switch to manual adding is the same person.

    Creating a second account would leave the first one's registration link
    live, pointing at a username the office has since given a password to.
    """
    client = _setup(app)
    stale = User(username="abhishek", role="student", invite_token="tok-123")
    stale.set_password("placeholder")
    db.session.add(stale)
    db.session.commit()
    stale_id = stale.id

    res = client.post("/api/admin/students", json=NEW)
    assert res.status_code == 201, res.get_json()

    assert User.query.filter_by(username="abhishek").count() == 1, "no duplicate account"
    user = db.session.get(User, stale_id)
    assert user.student_id == Student.query.one().id, "the existing account was adopted"
    assert user.invite_token is None, "the old registration link no longer works"


def test_adding_refuses_a_username_already_belonging_to_someone(app):
    client = _setup(app)
    assert client.post("/api/admin/students", json=NEW).status_code == 201
    second = dict(NEW, student_id="1000001", chinese_name="別人", english_name="Someone")
    res = client.post("/api/admin/students", json=second)
    assert res.status_code == 409
    assert res.get_json()["error"] == "username_taken"


def test_adding_refuses_a_duplicate_student_id(app):
    client = _setup(app)
    assert client.post("/api/admin/students", json=NEW).status_code == 201
    res = client.post("/api/admin/students",
                      json=dict(NEW, username="someone", english_name="Someone"))
    assert res.status_code == 409
    assert res.get_json()["error"] == "student_id_taken"


@pytest.mark.parametrize("field,value,error", [
    ("student_id", "", "invalid_student_id"),
    ("student_id", "has space", "invalid_student_id"),
    ("password", "short", "weak_password"),
    ("worker_type", "XX", "invalid_worker_type"),
])
def test_adding_validates_its_fields(app, field, value, error):
    client = _setup(app)
    res = client.post("/api/admin/students", json=dict(NEW, **{field: value}))
    assert res.status_code in (400, 409)
    assert res.get_json()["error"] == error


def test_adding_needs_at_least_one_name(app):
    client = _setup(app)
    res = client.post("/api/admin/students",
                      json=dict(NEW, chinese_name="", english_name=""))
    assert res.status_code == 400
    assert res.get_json()["error"] == "name_required"


def test_adding_without_an_active_semester_says_so(app):
    """A student belongs to a semester, so there has to be one. Worth its own
    case because the demo seeder deliberately leaves its semester inactive,
    which makes this the first thing anyone hits on a fresh database."""
    admin = User(username="boss", role="overseer")
    admin.set_password("x")
    db.session.add(admin)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True

    res = client.post("/api/admin/students", json=NEW)
    assert res.status_code == 400
    assert res.get_json()["error"] == "no_active_semester"
    assert Student.query.count() == 0


def test_deactivated_students_sort_below_the_working_roster(app):
    """Deactivating is how somebody leaves without losing their history, so
    they shouldn't keep a place in the middle of the list."""
    client = _setup(app)
    for i, (name, active) in enumerate(
            [("Alice", False), ("Bob", True), ("Zara", True)]):
        client.post("/api/admin/students", json=dict(
            NEW, english_name=name, chinese_name="", username=name.lower(),
            student_id=f"200000{i}"))
        if not active:
            s = Student.query.filter_by(english_name=name).one()
            s.is_active = False
            db.session.commit()

    names = [s["english_name"] for s in client.get("/api/admin/students").get_json()]
    assert names == ["Bob", "Zara", "Alice"], "the departed student is last, not alphabetical"
