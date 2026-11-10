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
from .models import SubmissionEmail

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


def deliver_next_email():
    if not settings.CASE_EMAIL_ENABLED:
        return False
    now = timezone.now()
    with transaction.atomic():
        job = (
            SubmissionEmail.objects.select_for_update()
            .filter(sent_at__isnull=True, next_attempt_at__lte=now)
            .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
            .order_by("pk")
            .first()
        )
        if job is None:
            return False
        token = uuid.uuid4()
        job.lease_token = token
        job.locked_until = now + datetime.timedelta(minutes=2)
        job.attempts += 1
        job.save(update_fields=["lease_token", "locked_until", "attempts"])
    try:
        message = EmailMessage(
            job.subject,
            job.body,
            job.sender,
            job.recipients,
            headers={"Message-ID": job.message_id},
        )
        if message.send(fail_silently=False) != 1:
            raise smtplib.SMTPException("SMTP backend did not accept the message")
    except (OSError, smtplib.SMTPException, ValueError) as error:
        # Provider responses can include credentials or addresses. Persist only type/code.
        summary = type(error).__name__
        if isinstance(error, smtplib.SMTPResponseException):
            summary += f" ({error.smtp_code})"
        delay = min(3600, 30 * 2 ** min(job.attempts - 1, 10))
        SubmissionEmail.objects.filter(pk=job.pk, lease_token=token).update(
            lease_token=None,
            locked_until=None,
            last_error=summary,
            next_attempt_at=timezone.now() + datetime.timedelta(seconds=delay),
        )
        logger.warning(
            "Submission email %s will retry after %s seconds (%s)",
            job.pk,
            delay,
            summary,
        )
    else:
        SubmissionEmail.objects.filter(pk=job.pk, lease_token=token).update(
            sent_at=timezone.now(), lease_token=None, locked_until=None, last_error=""
        )
        logger.info("Submission email %s accepted by SMTP server", job.pk)
    return True
