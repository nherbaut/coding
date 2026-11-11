from copy import deepcopy
import datetime
import io
import time
import zipfile
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .github_repository import dump, blob_sha, replay_codebook
from .github_uploads import uploaded_repository
from .github_views import cipher
from .github_sync import import_study, synchronize_next, record_event
from .github_client import GitHubClient, GitHubError
from .models import GitHubImportDraft, GitHubStudy, Study, GitHubRun
from .test_github_sync import fixture_files, FakeGit, file_tree


def corpus_zip(*, manifest=False):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        if manifest:
            archive.writestr(
                "manifest.yaml",
                dump(
                    dict(
                        version=1,
                        cases=[
                            dict(
                                id="project",
                                name="Research project",
                                metadata={"url": "https://example.org"},
                            )
                        ],
                        documents=[
                            dict(
                                id=23,
                                path="files/policy.md",
                                name="Policy.md",
                                metadata={"source": "original"},
                                cases=["project"],
                            )
                        ],
                    )
                ),
            )
            archive.writestr("files/policy.md", "A😀 review\r\n")
        else:
            archive.writestr("corpus/Alpha/policy.md", "A😀 review\r\n")
    return data.getvalue()


def uploads(manifest=False):
    source, _ = fixture_files()
    return dict(
        corpus=SimpleUploadedFile(
            "corpus.zip", corpus_zip(manifest=manifest), content_type="application/zip"
        ),
        codebook=SimpleUploadedFile(
            "book.yaml",
            source["codebook/codebook.yaml"].encode(),
            content_type="application/yaml",
        ),
    )


class BootstrapGit(FakeGit):
    def __init__(self, empty=False):
        super().__init__({} if empty else {"README.md": "Original README\n"})
        if empty:
            self.sha = ""
        self.seed_calls = 0
        self.lose_seed_response = False

    def initial_head(self, repo, branch):
        return dict(sha=self.sha, tree="tree" if self.sha else "")

    def seed_repository(self, repo, branch, path, content, message):
        if self.sha:
            raise GitHubError("Repository is no longer empty", conflict=True)
        self.files[path] = content
        self.sha = "c" * 40
        self.seed_calls += 1
        if self.lose_seed_response:
            self.lose_seed_response = False
            raise GitHubError("Seed response was lost")
        return self.sha


