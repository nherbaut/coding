import uuid
from django.contrib.auth import get_user_model
from .studies import create_study


def new_study():
    owner = get_user_model().objects.create_user(
        "owner-" + uuid.uuid4().hex, is_staff=True
    )
    study = create_study(owner, "Research study")
    study.notification_emails = ["admin@example.org"]
    study.save(update_fields=["notification_emails"])
    return study
