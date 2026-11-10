"""Private coder invitation bodies are encrypted while awaiting SMTP delivery."""

import base64
import hashlib
import uuid
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.validators import validate_email
from .models import InvitationEmail


def invitation_cipher():
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    )
    return Fernet(key)


def queue_invitation_email(session, link):
    if not settings.CASE_EMAIL_ENABLED:
        raise ValueError(
            "Configure transactional email before sending coder invitations"
        )
    validate_email(session.email)
    assignments = (
        session.assignment_set.filter(locked=False)
        .select_related("case")
        .order_by("case__name")
    )
    case_list = "\n".join(
        "- " + " ".join(item.case.name.splitlines()) for item in assignments
    )
    if not case_list:
        case_list = "No cases are currently awaiting coding."
    body = (
        f"Hello {session.coder},\n\nYour coding session is ready.\n\n"
        f'Open your private workspace:\n{settings.PUBLIC_BASE_URL.rstrip("/")}{link}\n\n'
        f"Cases to code:\n{case_list}\n\n"
        "Keep this link private. Submit each case when you have finished coding it.\n"
    )
    domain = urlsplit(settings.PUBLIC_BASE_URL).hostname
    return InvitationEmail.objects.create(
        session=session,
        token_hash=session.token_hash,
        subject="[Corpus] Your coding assignment",
        body=invitation_cipher().encrypt(body.encode()).decode(),
        sender=settings.DEFAULT_FROM_EMAIL,
        recipients=[session.email],
        message_id=f"<coding-invitation-{session.pk}-{uuid.uuid4().hex}@{domain}>",
    )
