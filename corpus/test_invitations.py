from .test_support import new_study
from .studies import publish_codebook
import hashlib
import re
import smtplib
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from .invitations import invitation_cipher
from .models import Case, CodingSession, InvitationEmail, SharedCodebook
from .notifications import deliver_next_email


@override_settings(
    CASE_EMAIL_ENABLED=True,
    DEFAULT_FROM_EMAIL="no-reply@example.org",
    PUBLIC_BASE_URL="https://coding.example.org",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class InvitationEmailTests(TestCase):

    def setUp(self):
        self.study = new_study()
        publish_codebook(
            self.study,
            book={"version": 1, "codes": []},
            expected_revision=(self.study)
            .__class__.objects.get(pk=(self.study).pk)
            .codebook_id,
        )
        self.beta = Case.objects.create(
            key="beta", name="Beta project", study=self.study
        )
        self.alpha = Case.objects.create(
            key="alpha", name="Alpha project", study=self.study
        )
        self.admin = get_user_model().objects.create_superuser(
            "admin", "admin@example.org", "password"
        )
        self.study.owner = self.admin
        self.study.save(update_fields=["owner"])
        self.client.force_login(self.admin)

    def create(self, email="coder@example.org"):
        response = self.client.post(
            f"/admin/studies/{self.study.pk}/",
            {
                "action": "create",
                "coder": "Coder",
                "email": email,
                "cases": [self.beta.pk, self.alpha.pk],
            },
        )
        self.assertEqual(response.status_code, 200)
        return CodingSession.objects.get()

    def body_and_token(self, job):
        body = invitation_cipher().decrypt(job.body.encode()).decode()
        token = re.search("/access/#([^\\s]+)", body).group(1)
        return (body, token)

    def test_creation_queues_private_link_and_sorted_cases(self):
        session = self.create()
        job = InvitationEmail.objects.get()
        body, token = self.body_and_token(job)
        self.assertEqual(session.email, "coder@example.org")
        self.assertEqual(job.recipients, ["coder@example.org"])
        self.assertNotIn(token, job.body)
        self.assertEqual(session.token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertLess(body.index("Alpha project"), body.index("Beta project"))
        self.assertIn("https://coding.example.org/access/#", body)
        self.assertTrue(deliver_next_email())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].body, body)
        job.refresh_from_db()
        self.assertEqual(job.body, "")
        self.assertEqual(job.status, "Sent")
        coder = Client()
        self.assertEqual(
            coder.post(
                "/access/exchange/", {"token": token}, content_type="application/json"
            ).status_code,
            200,
        )
        self.assertEqual(len(coder.get("/api/state/").json()["assignments"]), 2)

    def test_resend_replaces_link_and_cancels_pending_old_email(self):
        session = self.create()
        old = InvitationEmail.objects.get()
        _, old_token = self.body_and_token(old)
        response = self.client.post(
            f"/admin/studies/{self.study.pk}/",
            {
                "action": "send_invitation",
                "session": session.pk,
                "email": "other@example.org",
            },
        )
        self.assertEqual(response.status_code, 200)
        old.refresh_from_db()
        self.assertIsNotNone(old.cancelled_at)
        self.assertEqual(old.body, "")
        new = InvitationEmail.objects.latest("pk")
        _, new_token = self.body_and_token(new)
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(new.recipients, ["other@example.org"])
        self.assertEqual(
            Client()
            .post(
                "/access/exchange/",
                {"token": old_token},
                content_type="application/json",
            )
            .status_code,
            404,
        )
        self.assertTrue(deliver_next_email())
        self.assertFalse(deliver_next_email())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(new_token, mail.outbox[0].body)

    def test_revocation_cancels_queued_delivery(self):
        session = self.create()
        self.client.post(
            f"/admin/studies/{self.study.pk}/",
            {"action": "revoke", "session": session.pk},
        )
        job = InvitationEmail.objects.get()
        self.assertEqual(job.status, "Cancelled")
        self.assertEqual(job.body, "")
        self.assertFalse(deliver_next_email())

    def test_smtp_retry_preserves_encryption_until_sent(self):
        self.create()
        job = InvitationEmail.objects.get()
        body, token = self.body_and_token(job)
        message_id = job.message_id
        with patch(
            "corpus.notifications.EmailMessage.send",
            side_effect=smtplib.SMTPException("Temporary failure"),
        ):
            self.assertTrue(deliver_next_email())
        job.refresh_from_db()
        self.assertNotIn(token, job.body)
        self.assertEqual(job.status, "Retrying")
        InvitationEmail.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())
        self.assertTrue(deliver_next_email())
        job.refresh_from_db()
        self.assertEqual(job.body, "")
        self.assertEqual(mail.outbox[0].extra_headers["Message-ID"], message_id)
        self.assertEqual(mail.outbox[0].body, body)

    def test_invalid_recipient_does_not_replace_link(self):
        session = self.create()
        before = session.token_hash
        response = self.client.post(
            f"/admin/studies/{self.study.pk}/",
            {"action": "send_invitation", "session": session.pk, "email": "invalid"},
        )
        self.assertEqual(response.status_code, 400)
        session.refresh_from_db()
        self.assertEqual(session.token_hash, before)
        self.assertEqual(InvitationEmail.objects.count(), 1)

    @override_settings(CASE_EMAIL_ENABLED=False)
    def test_disabled_mail_rolls_back_creation_with_email(self):
        response = self.client.post(
            f"/admin/studies/{self.study.pk}/",
            {
                "action": "create",
                "coder": "Coder",
                "email": "coder@example.org",
                "cases": [self.alpha.pk],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(CodingSession.objects.count(), 0)
        self.assertEqual(InvitationEmail.objects.count(), 0)

    def test_creation_without_email_keeps_manual_link_workflow(self):
        self.create(email="")
        self.assertEqual(InvitationEmail.objects.count(), 0)
