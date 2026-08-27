from flask import jsonify, request
from flask_login import current_user

from app.tasks import bp
from app.extensions import db
from app.models import RegularTask, CustomTask
from app.utils.decorators import overseer_required, login_required_api
from app.utils.s3 import upload_object, presigned_view_url
from app.utils.tz import local_now


@bp.get("/regular")
@overseer_required
def list_regular():
    rows = RegularTask.query.order_by(RegularTask.title_en).all()
    return jsonify([r.to_dict() for r in rows])


@bp.post("/regular")
@overseer_required
def create_regular():
    data = request.get_json(force=True) or {}
    if not data.get("title_zh") or not data.get("frequency"):
        return jsonify({"error": "missing_fields"}), 400
    if data["frequency"] not in ("daily", "weekly", "monthly"):
        return jsonify({"error": "invalid_frequency"}), 400
    t = RegularTask(
        title_zh=data["title_zh"], title_en=data.get("title_en"),
        description=data.get("description"), frequency=data["frequency"],
        interval=int(data.get("interval", 1)), photo_required=bool(data.get("photo_required", False)),
    )
    db.session.add(t)
    db.session.commit()
    return jsonify(t.to_dict()), 201


@bp.patch("/regular/<int:task_id>")
@overseer_required
def update_regular(task_id):
    t = RegularTask.query.get_or_404(task_id)
    data = request.get_json(force=True) or {}
    for field in ("title_zh", "title_en", "description", "frequency", "interval", "is_active", "photo_required"):
        if field in data:
            setattr(t, field, data[field])
    db.session.commit()
    return jsonify(t.to_dict())


@bp.post("/regular/<int:task_id>/reference-photo")
@overseer_required
def upload_regular_reference(task_id):
    t = RegularTask.query.get_or_404(task_id)
    if "file" not in request.files:
        return jsonify({"error": "file_required"}), 400
    t.reference_s3_key = upload_object(request.files["file"], f"tasks/regular/{task_id}/reference")
    db.session.commit()
    return jsonify(t.to_dict())


@bp.get("/regular/<int:task_id>/reference-photo-url")
@login_required_api
def get_regular_reference_url(task_id):
    t = RegularTask.query.get_or_404(task_id)
    if not t.reference_s3_key:
        return jsonify({"error": "no_photo"}), 404
    return jsonify({"url": presigned_view_url(t.reference_s3_key)})


@bp.get("/custom")
@login_required_api
def list_custom():
    status = request.args.get("status")
    q = CustomTask.query
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(CustomTask.id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@bp.post("/custom")
@overseer_required
def create_custom():
    data = request.get_json(force=True) or {}
    if not data.get("title_zh"):
        return jsonify({"error": "missing_fields"}), 400
    t = CustomTask(
        title_zh=data["title_zh"], title_en=data.get("title_en"),
        description=data.get("description"), created_by=current_user.id, status="open",
        photo_required=bool(data.get("photo_required", False)),
    )
    db.session.add(t)
    db.session.commit()
    return jsonify(t.to_dict()), 201


@bp.patch("/custom/<int:task_id>")
@overseer_required
def update_custom(task_id):
    t = CustomTask.query.get_or_404(task_id)
    data = request.get_json(force=True) or {}
    for field in ("title_zh", "title_en", "description", "status", "photo_required"):
        if field in data:
            setattr(t, field, data[field])
    db.session.commit()
    return jsonify(t.to_dict())


@bp.post("/custom/<int:task_id>/reference-photo")
@overseer_required
def upload_custom_reference(task_id):
    t = CustomTask.query.get_or_404(task_id)
    if "file" not in request.files:
        return jsonify({"error": "file_required"}), 400
    t.reference_s3_key = upload_object(request.files["file"], f"tasks/custom/{task_id}/reference")
    db.session.commit()
    return jsonify(t.to_dict())


@bp.get("/custom/<int:task_id>/reference-photo-url")
@login_required_api
def get_custom_reference_url(task_id):
    t = CustomTask.query.get_or_404(task_id)
    if not t.reference_s3_key:
        return jsonify({"error": "no_photo"}), 404
    return jsonify({"url": presigned_view_url(t.reference_s3_key)})


@bp.post("/custom/<int:task_id>/claim")
@login_required_api
def claim_custom(task_id):
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    updated = (
        db.session.query(CustomTask)
        .filter(CustomTask.id == task_id, CustomTask.status == "open")
        .update({"status": "claimed", "claimed_by": current_user.student_id, "claimed_at": local_now()},
                synchronize_session=False)
    )
    if updated == 0:
        db.session.rollback()
        return jsonify({"error": "not_open"}), 409
    db.session.commit()
    return jsonify(CustomTask.query.get(task_id).to_dict())


@bp.post("/custom/<int:task_id>/proof-photo")
@login_required_api
def upload_custom_proof(task_id):
    """Student's completion evidence — separate key from the admin's reference photo."""
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    t = CustomTask.query.get_or_404(task_id)
    if t.claimed_by != current_user.student_id:
        return jsonify({"error": "not_your_task"}), 403
    if "file" not in request.files:
        return jsonify({"error": "file_required"}), 400
    t.proof_s3_key = upload_object(request.files["file"], f"tasks/custom/{task_id}/proof")
    db.session.commit()
    return jsonify(t.to_dict())
