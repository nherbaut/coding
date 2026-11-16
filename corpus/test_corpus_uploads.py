import datetime
from unittest.mock import patch
from django.test import TestCase, Client, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from . import models as m
from .test_reporting import report_fixture
from .test_support import new_study
from .corpus_uploads import preview_upload, commit_upload, assign_upload
from .services import Conflict, create_session
from .github_sync import (
    import_study,
    synchronize_next,
    claim_binding,
    prepare_run,
    publish_run,
)
from .test_github_sync import fixture_files, file_tree, FakeGit
from .github_repository import replay_codebook, inspect_repository


def upload(name="new.md", text="A😀 policy\r\nSecond line\r\n"):
    return SimpleUploadedFile(name, text.encode("utf-8"))


@override_settings(CASE_EMAIL_ENABLED=False)
class CorpusUploadTests(TestCase):
    def setUp(self):
        self.d = report_fixture()
        self.study, self.owner = self.d["study"], self.d["admin"]
        self.client.force_login(self.owner)

    def preview(self, **kwargs):
        return preview_upload(
            self.study, self.owner, [upload()], str(self.d["case"].pk), **kwargs
        )

    def commit(self, batch):
        return commit_upload(
            batch.pk,
            self.study.pk,
            self.owner,
            [
                dict(url="https://example.org/source", notes="Evidence")
                for f in batch.data["files"]
            ],
        )

    def test_preview_atomic_append_unicode_provenance_and_idempotency(self):
        before = m.Document.objects.filter(study=self.study).count()
        snapshot = self.d["submissions"][0].snapshot
        batch = self.preview()
        self.assertEqual(m.Document.objects.filter(study=self.study).count(), before)
        result = self.commit(batch)
        version = m.DocumentVersion.objects.get(pk=result.data["files"][0]["version"])
        self.assertEqual(version.text.encode(), upload().read())
        self.assertEqual(version.metadata["url"], "https://example.org/source")
        self.assertNotIn("text", result.data["files"][0])
        self.commit(batch)
        self.assertEqual(
            m.Document.objects.filter(study=self.study).count(), before + 1
        )
        for a in self.d["assignments"]:
            self.assertEqual(a.documents.count(), 1)
        self.d["submissions"][0].refresh_from_db()
        self.assertEqual(self.d["submissions"][0].snapshot, snapshot)

    def test_name_encoding_limits_and_coding_directory_collisions(self):
        for files in [
            [upload("new.md"), upload("new.md")],
            [upload("policy.md"), upload("policy.txt")],
            [SimpleUploadedFile("bad.txt", b"\xff")],
            [upload("binary.txt", "x\x00y")],
            [upload("web.html")],
        ]:
            with self.assertRaises(ValueError):
                preview_upload(self.study, self.owner, files, str(self.d["case"].pk))
        with self.assertRaises(ValueError):
            preview_upload(
                self.study,
                self.owner,
                [upload("huge.txt", "x" * (20 * 1024 * 1024 + 1))],
                str(self.d["case"].pk),
            )
        batch = self.preview()
        self.commit(batch)
        with self.assertRaises(ValueError):
            self.preview()

    def test_new_case_validation_expiry_rollback_and_cancel(self):
        batch = preview_upload(self.study, self.owner, [upload()], new_case="New case")
        with patch(
            "corpus.corpus_uploads.put_document", side_effect=ValueError("Failure")
        ):
            with self.assertRaises(ValueError):
                self.commit(batch)
        self.assertFalse(self.study.cases.filter(name="New case").exists())
        m.CorpusUploadBatch.objects.filter(pk=batch.pk).update(
            created=timezone.now() - datetime.timedelta(hours=2)
        )
        with self.assertRaisesMessage(ValueError, "expired"):
            self.commit(batch)
        self.client.post(
            reverse("corpus-upload-batch", args=[self.study.pk, batch.pk]),
            {"action": "cancel"},
        )
        self.assertFalse(m.CorpusUploadBatch.objects.filter(pk=batch.pk).exists())

    def test_explicit_assignment_open_only_revision_and_isolation(self):
        from .services import reopen_assignment

        batch = self.commit(self.preview())
        before = list(
            self.d["alice"].assignment_set.get().documents.values_list("pk", flat=True)
        )
        with self.assertRaises(Conflict):
            assign_upload(
                batch.pk, self.study.pk, self.owner, [str(self.d["alice"].pk)]
            )
        assignment = reopen_assignment(self.d["assignments"][0].pk)
        old_revision = assignment.revision
        self.assertEqual(
            assign_upload(
                batch.pk, self.study.pk, self.owner, [str(self.d["alice"].pk)]
            ),
            1,
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.revision, old_revision + 1)
        self.assertTrue(
            set(before).issubset(assignment.documents.values_list("pk", flat=True))
        )
        self.assertEqual(assignment.documents.count(), 2)
        self.assertEqual(self.d["assignments"][1].documents.count(), 1)
        self.assertEqual(
            assign_upload(
                batch.pk, self.study.pk, self.owner, [str(self.d["alice"].pk)]
            ),
            0,
        )
        self.assertEqual(m.Annotation.objects.count(), 4)

    def test_admin_permissions_draft_ownership_and_csrf(self):
        other = new_study()
        url = reverse("corpus-upload", args=[self.study.pk])
        self.client.force_login(other.owner)
        self.assertEqual(self.client.get(url).status_code, 404)
        m.StudyAdmin.objects.create(
            study=self.study, user=other.owner, granted_by=self.owner
        )
        self.assertEqual(self.client.get(url).status_code, 200)
        batch = self.preview()
        detail = reverse("corpus-upload-batch", args=[self.study.pk, batch.pk])
        self.assertEqual(self.client.get(detail).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.owner)
        self.assertEqual(
            secure.post(url, {"action": "preview", "files": upload()}).status_code, 403
        )
        self.assertEqual(m.Document.objects.filter(study=self.study).count(), 1)

    def test_stale_previews_and_invalid_metadata_do_not_add_partial_data(self):
        first, second = self.preview(), self.preview()
        self.commit(first)
        before = self.study.documents.count()
        with self.assertRaises(ValueError):
            self.commit(second)
        self.assertEqual(self.study.documents.count(), before)
        fresh = preview_upload(
            self.study, self.owner, [upload("another.md")], str(self.d["case"].pk)
        )
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            commit_upload(
                fresh.pk,
                self.study.pk,
                self.owner,
                [dict(url="javascript:alert(1)", notes="")],
            )
        self.assertEqual(self.study.documents.count(), before)

    def test_service_rechecks_revoked_admin_before_import(self):
        other = new_study().owner
        membership = m.StudyAdmin.objects.create(
            study=self.study, user=other, granted_by=self.owner
        )
        batch = preview_upload(self.study, other, [upload()], str(self.d["case"].pk))
        membership.delete()
        from django.http import Http404

        with self.assertRaises(Http404):
            commit_upload(batch.pk, self.study.pk, other, [dict(url="", notes="")])


