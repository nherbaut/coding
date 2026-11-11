"""Browser import, real coding and automatic export with a simulated GitHub API."""

import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, parse_qs, urlencode
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.db import connections
from django.urls import reverse
from .github_client import GitHubClient
from .github_sync import synchronize_next
from .github_repository import replay_codebook
from .test_github_sync import fixture_files
from .test_github_bootstrap import BootstrapGit, corpus_zip


@override_settings(
    CASE_EMAIL_ENABLED=False,
    GITHUB_APP_ID="1",
    GITHUB_CLIENT_ID="test",
    GITHUB_CLIENT_SECRET="test",
    GITHUB_PRIVATE_KEY_FILE="/protected/key.pem",
    GITHUB_APP_SLUG="coding-test",
)
class GitHubBootstrapBrowserTests(StaticLiveServerTestCase):
    def test_upload_empty_repository_coding_and_publication(self):
        from playwright.sync_api import sync_playwright

        get_user_model().objects.create_user(
            "github-owner", password="test-password", is_staff=True
        )
        files, code = fixture_files()
        fake = BootstrapGit(empty=True)
        repo = dict(id=99, full_name="research/data", permissions={"push": True})

        def sync():
            try:
                return synchronize_next(force=True, client_factory=lambda *_: fake)
            finally:
                connections.close_all()

        with patch(
            "corpus.github_views.http",
            return_value=dict(access_token="fake-token", expires_in=3600),
        ), patch.object(
            GitHubClient, "api", return_value={"login": "github-owner"}
        ), patch.object(
            GitHubClient,
            "repositories",
            return_value=[
                dict(id=99, name="research/data", installation=7, private=True)
            ],
        ), patch.object(
            GitHubClient, "repository_access", return_value=repo
        ), patch(
            "corpus.github_views.installation_client", return_value=fake
        ), sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=os.environ.get(
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/usr/bin/google-chrome"
                ),
            )
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            def oauth(route):
                response = route.fetch(max_redirects=0)
                state = parse_qs(urlsplit(response.headers["location"]).query)["state"][
                    0
                ]
                headers = dict(response.headers)
                headers["location"] = (
                    self.live_server_url
                    + reverse("github-callback")
                    + "?"
                    + urlencode(dict(state=state, code="fake-code"))
                )
                route.fulfill(status=302, headers=headers, body="")

            page.route(self.live_server_url + reverse("github-connect"), oauth)
            page.goto(self.live_server_url + "/admin/login/?next=/")
            page.get_by_label("Username:", exact=True).fill("github-owner")
            page.get_by_label("Password:", exact=True).fill("test-password")
            page.get_by_role("button", name="Log in", exact=True).click()
            page.get_by_role(
                "link", name="Create a study from GitHub", exact=True
            ).click()
            page.get_by_role(
                "button", name="Connect GitHub account", exact=True
            ).click()
            page.wait_for_load_state()
            page.get_by_label("Study title", exact=True).fill("GitHub research")
            page.get_by_label("Data source", exact=True).select_option("uploads")
            page.get_by_label("Corpus ZIP", exact=True).set_input_files(
                {
                    "name": "corpus.zip",
                    "mimeType": "application/zip",
                    "buffer": corpus_zip(),
                }
            )
            page.get_by_label("Codebook YAML", exact=True).set_input_files(
                {
                    "name": "book.yaml",
                    "mimeType": "application/yaml",
                    "buffer": files["codebook/codebook.yaml"].encode(),
                }
            )
            page.get_by_role("button", name="Preview import", exact=True).click()
            page.get_by_role("heading", name="Review import", exact=True).wait_for()
            page.get_by_role(
                "button", name="Create study and activate synchronization", exact=True
            ).click()
            page.get_by_role(
                "heading", name="GitHub research · GitHub", exact=True
            ).wait_for()
            github_url = page.url
            page.get_by_role("link", name="Codebook", exact=True).click()
            page.get_by_text("Shared codebook saved.", exact=True).wait_for()
            page.get_by_role("button", name="● Review", exact=True).click()
            page.get_by_label("Name", exact=True).fill("Review policies")
            page.get_by_role("button", name="Save codebook", exact=True).click()
            page.get_by_text("Shared codebook saved.", exact=True).wait_for()
            page.get_by_role("link", name="Code full corpus", exact=True).click()
            page.locator("#document").get_by_text("A😀 review", exact=False).wait_for()
            page.locator("#document").evaluate(
                "node=>{const text=document.createTreeWalker(node,NodeFilter.SHOW_TEXT).nextNode();const range=document.createRange();range.setStart(text,1);range.setEnd(text,3);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);node.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));}"
            )
            page.locator("#code-tree input[type=checkbox]").first.check()
            page.get_by_label("Annotation memo", exact=True).fill("Research evidence")
            page.get_by_role("button", name="Save annotation", exact=True).click()
            page.get_by_text("All changes saved", exact=True).wait_for()
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Submit case", exact=True).click()
            page.get_by_role("button", name="Unsubmit case", exact=True).wait_for()
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(sync).result())
            page.goto(github_url)
            page.get_by_text("0 pending events", exact=False).wait_for()
            page.get_by_role("cell", name="published", exact=True).wait_for()
            page.screenshot(
                path="/tmp/coding-github-upload-initialization.png", full_page=True
            )
            page.get_by_role("link", name="Cases and texts", exact=True).click()
            page.get_by_text("This corpus is frozen", exact=False).wait_for()
            self.assertEqual(
                page.get_by_role("button", name="Create case", exact=True).count(), 0
            )
            self.assertEqual(errors, [])
            browser.close()
        self.assertEqual(fake.seed_calls, 1)
        self.assertEqual(len(fake.commits), 1)
        self.assertTrue(any("/submissions/" in path for path in fake.files))
        self.assertEqual(
            replay_codebook(fake.files)[1]["codes"][0]["name"], "Review policies"
        )
