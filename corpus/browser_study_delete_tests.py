"""Owner-confirmed permanent study deletion through the browser."""

import os
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from .test_reporting import report_fixture
from .models import Study, Annotation


class StudyDeletionBrowserTests(StaticLiveServerTestCase):
    def test_owner_confirmation_cancel_and_delete(self):
        from playwright.sync_api import sync_playwright

        data = report_fixture()
        study = data["study"]
        identity = study.pk
        settings = reverse("study-settings", args=[identity])
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=os.environ.get(
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/usr/bin/google-chrome"
                ),
            )
            page = browser.new_page(viewport={"width": 1450, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.live_server_url + "/admin/login/?next=" + settings)
            page.get_by_label("Username:", exact=True).fill("report-admin")
            page.get_by_label("Password:", exact=True).fill("test-password")
            page.get_by_role("button", name="Log in", exact=True).click()
            page.get_by_role("link", name="Delete study…", exact=True).click()
            page.get_by_role("link", name="Cancel", exact=True).click()
            page.get_by_role("link", name="Delete study…", exact=True).click()
            page.get_by_label("Type the study title to confirm", exact=True).fill(
                "Wrong title"
            )
            page.get_by_role(
                "button", name="Permanently delete study", exact=True
            ).click()
            page.get_by_role("alert").wait_for()
            self.assertIn(
                "exact current study title", page.get_by_role("alert").inner_text()
            )
            page.get_by_label("Type the study title to confirm", exact=True).fill(
                study.title
            )
            page.screenshot(
                path="/tmp/coding-delete-study-confirmation.png", full_page=True
            )
            page.get_by_role(
                "button", name="Permanently delete study", exact=True
            ).click()
            page.get_by_role("heading", name="My studies", exact=True).wait_for()
            page.get_by_text("permanently deleted.", exact=False).wait_for()
            self.assertEqual(
                page.request.get(self.live_server_url + settings).status, 404
            )
            self.assertEqual(errors, [])
            browser.close()
        self.assertFalse(Study.objects.filter(pk=identity).exists())
        self.assertFalse(Annotation.objects.exists())
