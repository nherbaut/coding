import time
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from .github_views import cipher
from .models import GitHubStudy, Study
from .test_github_sync import fixture_files, FakeGit
from .github_client import GitHubClient, GitHubError


@override_settings(
    GITHUB_APP_ID="1",
    GITHUB_CLIENT_ID="test-client",
    GITHUB_CLIENT_SECRET="test-secret",
    GITHUB_PRIVATE_KEY_FILE="/protected/key.pem",
    GITHUB_APP_SLUG="coding-test",
)
class GitHubViewsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", is_staff=True)
        self.client.force_login(self.owner)
        self.files, _ = fixture_files()
        self.fake = FakeGit(self.files)
        self.repo = dict(id=99, full_name="research/data", permissions={"push": True})

    def authorize(self):
        session = self.client.session
        session["github_authorization"] = dict(
            admin=str(self.owner.pk),
            expires=time.time() + 3600,
            login="github-owner",
            token=cipher().encrypt(b"never-print-this-token").decode(),
        )
        session.save()

    def test_oauth_state_is_bound_to_current_admin_and_one_time(self):
        self.assertEqual(self.client.get(reverse("github-connect")).status_code, 405)
        start = self.client.post(reverse("github-connect"))
        self.assertEqual(start.status_code, 302)
        self.assertIn("https://github.com/login/oauth/authorize?", start.url)
        state = self.client.session["github_oauth_state"]["state"]
        with patch("corpus.github_views.http") as exchange:
            response = self.client.get(
                reverse("github-callback"), dict(state="wrong", code="secret-code")
            )
            self.assertEqual(response.status_code, 302)
            exchange.assert_not_called()
        self.client.post(reverse("github-connect"))
        state = self.client.session["github_oauth_state"]["state"]
        with patch(
            "corpus.github_views.http",
            return_value=dict(access_token="private-user-token", expires_in=3600),
        ), patch.object(GitHubClient, "api", return_value={"login": "github-owner"}):
            self.client.get(
                reverse("github-callback"), dict(state=state, code="secret-code")
            )
        stored = self.client.session["github_authorization"]
        self.assertNotIn("private-user-token", stored["token"])
        self.assertEqual(
            cipher().decrypt(stored["token"].encode()), b"private-user-token"
        )
        with patch("corpus.github_views.http") as exchange:
            self.client.get(
                reverse("github-callback"), dict(state=state, code="replayed-code")
            )
            exchange.assert_not_called()

    def test_cross_admin_and_expired_authorization_is_rejected(self):
        self.client.post(reverse("github-connect"))
        state = self.client.session["github_oauth_state"]["state"]
        other = get_user_model().objects.create_user("other", is_staff=True)
        self.client.force_login(other)
        with patch("corpus.github_views.http") as exchange:
            self.client.get(reverse("github-callback"), dict(state=state, code="code"))
            exchange.assert_not_called()
        self.client.force_login(self.owner)
        self.authorize()
        session = self.client.session
        session["github_authorization"]["expires"] = 0
        session.save()
        self.assertContains(
            self.client.get(reverse("github-create")), "Connect GitHub account"
        )

    def test_preview_create_import_and_reject_repeated_creation(self):
        self.authorize()
        with patch.object(
            GitHubClient, "repository_access", return_value=self.repo
        ), patch("corpus.github_views.installation_client", return_value=self.fake):
            response = self.client.post(
                reverse("github-create"),
                dict(
                    action="preview",
                    repository="99:7",
                    branch="main",
                    title="From GitHub",
                ),
            )
            self.assertContains(response, "Review import")
            self.assertEqual(Study.objects.count(), 0)
            nonce = self.client.session["github_preview"]["nonce"]
            response = self.client.post(
                reverse("github-create"), dict(action="create", nonce=nonce)
            )
            self.assertEqual(response.status_code, 302)
            binding = GitHubStudy.objects.get()
            self.assertRedirects(
                response, reverse("github-status", args=[binding.study_id])
            )
            self.assertEqual(binding.study.owner, self.owner)
            self.assertEqual(
                self.client.post(
                    reverse("github-create"), dict(action="create", nonce=nonce)
                ).status_code,
                400,
            )
            self.assertEqual(Study.objects.count(), 1)

    def test_stale_preview_does_not_create_any_study(self):
        self.authorize()
        with patch.object(
            GitHubClient, "repository_access", return_value=self.repo
        ), patch("corpus.github_views.installation_client", return_value=self.fake):
            self.client.post(
                reverse("github-create"),
                dict(
                    action="preview",
                    repository="99:7",
                    branch="main",
                    title="From GitHub",
                ),
            )
            nonce = self.client.session["github_preview"]["nonce"]
            self.fake.sha = "b" * 40
            self.assertEqual(
                self.client.post(
                    reverse("github-create"), dict(action="create", nonce=nonce)
                ).status_code,
                409,
            )
            self.assertFalse(Study.objects.exists())

    def test_permission_failure_does_not_use_installation_credentials(self):
        self.authorize()
        with patch.object(
            GitHubClient,
            "repository_access",
            side_effect=GitHubError("No repository write access"),
        ), patch("corpus.github_views.installation_client") as installation:
            response = self.client.post(
                reverse("github-create"),
                dict(
                    action="preview",
                    repository="99:7",
                    branch="main",
                    title="Unauthorized",
                ),
            )
            self.assertEqual(response.status_code, 400)
            installation.assert_not_called()
        self.assertFalse(Study.objects.exists())

    def test_unconfigured_app_renders_setup_without_network(self):
        with override_settings(GITHUB_APP_ID=""), patch.object(
            GitHubClient, "api"
        ) as api:
            self.assertContains(
                self.client.get(reverse("github-create")), "GitHub App setup required"
            )
            api.assert_not_called()

    def test_connect_is_csrf_protected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(reverse("github-connect")).status_code, 403)

    def test_owner_reconnects_reinstalled_app_without_changing_repository_or_branch(
        self,
    ):
        from .github_sync import import_study
        from .test_github_sync import file_tree

        study = import_study(
            self.owner,
            self.repo,
            "main",
            7,
            self.fake.head("", ""),
            self.files,
            file_tree(self.files),
            "Bound",
        )
        self.authorize()
        url = reverse("github-status", args=[study.pk])
        with patch.object(
            GitHubClient, "repositories", return_value=[dict(id=99, installation=8)]
        ), patch.object(
            GitHubClient, "repository_access", return_value=self.repo
        ), patch(
            "corpus.github_views.installation_client", return_value=self.fake
        ):
            self.assertEqual(
                self.client.post(url, dict(action="reconnect")).status_code, 302
            )
        study.github.refresh_from_db()
        self.assertEqual(study.github.installation_id, 8)
        self.assertEqual(study.github.branch, "main")
        self.assertEqual(study.github.repository_id, 99)
        self.assertEqual(
            self.client.post(
                reverse("github-connect"), dict(study="invalid")
            ).status_code,
            400,
        )

    def test_failed_import_is_atomic_after_cases_are_created(self):
        from .github_sync import import_study
        from .test_github_sync import file_tree
        from .github_repository import dump

        files = dict(self.files)
        files["codebook/study.yaml"] = dump(
            dict(
                version=1,
                instructions=dict(
                    content=dict(
                        guidelines=[
                            dict(title="Guidance", text="Text", questions=["missing"])
                        ]
                    )
                ),
            )
        )
        with self.assertRaisesMessage(ValueError, "unknown research question"):
            import_study(
                self.owner,
                self.repo,
                "main",
                7,
                self.fake.head("", ""),
                files,
                file_tree(files),
                "Invalid",
            )
        self.assertFalse(Study.objects.exists())
        self.assertFalse(GitHubStudy.objects.exists())