@override_settings(CASE_EMAIL_ENABLED=False)
class GitHubCorpusUploadTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.owner = get_user_model().objects.create_user("owner", is_staff=True)
        self.files, _ = fixture_files()
        self.fake = FakeGit(self.files)
        self.study = import_study(
            self.owner,
            dict(id=99, full_name="research/data"),
            "main",
            7,
            self.fake.head("", ""),
            self.files,
            file_tree(self.files),
            "Research",
        )
        self.sync()

    def sync(self):
        synchronize_next(force=True, client_factory=lambda *_: self.fake)

    def add(self, name="added.md"):
        case = self.study.cases.get(key="Alpha")
        batch = preview_upload(self.study, self.owner, [upload(name)], str(case.pk))
        return commit_upload(
            batch.pk, self.study.pk, self.owner, [dict(url="", notes="new")]
        )

    def test_append_export_and_lost_response_recovery_preserve_history(self):
        self.add()
        self.fake.lose_response = True
        self.sync()
        binding = self.study.github
        binding.refresh_from_db()
        self.assertFalse(binding.blocked)
        self.assertEqual(binding.runs.filter(status="retrying").count(), 1)
        m.GitHubStudy.objects.filter(pk=binding.pk).update(
            next_attempt_at=timezone.now()
        )
        commits = len(self.fake.commits)
        self.sync()
        binding.refresh_from_db()
        self.assertFalse(binding.last_error)
        self.assertEqual(len(self.fake.commits), commits)
        self.assertIn("corpus/Alpha/added.md", binding.corpus_blobs)
        for path, text in self.files.items():
            if path.startswith("corpus/"):
                self.assertEqual(self.fake.files[path], text)
        self.assertEqual(
            self.fake.files["corpus/Alpha/added.md"], upload().read().decode()
        )
        replay_codebook(self.fake.files)
        self.assertEqual(len(inspect_repository(self.fake.files, 99)["documents"]), 3)

    def test_external_edits_block_and_existing_models_remain_frozen(self):
        self.add()
        self.fake.files["corpus/Alpha/policy.md"] = "External replacement"
        self.sync()
        binding = self.study.github
        binding.refresh_from_db()
        self.assertTrue(binding.blocked)
        self.assertNotIn("corpus/Alpha/added.md", self.fake.files)
        with self.assertRaisesMessage(ValueError, "frozen"):
            self.study.cases.create(key="no", name="Not approved")

    def test_metadata_only_durable_snapshot_keeps_published_corpus_baseline(self):
        from .github_sync import record_event

        record_event(self.study.pk, "instructions")
        binding = claim_binding(force=True)
        run = prepare_run(binding)
        run.files = {
            p: value
            for p, value in run.files.items()
            if not p.startswith("corpus/") or ".coding/" in p
        }
        run.save(update_fields=["files"])
        original = dict(binding.corpus_blobs)
        m.GitHubStudy.objects.filter(pk=binding.pk).update(
            lease_token=None, locked_until=None
        )
        self.sync()
        binding.refresh_from_db()
        self.assertEqual(binding.corpus_blobs, original)
        self.assertFalse(binding.blocked)

    def test_new_upload_during_prepared_run_is_published_next(self):
        first = self.add("first.md")
        binding = claim_binding(force=True)
        run = prepare_run(binding)
        self.add("second.md")
        from .github_sync import LeaseLost

        # Publish the older durable snapshot through the regular recovery path.
        m.GitHubStudy.objects.filter(pk=binding.pk).update(
            lease_token=None, locked_until=None
        )
        self.sync()
        self.assertIn("corpus/Alpha/first.md", self.fake.files)
        self.assertNotIn("corpus/Alpha/second.md", self.fake.files)
        self.sync()
        self.assertIn("corpus/Alpha/second.md", self.fake.files)
        self.assertEqual(self.study.github.events.filter(published=False).count(), 0)
