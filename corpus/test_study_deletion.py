import datetime
import uuid
from unittest.mock import patch
from django.test import (
    TestCase,
    Client,
    override_settings,
    TransactionTestCase,
    skipUnlessDBFeature,
)
from django.db import connections, transaction
from django.urls import reverse
from django.utils import timezone
from concurrent.futures import ThreadPoolExecutor
from . import models as m
from .test_reporting import report_fixture
from .test_support import new_study
from .study_deletion import delete_study
from .services import Conflict, merge_submission
from .studies import publish_instructions
from .github_sync import import_study
from .test_github_sync import fixture_files, file_tree, FakeGit


@override_settings(CASE_EMAIL_ENABLED=False)
class StudyDeletionTests(TestCase):
    def setUp(self):
        self.data = report_fixture()
        self.study = self.data["study"]
        self.owner = self.data["admin"]
        self.other = new_study()
        self.url = reverse("study-delete", args=[self.study.pk])
        self.client.force_login(self.owner)

    def test_confirmation_page_is_read_only_and_delete_is_owner_only(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Permanently delete study")
        self.assertTrue(m.Study.objects.filter(pk=self.study.pk).exists())
        member = self.other.owner
        m.StudyAdmin.objects.create(
            study=self.study, user=member, granted_by=self.owner
        )
        self.client.force_login(member)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(
            self.client.post(self.url, dict(confirmation=self.study.title)).status_code,
            403,
        )
        settings = self.client.get(reverse("study-settings", args=[self.study.pk]))
        self.assertNotContains(settings, "Delete study…")
        m.StudyAdmin.objects.filter(study=self.study, user=member).delete()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_exact_title_and_csrf_are_required(self):
        for text in ["", self.study.title.lower(), "wrong title"]:
            self.assertEqual(
                self.client.post(self.url, dict(confirmation=text)).status_code, 400
            )
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.owner)
        self.assertEqual(
            secure.post(self.url, dict(confirmation=self.study.title)).status_code, 403
        )
        self.assertTrue(m.Study.objects.filter(pk=self.study.pk).exists())
        self.assertEqual(self.client.put(self.url).status_code, 405)

    def test_complete_graph_is_deleted_without_touching_other_studies_or_users(self):
        m.ResearchQuestion.objects.create(
            study=self.study, label="RQ1", text="Question"
        )
        m.CodingGuideline.objects.create(
            study=self.study, title="Guide", text="Guidelines"
        )
        publish_instructions(self.study, self.owner)
        m.CodingReport.objects.create(
            study=self.study,
            name="Saved",
            created_by=self.owner,
            payload={"records": []},
        )
        m.StudyAdminInvitation.objects.create(
            study=self.study, username="invitee", invited_by=self.owner
        )
        self.study.refresh_from_db()
        merge_submission(
            self.data["submissions"][0].pk, self.owner, self.study.codebook_id, {}
        )
        users = m.Study._meta.get_field("owner").related_model.objects.count()
        other_revision = self.other.codebook_id
        coder = Client()
        coder.post(
            "/access/exchange/",
            dict(token=self.data["link"].split("#")[1]),
            content_type="application/json",
        )
        response = self.client.post(self.url, dict(confirmation=self.study.title))
        self.assertRedirects(response, "/")
        self.assertFalse(m.Study.objects.filter(pk=self.study.pk).exists())
        for model in (
            m.Case,
            m.Document,
            m.SharedCodebook,
            m.StudyProtocol,
            m.CodingSession,
            m.ResearchQuestion,
            m.CodingGuideline,
            m.CodingReport,
            m.StudyAdminInvitation,
        ):
            self.assertFalse(
                model.objects.filter(study_id=self.study.pk).exists(), model.__name__
            )
        for model in (
            m.Assignment,
            m.Annotation,
            m.Submission,
            m.SubmissionWithdrawal,
            m.MergeAudit,
            m.DocumentVersion,
        ):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertEqual(
            m.Study._meta.get_field("owner").related_model.objects.count(), users
        )
        self.assertEqual(
            m.Study.objects.get(pk=self.other.pk).codebook_id, other_revision
        )
        self.assertEqual(coder.get("/api/state/").status_code, 404)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_active_email_delivery_refuses_deletion_without_partial_changes(self):
        job = m.InvitationEmail.objects.create(
            session=self.data["alice"],
            token_hash=self.data["alice"].token_hash,
            subject="Invitation",
            body="encrypted body",
            sender="sender@example.org",
            recipients=["coder@example.org"],
            message_id="test",
            lease_token=uuid.uuid4(),
            locked_until=timezone.now() + datetime.timedelta(minutes=1),
        )
        self.assertEqual(
            self.client.post(self.url, dict(confirmation=self.study.title)).status_code,
            409,
        )
        self.assertTrue(m.Study.objects.filter(pk=self.study.pk).exists())
        self.assertEqual(m.Annotation.objects.count(), 4)
        job.locked_until = timezone.now() - datetime.timedelta(seconds=1)
        job.save(update_fields=["locked_until"])
        self.assertEqual(
            self.client.post(self.url, dict(confirmation=self.study.title)).status_code,
            302,
        )
        self.assertFalse(m.InvitationEmail.objects.filter(pk=job.pk).exists())

    def test_transaction_rolls_back_the_whole_delete_on_failure(self):
        with patch.object(m.DocumentVersion.objects, "filter") as query:
            query.return_value.delete.side_effect = RuntimeError(
                "database interruption"
            )
            with self.assertRaises(RuntimeError):
                delete_study(self.study.pk, self.owner, self.study.title)
        self.assertTrue(m.Study.objects.filter(pk=self.study.pk).exists())
        self.assertEqual(m.Annotation.objects.count(), 4)
        self.assertEqual(m.Submission.objects.count(), 2)

    def test_github_sync_work_is_cancelled_locally_and_never_contacts_github(self):
        files, _ = fixture_files()
        fake = FakeGit(files)
        study = import_study(
            self.owner,
            dict(id=99, full_name="research/data"),
            "main",
            7,
            fake.head("", ""),
            files,
            file_tree(files),
            "GitHub study",
        )
        binding = study.github
        m.GitHubRun.objects.create(
            binding=binding,
            target_event=binding.events.get().pk,
            files=files,
            event_ids=[binding.events.get().pk],
        )
        binding.lease_token = uuid.uuid4()
        binding.locked_until = timezone.now() + datetime.timedelta(minutes=1)
        binding.save(update_fields=["lease_token", "locked_until"])
        url = reverse("study-delete", args=[study.pk])
        self.assertEqual(
            self.client.post(url, dict(confirmation=study.title)).status_code, 409
        )
        binding.locked_until = timezone.now() - datetime.timedelta(seconds=1)
        binding.save(update_fields=["locked_until"])
        with patch("corpus.github_client.http") as api:
            self.assertEqual(
                self.client.post(url, dict(confirmation=study.title)).status_code, 302
            )
            api.assert_not_called()
        self.assertFalse(m.GitHubStudy.objects.filter(pk=binding.pk).exists())
        self.assertEqual(m.GitHubEvent.objects.count(), 0)
        self.assertEqual(m.GitHubRun.objects.count(), 0)
        self.assertTrue(m.Study.objects.filter(pk=self.study.pk).exists())


class StudyDeletionConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update_nowait")
    def test_coding_lock_returns_retry_conflict_instead_of_deadlock(self):
        data = report_fixture()
        study = data["study"]
        owner = data["admin"]

        def attempt():
            try:
                try:
                    delete_study(study.pk, owner, study.title)
                except Conflict:
                    return "conflict"
                return "deleted"
            finally:
                connections.close_all()

        with transaction.atomic():
            m.CodingSession.objects.select_for_update().get(pk=data["alice"].pk)
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(attempt).result(timeout=10), "conflict")
        self.assertTrue(m.Study.objects.filter(pk=study.pk).exists())
        delete_study(study.pk, owner, study.title)
        self.assertFalse(m.Study.objects.filter(pk=study.pk).exists())
