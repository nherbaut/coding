import os
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from .test_reporting import report_fixture
from .services import create_session
from .models import DocumentVersion, Annotation


class CorpusUploadBrowserTests(StaticLiveServerTestCase):
    def test_preview_import_then_explicit_assignment(self):
        self.workflow(zip_mode=False)

    def test_zip_preview_import_then_explicit_assignment(self):
        self.workflow(zip_mode=True)

    def workflow(self, zip_mode):
        from playwright.sync_api import sync_playwright

        data = report_fixture()
        coder, link = create_session(data["study"], "Clara", [data["case"]])
        url = reverse("corpus-upload", args=[data["study"].pk])
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=os.environ.get(
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/usr/bin/google-chrome"
                ),
            )
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(self.live_server_url + "/admin/login/?next=" + url)
            page.get_by_label("Username:", exact=True).fill("report-admin")
            page.get_by_label("Password:", exact=True).fill("test-password")
            page.get_by_role("button", name="Log in", exact=True).click()
            if zip_mode:
                from .test_corpus_zip import archive

                source = archive(
                    [
                        ("nested/extra.md", "# Policy\r\nA😀 review\r\n".encode()),
                        ("notes.txt", b"Additional evidence\n"),
                    ]
                )
                page.get_by_label("Files", exact=True).set_input_files(
                    {
                        "name": "corpus.zip",
                        "mimeType": "application/zip",
                        "buffer": source.read(),
                    }
                )
            else:
                page.get_by_label("Files", exact=True).set_input_files(
                    [
                        {
                            "name": "extra.md",
                            "mimeType": "text/markdown",
                            "buffer": "# Policy\r\nA😀 review\r\n".encode(),
                        },
                        {
                            "name": "notes.txt",
                            "mimeType": "text/plain",
                            "buffer": b"Additional evidence\n",
                        },
                    ]
                )
            page.get_by_label("Existing case", exact=True).select_option(
                str(data["case"].pk)
            )
            page.get_by_role("button", name="Preview import", exact=True).click()
            page.get_by_role(
                "heading", name="2. Review and import", exact=True
            ).wait_for()
            page.get_by_label("Source URL for extra.md", exact=True).fill(
                "https://example.org/new"
            )
            if zip_mode:
                page.get_by_text(
                    "From corpus.zip / nested/extra.md", exact=False
                ).wait_for()
                page.screenshot(
                    path="/tmp/coding-corpus-zip-preview.png", full_page=True
                )
            page.get_by_role("button", name="Confirm import", exact=True).click()
            page.get_by_role(
                "heading", name="3. Assign to coders", exact=True
            ).wait_for()
            self.assertTrue(
                page.get_by_role(
                    "checkbox", name="Alice · Submitted — reopen first", exact=True
                ).is_disabled()
            )
            other = browser.new_context()
            coder_page = other.new_page()
            coder_page.goto(self.live_server_url + link)
            coder_page.wait_for_url("**/work/")
            before = other.request.get(self.live_server_url + "/api/state/").json()
            self.assertEqual(len(before["assignments"][0]["documents"]), 1)
            page.get_by_role("checkbox", name="Clara · Available", exact=True).check()
            page.get_by_role(
                "button", name="Add texts to selected coders", exact=True
            ).click()
            page.get_by_text(
                "Imported texts added to 1 coder assignment(s).", exact=True
            ).wait_for()
            self.assertTrue(
                page.get_by_role(
                    "checkbox", name="Clara · Already includes these texts", exact=True
                ).is_disabled()
            )
            after = other.request.get(self.live_server_url + "/api/state/").json()
            self.assertEqual(len(after["assignments"][0]["documents"]), 3)
            page.screenshot(path="/tmp/coding-corpus-upload.png", full_page=True)
            self.assertEqual(errors, [])
            browser.close()
        version = DocumentVersion.objects.get(document__name="extra.md")
        self.assertEqual(version.text, "# Policy\r\nA😀 review\r\n")
        self.assertEqual(version.metadata["url"], "https://example.org/new")
        if zip_mode:
            self.assertEqual(
                version.metadata["source_archive"]["member"], "nested/extra.md"
            )
        self.assertEqual(Annotation.objects.count(), 4)
