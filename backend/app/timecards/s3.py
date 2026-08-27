from app.utils.s3 import upload_object, presigned_view_url  # noqa: F401


def upload_timecard(file_storage, student_id, period_label):
    return upload_object(file_storage, f"timecards/{student_id}/{period_label}")
