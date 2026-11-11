import io
import json
import tempfile
from unittest.mock import patch
import urllib.error
import urllib.request
from django.test import SimpleTestCase, override_settings
from .github_client import GitHubClient, GitHubError, http, installation_client


class GitHubClientTests(SimpleTestCase):
    def test_git_commit_uses_single_non_forced_ref_update(self):
        calls = []

        def api(path, method="GET", data=None):
            calls.append((path, method, data))
            return {"sha": "commit-sha"}

        client = GitHubClient("private-token")
        with patch.object(client, "api", side_effect=api):
            result = client.commit_files(
                "owner/repo",
                "feature/data",
                dict(sha="parent", tree="base"),
                {"codebook/codebook.yaml": "version: 1"},
                "Publish",
            )
        self.assertEqual(result, "commit-sha")
        updates = [c for c in calls if c[1] == "PATCH"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][2], {"sha": "commit-sha", "force": False})
        self.assertIn("feature%2Fdata", updates[0][0])
        commit = next(c for c in calls if "/git/commits" in c[0])
        self.assertEqual(commit[2]["parents"], ["parent"])
        tree = next(c for c in calls if "/git/trees" in c[0])
        self.assertEqual(tree[2]["base_tree"], "base")

    def test_wrong_hosts_and_redirects_never_receive_token(self):
        with self.assertRaises(GitHubError):
            http("http://api.github.com/repos/x", "secret")
        with self.assertRaises(GitHubError):
            http("https://attacker.invalid/path", "secret")
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError(
                "https://api.github.com/path", 302, "redirect", {}, None
            )
            with self.assertRaises(GitHubError) as error:
                http("https://api.github.com/path", "secret")
            self.assertNotIn("secret", str(error.exception))

    def test_rate_limit_error_is_redacted_and_respects_retry_after(self):
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError(
                "https://api.github.com/path",
                403,
                "secret response",
                {"Retry-After": "120"},
                io.BytesIO(b"private-token"),
            )
            with self.assertRaises(GitHubError) as error:
                http("https://api.github.com/path", "private-token")
            self.assertEqual(error.exception.retry_after, 120)
            self.assertNotIn("private-token", str(error.exception))

    def test_user_must_have_push_and_installation_repository_access(self):
        client = GitHubClient("token")
        with patch.object(
            client, "api", return_value={"id": 1, "permissions": {"push": False}}
        ):
            with self.assertRaisesMessage(GitHubError, "write access"):
                client.repository_access(1, 2)
        with patch.object(
            client, "api", return_value={"id": 1, "permissions": {"push": True}}
        ), patch.object(client, "pages", return_value=[{"id": 99}]):
            with self.assertRaisesMessage(GitHubError, "installation"):
                client.repository_access(1, 2)

    def test_tree_truncation_and_symlink_are_rejected(self):
        client = GitHubClient("token")
        with patch.object(client, "api", return_value={"truncated": True, "tree": []}):
            with self.assertRaisesMessage(GitHubError, "incomplete"):
                client.tree("owner/repo", "tree")
        with self.assertRaisesMessage(GitHubError, "Symlinks"):
            client.read_files(
                "owner/repo",
                {"corpus/Case/a": dict(type="blob", mode="120000", sha="x")},
            )

    @override_settings(
        GITHUB_APP_ID="1",
        GITHUB_CLIENT_ID="client",
        GITHUB_CLIENT_SECRET="secret",
        GITHUB_APP_SLUG="app",
    )
    def test_installation_token_is_signed_and_limited_to_one_repository(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        import jwt

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with tempfile.NamedTemporaryFile() as file:
            file.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            file.flush()
            with override_settings(GITHUB_PRIVATE_KEY_FILE=file.name), patch.object(
                GitHubClient, "api", return_value={"token": "installation-token"}
            ) as api:
                client = installation_client(7, 99)
            self.assertEqual(client.token, "installation-token")
            self.assertEqual(
                api.call_args.args[2],
                {"repository_ids": [99], "permissions": {"contents": "write"}},
            )

    def test_deterministic_patch_replays_nested_arrays_and_escaped_keys(self):
        from .github_repository import make_patch, digest
        import jsonpatch

        pairs = [
            (
                {"a/b": {"~key": [1, True, {"x": "quote"}]}, "removed": 0},
                {"a/b": {"~key": [False, 1]}, "added": {"memo": "😀"}},
            ),
            (
                {
                    "codes": [
                        {"id": "a", "memo": "quoted"},
                        {"id": "b", "memo": "other"},
                    ]
                },
                {"codes": [{"id": "b", "memo": "other"}]},
            ),
            (
                {"codes": []},
                {"codes": [{"name": "Added", "references": [{"quote": "Source"}]}]},
            ),
        ]
        for before, after in pairs:
            patch = make_patch(before, after)
            self.assertEqual(
                digest(jsonpatch.apply_patch(before, patch, in_place=False)),
                digest(after),
            )
            self.assertEqual(patch, make_patch(before, after))
