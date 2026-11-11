from copy import deepcopy
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from django.urls import reverse
from .test_reporting import report_fixture
from .models import Assignment, CodingSession, InvitationEmail, Case
from .services import create_session
from .studies import publish_codebook
from .importers import put_document
from .invitations import invitation_cipher, queue_invitation_email


@override_settings(
    CASE_EMAIL_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class DuplicateRequestTests(TestCase):
    def setUp(self):
        self.data = report_fixture()
        self.study = self.data["study"]
        self.source = self.data["alice"]
        self.client.force_login(self.data["admin"])
        self.url = reverse("dashboard", args=[self.study.pk])

    def duplicate(self, **updates):
        body = dict(
            action="duplicate",
            session=str(self.source.pk),
            coder="Carol",
            email="carol@example.org",
        )
        body.update(updates)
        return self.client.post(self.url, body)

    def test_duplicate_sends_new_request_with_pinned_texts_and_empty_coding(self):
        self.source.email = "alice@example.org"
        self.source.save(update_fields=["email"])
        original_job = queue_invitation_email(self.source, self.data["link"])
        source_token = self.source.token_hash
        personal = deepcopy(self.source.codebook)
        personal["codes"][1]["name"] = "Personal proposal"
        self.source.codebook = personal
        self.source.save(update_fields=["codebook"])
        newer = put_document(
            self.study,
            "policy",
            "AI_POLICY.md",
            "Changed text",
            {},
            [self.data["case"]],
        )
        response = self.duplicate()
        self.assertRedirects(response, self.url)
        target = CodingSession.objects.get(coder="Carol")
        original = self.source.assignment_set.get()
        copied = target.assignment_set.get()
        self.assertTrue(original.locked)
        self.assertFalse(copied.locked)
        self.assertEqual(list(original.documents.all()), list(copied.documents.all()))
        self.assertNotIn(newer, list(copied.documents.all()))
        self.assertFalse(copied.annotation_set.exists())
        self.assertFalse(copied.submission_set.exists())
        self.study.refresh_from_db()
        self.assertEqual(target.base_id, self.study.codebook_id)
        self.assertEqual(target.codebook, self.study.codebook.content)
        self.assertEqual(target.instructions_id, self.study.instructions_id)
        self.assertNotEqual(target.token_hash, source_token)
        self.source.refresh_from_db()
        original_job.refresh_from_db()
        self.assertEqual(self.source.token_hash, source_token)
        self.assertIsNone(original_job.cancelled_at)
        self.assertEqual(original.annotation_set.count(), 2)
        job = InvitationEmail.objects.get(session=target)
        self.assertEqual(job.recipients, ["carol@example.org"])
        body = invitation_cipher().decrypt(job.body.encode()).decode()
        self.assertIn(self.study.title, body)
        self.assertIn("Research project", body)
        self.assertIn("Hello Carol", body)
        self.assertNotIn(self.data["link"], body)

    def test_removed_assignments_are_not_copied(self):
        removed = Case.objects.create(study=self.study, key="removed", name="Removed")
        Assignment.objects.create(
            session=self.source, case=removed, removed_at=timezone.now()
        )
        self.assertEqual(self.duplicate().status_code, 302)
        self.assertEqual(
            CodingSession.objects.get(coder="Carol").assignment_set.count(), 1
        )

    def test_foreign_deleted_or_admin_requests_cannot_be_duplicated(self):
        from .test_support import new_study

        foreign_study = new_study()
        foreign, _ = create_session(foreign_study, "Hidden", [])
        count = CodingSession.objects.count()
        self.assertEqual(self.duplicate(session=str(foreign.pk)).status_code, 404)
        self.source.deleted_at = timezone.now()
        self.source.save(update_fields=["deleted_at"])
        self.assertEqual(self.duplicate().status_code, 404)
        self.source.deleted_at = None
        self.source.admin_user = self.data["admin"]
        self.source.save(update_fields=["deleted_at", "admin_user"])
        self.assertEqual(self.duplicate().status_code, 404)
        self.assertEqual(CodingSession.objects.count(), count)

    def test_invalid_recipient_and_disabled_email_roll_back_new_session(self):
        count = CodingSession.objects.count()
        for fields in [dict(email=""), dict(email="invalid"), dict(coder="")]:
            self.assertEqual(self.duplicate(**fields).status_code, 400)
            self.assertEqual(CodingSession.objects.count(), count)
        with override_settings(CASE_EMAIL_ENABLED=False):
            self.assertEqual(self.duplicate().status_code, 400)
        self.assertEqual(CodingSession.objects.count(), count)
        self.assertFalse(InvitationEmail.objects.exists())


class ReadOnlyCodebookTests(TestCase):
    def setUp(self):
        self.data = report_fixture()
        self.study = self.data["study"]
        self.session = self.data["alice"]
        self.view = "/work/codebook/"
        self.state = self.view + "state/"

    def enter(self):
        return self.client.post(
            "/access/exchange/",
            dict(token=self.data["link"].split("#")[1]),
            content_type="application/json",
        )

    def test_private_view_requires_link_and_exposes_only_personal_book(self):
        self.assertEqual(self.client.get(self.view).status_code, 404)
        self.assertEqual(self.client.get(self.state).status_code, 404)
        self.enter()
        changed = deepcopy(self.data["book"])
        changed["codes"][1]["name"] = "New shared name"
        self.study.refresh_from_db()
        publish_codebook(self.study, changed, expected_revision=self.study.codebook_id)
        page = self.client.get(self.view)
        self.assertContains(page, "read-only view of your personal codebook")
        self.assertNotContains(page, "data-publish=")
        self.assertIn("no-store", page["Cache-Control"])
        state = self.client.get(self.state).json()
        self.assertEqual(state["codebook"], self.session.codebook)
        self.assertNotIn("assignments", state)
        self.assertEqual(self.client.post(self.state, {}).status_code, 405)
        self.assertEqual(self.client.post(self.view, {}).status_code, 405)
        self.session.refresh_from_db()
        self.assertEqual(self.session.revision, 0)
        self.assertEqual(
            self.client.get(
                reverse("codebook-state", args=[self.study.pk])
            ).status_code,
            302,
        )

    def test_revocation_stops_read_only_access(self):
        self.enter()
        self.session.token_hash = ""
        self.session.save(update_fields=["token_hash"])
        self.assertEqual(self.client.get(self.state).status_code, 404)

    def test_admin_personal_view_stays_separate_from_private_coder(self):
        self.enter()
        self.client.force_login(self.data["admin"])
        root = reverse("admin-coding", args=[self.study.pk])
        self.client.get(root)
        admin_session = CodingSession.objects.get(admin_user=self.data["admin"])
        personal = deepcopy(admin_session.codebook)
        personal["codes"][1]["name"] = "Admin personal"
        admin_session.codebook = personal
        admin_session.save(update_fields=["codebook"])
        view = reverse("admin-personal-codebook", args=[self.study.pk])
        self.assertEqual(self.client.get(view).status_code, 200)
        self.assertEqual(self.client.get(view + "state/").json()["codebook"], personal)
        self.assertEqual(
            self.client.get(self.state).json()["codebook"], self.session.codebook
        )
        self.client.logout()
        self.assertEqual(self.client.get(view).status_code, 302)
