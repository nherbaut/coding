import datetime
import smtplib
import uuid
from unittest.mock import patch
from django.core import mail
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from .importers import put_document
from .models import Assignment, Case, SharedCodebook, Submission, SubmissionEmail
from .notifications import deliver_next_email
from .services import Conflict, create_session, submit


@override_settings(
    CASE_EMAIL_ENABLED=True,
    CASE_SUBMISSION_EMAIL_TO=["admin@example.org"],
    DEFAULT_FROM_EMAIL="no-reply@example.org",
    PUBLIC_BASE_URL="https://coding.example.org",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class SubmissionEmailTests(TestCase):
    def setUp(self):
        SharedCodebook.objects.create(content={"version": 1, "codes": []})
        case = Case.objects.create(key="mail", name="Review case")
        put_document("mail-doc", "Policy", "Review before merging", {}, [case])
        self.session, _ = create_session("Coder", [case])
        self.assignment = Assignment.objects.get(session=self.session)

    def test_submission_queues_one_message_and_worker_delivers_it(self):
        submission = submit(self.assignment.pk, self.session.pk, 0)
        job = SubmissionEmail.objects.get()
        self.assertEqual(job.submission, submission)
        self.assertEqual(job.attempts, 0)
        self.assertFalse(getattr(mail, "outbox", []))
        with self.assertRaises(Conflict):
            submit(self.assignment.pk, self.session.pk, 0)
        self.assertEqual(SubmissionEmail.objects.count(), 1)
        self.assertTrue(deliver_next_email())
        self.assertFalse(deliver_next_email())
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["admin@example.org"])
        self.assertIn("Coder", message.body)
        self.assertIn(
            "https://coding.example.org/review/" + str(submission.pk) + "/",
            message.body,
        )
        self.assertNotIn("/access/#", message.body)
        job.refresh_from_db()
        self.assertIsNotNone(job.sent_at)
        self.assertEqual(job.status, "Sent")

    def test_failed_smtp_keeps_submission_and_retries(self):
        submit(self.assignment.pk, self.session.pk, 0)
        with patch(
            "corpus.notifications.EmailMessage.send",
            side_effect=smtplib.SMTPAuthenticationError(
                535, b"private provider details"
            ),
        ):
            self.assertTrue(deliver_next_email())
        job = SubmissionEmail.objects.get()
        self.assertIsNone(job.sent_at)
        self.assertEqual(job.last_error, "SMTPAuthenticationError (535)")
        self.assertGreater(job.next_attempt_at, timezone.now())
        self.assertEqual(job.status, "Retrying")
        self.assignment.refresh_from_db()
        self.assertTrue(self.assignment.locked)
        self.assertFalse(deliver_next_email())
        SubmissionEmail.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())
        self.assertTrue(deliver_next_email())
        job.refresh_from_db()
        self.assertEqual(job.attempts, 2)
        self.assertIsNotNone(job.sent_at)

    def test_rollback_discards_submission_and_email(self):
        with self.assertRaises(ValueError), transaction.atomic():
            submit(self.assignment.pk, self.session.pk, 0)
            raise ValueError("Cancel transaction")
        self.assertEqual(Submission.objects.count(), 0)
        self.assertEqual(SubmissionEmail.objects.count(), 0)

    def test_expired_delivery_lease_is_recovered(self):
        submit(self.assignment.pk, self.session.pk, 0)
        job = SubmissionEmail.objects.get()
        SubmissionEmail.objects.filter(pk=job.pk).update(
            lease_token=uuid.uuid4(),
            locked_until=timezone.now() + datetime.timedelta(minutes=1),
        )
        self.assertFalse(deliver_next_email())
        SubmissionEmail.objects.filter(pk=job.pk).update(
            locked_until=timezone.now() - datetime.timedelta(seconds=1)
        )
        self.assertTrue(deliver_next_email())

    @override_settings(CASE_EMAIL_ENABLED=False)
    def test_disabled_email_preserves_submission(self):
        submit(self.assignment.pk, self.session.pk, 0)
        self.assertEqual(SubmissionEmail.objects.count(), 0)
        self.assertEqual(Submission.objects.count(), 1)

    def test_disabling_delivery_holds_existing_queue(self):
        submit(self.assignment.pk, self.session.pk, 0)
        with override_settings(CASE_EMAIL_ENABLED=False):
            self.assertFalse(deliver_next_email())
        job = SubmissionEmail.objects.get()
        self.assertEqual(job.attempts, 0)
        self.assertIsNone(job.sent_at)
        self.assertTrue(deliver_next_email())