@override_settings(CASE_EMAIL_ENABLED=False)
class BootstrapTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            "bootstrap-owner", is_staff=True
        )
        self.files = uploaded_repository(**uploads())

    def binding(self, fake):
        return import_study(
            self.owner,
            dict(id=99, full_name="research/new"),
            "main",
            7,
            fake.initial_head("", ""),
            self.files,
            {},
            "Uploaded study",
            bootstrap=True,
        ).github

    def sync(self, fake):
        return synchronize_next(force=True, client_factory=lambda *_: fake)

    def test_readme_repository_is_preserved_and_initialized_in_one_commit(self):
        fake = BootstrapGit()
        binding = self.binding(fake)
        self.assertTrue(binding.bootstrap_pending)
        self.sync(fake)
        binding.refresh_from_db()
        self.assertFalse(binding.bootstrap_pending)
        self.assertEqual(fake.files["README.md"], "Original README\n")
        self.assertEqual(fake.seed_calls, 0)
        self.assertEqual(len(fake.commits), 1)
        self.assertEqual(fake.files["corpus/Alpha/policy.md"], "A😀 review\r\n")
        self.assertEqual(replay_codebook(fake.files)[0], replay_codebook(fake.files)[1])
        record_event(binding.study_id, "unchanged")
        self.sync(fake)
        self.assertEqual(len(fake.commits), 1)

    def test_empty_repository_seeds_base_then_publishes_complete_data(self):
        fake = BootstrapGit(empty=True)
        binding = self.binding(fake)
        self.sync(fake)
        binding.refresh_from_db()
        self.assertEqual(fake.seed_calls, 1)
        self.assertEqual(len(fake.commits), 1)
        self.assertFalse(binding.bootstrap_pending)
        self.assertTrue(binding.last_commit)
        self.assertIn("corpus/Alpha/policy.md", fake.files)
        self.assertIn("codebook/history.yaml", fake.files)

    def test_seed_lost_response_is_recovered_without_overwrite_or_reseeding(self):
        fake = BootstrapGit(empty=True)
        fake.lose_seed_response = True
        binding = self.binding(fake)
        self.sync(fake)
        binding.refresh_from_db()
        self.assertTrue(binding.bootstrap_pending)
        self.assertEqual(fake.seed_calls, 1)
        binding.next_attempt_at = timezone.now()
        binding.save(update_fields=["next_attempt_at"])
        self.sync(fake)
        binding.refresh_from_db()
        self.assertFalse(binding.bootstrap_pending)
        self.assertEqual(fake.seed_calls, 1)
        self.assertEqual(len(fake.commits), 1)

    def test_final_commit_lost_response_is_recovered(self):
        fake = BootstrapGit()
        fake.lose_response = True
        binding = self.binding(fake)
        self.sync(fake)
        binding.refresh_from_db()
        self.assertTrue(binding.bootstrap_pending)
        binding.next_attempt_at = timezone.now()
        binding.save(update_fields=["next_attempt_at"])
        self.sync(fake)
        binding.refresh_from_db()
        self.assertFalse(binding.bootstrap_pending)
        self.assertEqual(len(fake.commits), 1)

    def test_concurrent_data_is_never_overwritten(self):
        fake = BootstrapGit()
        binding = self.binding(fake)
        fake.files["corpus/Alpha/policy.md"] = "Someone else wrote this"
        self.sync(fake)
        binding.refresh_from_db()
        self.assertTrue(binding.blocked)
        self.assertEqual(
            fake.files["corpus/Alpha/policy.md"], "Someone else wrote this"
        )
        self.assertEqual(len(fake.commits), 0)

    def test_manifest_upload_preserves_metadata_and_portable_references(self):
        data = uploads(manifest=True)
        files = uploaded_repository(**data)
        self.assertIn("corpus/project/policy.md", files)
        from .github_repository import inspect_repository

        parsed = inspect_repository(files)
        self.assertEqual(parsed["cases"][0]["name"], "Research project")
        self.assertEqual(parsed["documents"][0]["metadata"], {"source": "original"})

    def test_manifest_document_ids_in_codebook_references_are_resolved(self):
        import hashlib
        from .github_repository import load_yaml

        data = uploads(manifest=True)
        book = load_yaml(data["codebook"].read().decode())
        book["codes"][0]["references"] = [
            dict(
                type="corpus",
                document=23,
                hash=hashlib.sha256("A😀 review\r\n".encode()).hexdigest(),
                start=1,
                end=3,
                quote="😀",
            )
        ]
        data["codebook"] = SimpleUploadedFile("book.yaml", dump(book).encode())
        files = uploaded_repository(**data)
        self.assertEqual(
            load_yaml(files["codebook/codebook.yaml"])["codes"][0]["references"][0][
                "document"
            ],
            "corpus/project/policy.md",
        )

    def test_invalid_zip_yaml_and_collision_fail_before_creating_a_study(self):
        bad = uploads()
        bad["corpus"] = SimpleUploadedFile("bad.zip", b"not a ZIP")
        with self.assertRaises(ValueError):
            uploaded_repository(**bad)
        bad = uploads()
        bad["codebook"] = SimpleUploadedFile("bad.yaml", b"codes: [invalid")
        with self.assertRaises(ValueError):
            uploaded_repository(**bad)
        self.assertFalse(Study.objects.exists())


