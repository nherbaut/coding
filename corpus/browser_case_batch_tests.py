import os
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from .test_reporting import report_fixture
from .test_case_batches import folder_zip
from .services import create_session


class CaseBatchBrowserTests(StaticLiveServerTestCase):
    def test_import_multiple_cases_without_naming_a_case(self):
        from playwright.sync_api import sync_playwright

        data = report_fixture()
        coder, _ = create_session(data["study"], "Charlie", [])
        source = folder_zip()
        url = reverse("corpus-upload", args=[data["study"].pk])
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=os.environ.get(
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/usr/bin/google-chrome"
                ),
            )
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            page.goto(self.live_server_url + "/admin/login/?next=" + url)
            page.get_by_label("Username:", exact=True).fill("report-admin")
            page.get_by_label("Password:", exact=True).fill("test-password")
            page.get_by_role("button", name="Log in", exact=True).click()
            page.get_by_label("Cases ZIP", exact=True).set_input_files(
                {
                    "name": "cases.zip",
                    "mimeType": "application/zip",
                    "buffer": source.read(),
                }
            )
            page.get_by_role("button", name="Preview cases", exact=True).click()
            page.get_by_role("heading", name="2. Review cases", exact=True).wait_for()
            page.get_by_role("heading", name="Project A", exact=True).wait_for()
            page.get_by_role("heading", name="Project B", exact=True).wait_for()
            page.screenshot(path="/tmp/coding-case-batch-preview.png", full_page=True)
            page.get_by_role("button", name="Confirm case import", exact=True).click()
            page.get_by_role(
                "heading", name="3. Assign imported cases", exact=True
            ).wait_for()
            page.get_by_role("checkbox", name="Charlie", exact=True).check()
            page.get_by_role(
                "button", name="Assign all imported cases", exact=True
            ).click()
            page.get_by_text(
                "New cases added to 2 coder assignment(s).", exact=True
            ).wait_for()
            browser.close()
        self.assertEqual(coder.assignment_set.count(), 2)
