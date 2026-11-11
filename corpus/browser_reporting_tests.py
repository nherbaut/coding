from .test_support import new_study
from .studies import publish_codebook

"""Optional browser coverage for report exploration, comparisons and exports."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from .test_reporting import report_fixture


class ReportBrowserTests(StaticLiveServerTestCase):

    def test_report_comparison_and_saved_exports(self):
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
            page = browser.new_page(viewport=dict(width=1500, height=1000))
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(
                self.live_server_url
                + ("/admin/login/?next=" + f"/admin/studies/{self.study.pk}/")
            )
            page.get_by_label("Username:").fill("report-admin")
            page.get_by_label("Password:").fill("test-password")
            page.get_by_role("button", name="Log in", exact=True).click()
            page.get_by_role("link", name="Coding reports", exact=True).click()
            page.locator(".report-excerpt").first.wait_for()
            self.assertEqual(page.locator(".report-excerpt").count(), 4)
            page.get_by_label("Code or category", exact=True).select_option(
                "original:" + data["review"]
            )
            page.get_by_role("button", name="Filter evidence", exact=True).click()
            page.locator(".report-excerpt").first.wait_for()
            self.assertEqual(page.locator(".report-excerpt").count(), 3)
            self.assertTrue(
                page.get_by_role(
                    "heading", name="Social › Review", exact=True
                ).is_visible()
            )
            page.get_by_role("link", name="Document comparison", exact=True).click()
            page.locator(".comparison-pane").first.wait_for()
            self.assertEqual(page.locator(".comparison-pane").count(), 2)
            self.assertTrue(
                page.get_by_label("Scroll both document panels together").is_checked()
            )
            page.locator(".comparison-pane").first.get_by_role(
                "button", name="Locate passage", exact=True
            ).first.click()
            self.assertEqual(
                page.locator(".comparison-pane")
                .first.locator("mark.report-selected")
                .count(),
                1,
            )
            panels = page.locator(".comparison-text")
            panels.first.evaluate("node => node.scrollTop = 400")
            page.wait_for_function(
                "Math.abs(document.querySelectorAll('.comparison-text')[0].scrollTop-document.querySelectorAll('.comparison-text')[1].scrollTop)<3"
            )
            panels.first.locator("mark").first.click()
            self.assertGreater(
                page.locator(".comparison-annotation.report-selected").count(), 0
            )
            page.screenshot(path="/tmp/coding-report-comparison.png", full_page=False)
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path="/tmp/coding-report-controls.png", full_page=False)
            with page.expect_download() as download_info:
                page.get_by_role("link", name="Excerpts CSV", exact=True).click()
            downloaded = download_info.value
            self.assertIn("document_hash", Path(downloaded.path()).read_text())
            page.get_by_label("Report name", exact=True).fill("Browser final report")
            page.get_by_role("button", name="Save report snapshot", exact=True).click()
            self.assertTrue(
                page.get_by_role(
                    "heading", name="Browser final report", exact=True
                ).is_visible()
            )
            saved = page.request.get(page.url + "?format=json").json()
            self.assertEqual(
                saved["payload"]["filters"]["code"], "original:" + data["review"]
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(
                    lambda: data["assignments"][0].annotation_set.all().delete()
                ).result()
            page.reload()
            page.locator(".report-excerpt").first.wait_for()
            self.assertEqual(page.locator(".report-excerpt").count(), 3)
            with page.expect_download() as download_info:
                page.get_by_role("link", name="Download HTML", exact=True).click()
            html = Path(download_info.value.path()).read_text()
            self.assertIn("Browser final report", html)
            self.assertIn("Policy source", html)
            self.assertNotIn("<script src=", html)
            page.set_viewport_size(dict(width=600, height=900))
            page.get_by_role("link", name="Document comparison", exact=True).click()
            self.assertLessEqual(
                page.evaluate("document.documentElement.scrollWidth"), 610
            )
            self.assertEqual(errors, [])
            browser.close()