@override_settings(
    GITHUB_APP_ID="1",
    GITHUB_CLIENT_ID="client",
    GITHUB_CLIENT_SECRET="secret",
    GITHUB_APP_SLUG="app",
    GITHUB_PRIVATE_KEY_FILE="/protected/app.pem",
)
class UploadPreviewTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", is_staff=True)
        self.client.force_login(self.owner)
        session = self.client.session
        session["github_authorization"] = dict(
            admin=str(self.owner.pk),
            expires=time.time() + 3600,
            login="github-owner",
            token=cipher().encrypt(b"fake-token").decode(),
        )
        session.save()
        self.fake = BootstrapGit(empty=True)
        self.repo = dict(id=99, full_name="research/new", permissions={"push": True})

    def preview(self):
        return self.client.post(
            reverse("github-create"),
            dict(
                action="preview",
                source="uploads",
                repository="99:7",
                branch="main",
                title="New upload",
                **uploads()
            ),
        )

    def test_upload_preview_and_finalize_are_scoped_and_repeat_safe(self):
        with patch.object(
            GitHubClient, "repository_access", return_value=self.repo
        ), patch("corpus.github_views.installation_client", return_value=self.fake):
            response = self.preview()
            self.assertContains(response, "Empty repository")
            self.assertFalse(Study.objects.exists())
            choice = self.client.session["github_preview"]
            self.assertNotIn("files", choice)
            self.assertEqual(GitHubImportDraft.objects.get().owner, self.owner)
            response = self.client.post(
                reverse("github-create"), dict(action="create", nonce=choice["nonce"])
            )
            self.assertEqual(response.status_code, 302)
            self.assertTrue(GitHubStudy.objects.get().bootstrap_pending)
            self.assertFalse(GitHubImportDraft.objects.exists())
            self.assertEqual(
                self.client.post(
                    reverse("github-create"),
                    dict(action="create", nonce=choice["nonce"]),
                ).status_code,
                400,
            )
            self.assertEqual(Study.objects.count(), 1)

    def test_expired_or_other_owners_draft_cannot_initialize_repository(self):
        with patch.object(
            GitHubClient, "repository_access", return_value=self.repo
        ), patch("corpus.github_views.installation_client", return_value=self.fake):
            self.preview()
            choice = self.client.session["github_preview"]
            GitHubImportDraft.objects.update(
                created=timezone.now() - datetime.timedelta(hours=2)
            )
            self.assertEqual(
                self.client.post(
                    reverse("github-create"),
                    dict(action="create", nonce=choice["nonce"]),
                ).status_code,
                400,
            )
            self.assertFalse(Study.objects.exists())

    def test_other_admin_upload_preview_cannot_be_used(self):
        other = get_user_model().objects.create_user("another-owner", is_staff=True)
        with patch.object(
            GitHubClient, "repository_access", return_value=self.repo
        ), patch("corpus.github_views.installation_client", return_value=self.fake):
            self.preview()
            choice = self.client.session["github_preview"]
            GitHubImportDraft.objects.update(owner=other)
            self.assertEqual(
                self.client.post(
                    reverse("github-create"),
                    dict(action="create", nonce=choice["nonce"]),
                ).status_code,
                400,
            )
            self.assertFalse(Study.objects.exists())

    def test_repository_data_cannot_be_replaced_by_upload(self):
        self.fake.files = {"codebook/codebook.yaml": "version: 1\ncodes: []\n"}
        self.fake.sha = "a" * 40
        with patch.object(
            GitHubClient, "repository_access", return_value=self.repo
        ), patch("corpus.github_views.installation_client", return_value=self.fake):
            self.assertEqual(self.preview().status_code, 400)
            self.assertFalse(GitHubImportDraft.objects.exists())


class EmptyRepositoryApiTests(TestCase):
    def test_empty_repository_detection_does_not_mask_missing_branch_or_permissions(
        self,
    ):
        client = GitHubClient("token")
        with patch.object(
            client, "head", side_effect=GitHubError("empty", status=409)
        ), patch.object(client, "api", side_effect=[[], {"default_branch": "main"}]):
            self.assertEqual(
                client.initial_head("owner/repo", "main"), {"sha": "", "tree": ""}
            )
        with patch.object(
            client, "head", side_effect=GitHubError("missing branch", status=404)
        ), patch.object(client, "api", return_value=[{"name": "main"}]):
            with self.assertRaisesMessage(GitHubError, "missing branch"):
                client.initial_head("owner/repo", "other")
        with patch.object(
            client, "head", side_effect=GitHubError("denied", status=403)
        ), patch.object(client, "api") as api:
            with self.assertRaises(GitHubError):
                client.initial_head("owner/repo", "main")
            api.assert_not_called()

    def test_first_commit_creates_base_without_sha_and_uses_default_branch(self):
        import base64

        client = GitHubClient("token")
        with patch.object(
            client,
            "api",
            side_effect=[{"default_branch": "main"}, {"commit": {"sha": "first"}}],
        ) as api:
            self.assertEqual(
                client.seed_repository(
                    "owner/repo",
                    "main",
                    "codebook/codebook-base.yaml",
                    "version: 1\n",
                    "Initialize",
                ),
                "first",
            )
        path, method, body = api.call_args.args
        self.assertEqual(method, "PUT")
        self.assertNotIn("sha", body)
        self.assertNotIn("branch", body)
        self.assertEqual(base64.b64decode(body["content"]), b"version: 1\n")
