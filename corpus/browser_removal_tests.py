from .test_support import new_study
from .studies import publish_codebook

"""Exercise removal buttons and coder resubmission against a temporary database."""
import os
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from .test_reporting import report_fixture


class RemovalBrowserTests(StaticLiveServerTestCase):

    def test_unsubmit_resubmit_remove_case_and_delete_session(self):
        from playwright.sync_api import sync_playwright

        data = report_fixture()
        self.study = data["study"]
        with sync_playwright() as playwright:
            options = dict(headless=True)
            if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
                options["executable_path"] = os.environ[
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE"
                ]
            browser = playwright.chromium.launch(**options)
            admin = browser.new_page(viewport=dict(width=1500, height=1000))
            coder = browser.new_context().new_page()
            errors = []
            for page in (admin, coder):
                page.on("pageerror", lambda error: errors.append(str(error)))
            admin.goto(
                self.live_server_url
                + ("/admin/login/?next=" + f"/admin/studies/{self.study.pk}/")
            )
            admin.get_by_label("Username:").fill("report-admin")
            admin.get_by_label("Password:").fill("test-password")
            admin.get_by_role("button", name="Log in", exact=True).click()
            coder.goto(self.live_server_url + data["link"])
            coder.get_by_role("button", name="Unsubmit case", exact=True).wait_for()
            coder.once("dialog", lambda dialog: dialog.accept())
            coder.get_by_role("button", name="Unsubmit case", exact=True).click()
            coder.get_by_role("button", name="Submit case", exact=True).wait_for()
            self.assertGreater(coder.locator("#document mark").count(), 0)
            coder.once("dialog", lambda dialog: dialog.accept())
            coder.get_by_role("button", name="Submit case", exact=True).click()
            coder.get_by_role("button", name="Unsubmit case", exact=True).wait_for()
            admin.reload()
            submission = (
                admin.locator("#submitted-cases tbody tr")
                .filter(has_text="Alice")
                .first
            )
            self.assertEqual(submission.count(), 1)
            admin.once("dialog", lambda dialog: dialog.dismiss())
            submission.get_by_role(
                "button", name="Delete submitted case", exact=True
            ).click()
            self.assertEqual(
                admin.locator("#submitted-cases tbody tr")
                .filter(has_text="Alice")
                .count(),
                1,
            )
            admin.once("dialog", lambda dialog: dialog.accept())
            submission.get_by_role(
                "button", name="Delete submitted case", exact=True
            ).click()
            admin.get_by_text(
                "Submitted case removed from review.", exact=False
            ).wait_for()
            self.assertEqual(
                admin.locator("#submitted-cases tbody tr")
                .filter(has_text="Alice")
                .count(),
                0,
            )
            coder.reload()
            coder.get_by_role("button", name="Submit case", exact=True).wait_for()
            assignment = (
                admin.locator("#coding-assignments tr").filter(has_text="Alice").first
            )
            admin.once("dialog", lambda dialog: dialog.accept())
            assignment.get_by_role(
                "button", name="Remove assignment", exact=True
            ).click()
            admin.get_by_text("Assignment removed.", exact=False).wait_for()
            self.assertEqual(
                admin.locator("#coding-assignments tr")
                .filter(has_text="Alice")
                .count(),
                0,
            )
            coder.reload()
            coder.get_by_text("No cases assigned", exact=True).wait_for()
            session = admin.locator("article.session").filter(
                has=admin.get_by_role("heading", name="Alice", exact=True)
            )
            admin.once("dialog", lambda dialog: dialog.accept())
            session.get_by_role(
                "button", name="Delete coder session", exact=True
            ).click()
            admin.get_by_text("Coder session deleted.", exact=False).wait_for()
            self.assertEqual(
                admin.locator("article.session")
                .filter(has=admin.get_by_role("heading", name="Alice", exact=True))
                .count(),
                0,
            )
            response = coder.request.get(self.live_server_url + "/api/state/")
            self.assertEqual(response.status, 404)
            admin.get_by_role("link", name="Archived coding", exact=True).click()
            admin.get_by_role("heading", name="Archived coding", exact=True).wait_for()
            self.assertTrue(
                admin.get_by_role("heading", name="Alice", exact=True).is_visible()
            )
            self.assertGreater(
                admin.get_by_role(
                    "link", name="View saved submission", exact=True
                ).count(),
                0,
            )
            admin.screenshot(path="/tmp/coding-removal-archive.png", full_page=True)
            self.assertEqual(errors, [])
            browser.close()
