import os
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from .test_reporting import report_fixture


class PrivateLinksBrowserTests(StaticLiveServerTestCase):
    def test_admin_shows_copies_and_reuses_private_link(self):
        from playwright.sync_api import sync_playwright

        data = report_fixture()
        dashboard = reverse("dashboard", args=[data["study"].pk])
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=os.environ.get(
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/usr/bin/google-chrome"
                ),
            )
            context = browser.new_context(
                permissions=["clipboard-read", "clipboard-write"]
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.live_server_url + "/admin/login/?next=" + dashboard)
            page.get_by_label("Username:", exact=True).fill("report-admin")
            page.get_by_label("Password:", exact=True).fill("test-password")
            page.get_by_role("button", name="Log in", exact=True).click()
            panel = page.locator("article.session").filter(
                has=page.get_by_role("heading", name="Alice", exact=True)
            )
            panel.get_by_role("button", name="Show private link", exact=True).click()
            field = panel.get_by_label("Private coder URL", exact=True)
            field.wait_for(state="visible")
            self.assertEqual(field.input_value(), self.live_server_url + data["link"])
            panel.get_by_role("button", name="Copy link", exact=True).click()
            panel.get_by_text("Private link copied.", exact=True).wait_for()
            self.assertEqual(
                page.evaluate("navigator.clipboard.readText()"),
                self.live_server_url + data["link"],
            )
            self.assertFalse(panel.get_by_role("button", name="Delete coder session", exact=True).is_visible())
            panel.screenshot(path="/tmp/coding-invitation-desktop.png")
            page.set_viewport_size({"width": 390, "height": 844})
            panel.screenshot(path="/tmp/coding-invitation-mobile.png")
            self.assertLessEqual(panel.bounding_box()["width"], 390)
            panel.get_by_text("Access and session management", exact=True).click()
            self.assertTrue(panel.get_by_role("button", name="Delete coder session", exact=True).is_visible())
            page.set_viewport_size({"width": 1280, "height": 900})
            coder = browser.new_context()
            coder_page = coder.new_page()
            coder_page.goto(field.input_value())
            coder_page.wait_for_url("**/work/")
            self.assertEqual(errors, [])
            browser.close()
