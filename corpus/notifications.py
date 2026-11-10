"""Durable case-submission email delivery, separate from the submission transaction."""

import datetime
import logging
import smtplib
import uuid
from urllib.parse import urlsplit
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import SubmissionEmail, InvitationEmail
from .invitations import invitation_cipher
from cryptography.fernet import InvalidToken

logger = logging.getLogger(__name__)


def queue_submission_email(submission):
    if not settings.CASE_EMAIL_ENABLED:
        return
    snapshot = submission.snapshot
    subject = "[Corpus] Case submitted: " + " ".join(snapshot["case"].splitlines())
    body = (
        f"{snapshot['coder']} submitted a case for review.\n\n"
        f"Case: {snapshot['case']}\n"
        f"Submitted: {submission.created.isoformat()}\n"
        f"Documents: {len(snapshot['documents'])}\n"
        f"Annotations: {len(snapshot['annotations'])}\n\n"
        f"Review: {settings.PUBLIC_BASE_URL.rstrip('/')}/review/{submission.pk}/\n"
    )
    domain = urlsplit(settings.PUBLIC_BASE_URL).hostname
    SubmissionEmail.objects.create(
        submission=submission,
        subject=subject[:250],
        body=body,
        recipients=list(settings.CASE_SUBMISSION_EMAIL_TO),
        sender=settings.DEFAULT_FROM_EMAIL,
        message_id=f"<coding-submission-{submission.pk}-{uuid.uuid4().hex}@{domain}>",
    )


def claim_next_email():
    now = timezone.now()
    for model in (SubmissionEmail, InvitationEmail):
        with transaction.atomic():
            query = (
                model.objects.select_for_update()
                .filter(sent_at__isnull=True, next_attempt_at__lte=now)
                .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
            )
            if model is InvitationEmail:
                query = query.filter(cancelled_at__isnull=True)
            job = query.order_by("pk").first()
            if job is None:
                continue
            job.lease_token = uuid.uuid4()
            job.locked_until = now + datetime.timedelta(minutes=2)
            job.attempts += 1
            job.save(update_fields=["lease_token", "locked_until", "attempts"])
            return job
    return None


def deliver_next_email():
    if not settings.CASE_EMAIL_ENABLED:
        return False
    job = claim_next_email()
    if job is None:
        return False
    model = type(job)
    updates = model.objects.filter(pk=job.pk, lease_token=job.lease_token)
    try:
        body = job.body
        if isinstance(job, InvitationEmail):
            if job.token_hash != job.session.token_hash:
                updates.update(
                    cancelled_at=timezone.now(),
                    body="",
                    lease_token=None,
                    locked_until=None,
                )
                return True
            body = invitation_cipher().decrypt(job.body.encode()).decode()
        message = EmailMessage(
            job.subject,
            body,
            job.sender,
            job.recipients,
            headers={"Message-ID": job.message_id},
        )
        if message.send(fail_silently=False) != 1:
            raise smtplib.SMTPException("SMTP backend did not accept the message")
    except (OSError, smtplib.SMTPException, ValueError, InvalidToken) as error:
        summary = type(error).__name__
        if isinstance(error, smtplib.SMTPResponseException):
            summary += f" ({error.smtp_code})"
        delay = min(3600, 30 * 2 ** min(job.attempts - 1, 10))
        updates.update(
            lease_token=None,
            locked_until=None,
            last_error=summary,
            next_attempt_at=timezone.now() + datetime.timedelta(seconds=delay),
        )
        logger.warning(
            "%s %s will retry after %s seconds (%s)",
            model.__name__,
            job.pk,
            delay,
            summary,
        )
    else:
        values = dict(
            sent_at=timezone.now(), lease_token=None, locked_until=None, last_error=""
        )
        if isinstance(job, InvitationEmail):
            values["body"] = ""
        updates.update(**values)
        logger.info("%s %s accepted by SMTP server", model.__name__, job.pk)
    return True
