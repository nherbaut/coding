"""Optional real-browser workflow. Run manage.py test corpus.browser_tests."""

import os
import uuid
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from .importers import put_document
from .models import Case, SharedCodebook


class BrowserWorkflowTests(StaticLiveServerTestCase):
    def test_assignment_to_codebook_merge(self):
        from playwright.sync_api import sync_playwright

        SharedCodebook.objects.create(
            content=dict(
                version=1,
                codes=[
                    dict(
                        id=str(uuid.uuid4()),
                        kind="code",
                        name="Human review",
                        parent=None,
                        memo="Verified corpus excerpt",
                        references=[],
                    )
                ],
            )
        )
        # Exercise a long taxonomy that previously pushed Save below the viewport.
        book = SharedCodebook.objects.get()
        book.content["codes"].extend(
            dict(
                id=str(uuid.uuid4()),
                kind="code",
                name=f"Taxonomy leaf {index}",
                parent=None,
            )
            for index in range(70)
        )
        # Shared revisions are immutable, so publish a new setup revision.
        SharedCodebook.objects.create(content=book.content)
        case = Case.objects.create(key="browser", name="Example OSS project")
        put_document(
            "browser-document",
            "AI_POLICY.md",
            "Contributors must disclose AI use. Human review is required. 😀",
            {"repository": "example/project"},
            [case],
        )
        get_user_model().objects.create_superuser(
            "test-admin", "test@example.org", "test-password"
        )
        with sync_playwright() as playwright:
            options = {"headless": True}
            if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
                options["executable_path"] = os.environ[
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE"
                ]
            browser = playwright.chromium.launch(**options)
            admin = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            admin.on("pageerror", lambda error: errors.append(str(error)))
            admin.goto(self.live_server_url + "/admin/login/?next=/")
            admin.get_by_label("Username:").fill("test-admin")
            admin.get_by_label("Password:").fill("test-password")
            admin.get_by_role("button", name="Log in").click()
            admin.get_by_label("Coder name").fill("Browser Coder")
            admin.get_by_label("Assigned cases").select_option(label=case.name)
            admin.get_by_role("button", name="Create private link").click()
            link = admin.locator(".notice").inner_text()
            coder = browser.new_context(
                viewport={"width": 1440, "height": 1000}
            ).new_page()
            coder.on("pageerror", lambda error: errors.append(str(error)))
            coder.goto(link)
            coder.wait_for_selector('#document:has-text("Contributors")')
            self.assertFalse(coder.locator("#selection").is_visible())
            self.assertFalse(coder.locator("#annotation-guidance").is_visible())
            self.assertGreater(
                coder.locator("#code-tree").bounding_box()["height"], 400
            )

            coder.locator("#document").evaluate(
                """node => {
                const range = document.createRange();
                range.setStart(node.firstChild, 0);
                range.setEnd(node.firstChild, 12);
                const selection = window.getSelection();
                selection.removeAllRanges(); selection.addRange(range);
                node.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
            }"""
            )
            coder.get_by_role("button", name="Add code", exact=True).click()
            coder.get_by_label("Name", exact=True).fill("Disclosure requirement")
            coder.get_by_label("Highlight color").evaluate(
                "input => input.value = '#f08080'"
            )
            coder.get_by_label("Memo · quoted evidence and citation").fill(
                '"Contributors" (AI_POLICY.md, selected passage).'
            )
            coder.get_by_label("Attach selected corpus excerpt as a reference").check()
            coder.get_by_role("button", name="Save concept", exact=True).click()
            coder.wait_for_function("!document.querySelector('#code-dialog').open")
            coder.locator("#code-tree").get_by_label(
                "Disclosure requirement", exact=True
            ).check()
            coder.locator("#document mark.annotation-preview").wait_for()
            preview_state = coder.evaluate("fetch('/api/state/').then(r => r.json())")
            self.assertEqual(preview_state["assignments"][0]["annotations"], [])
            self.assertIn(
                "Unsaved annotation", coder.locator("#annotation-guidance").inner_text()
            )
            save_rect = coder.locator("#save-annotation").bounding_box()
            self.assertLessEqual(
                save_rect["y"] + save_rect["height"],
                coder.evaluate("window.innerHeight"),
            )
            # Filtering the tree must preserve selected codes hidden by the search.
            coder.get_by_label("Find a code").fill("Human")
            coder.get_by_label("Annotation memo").fill("Coding evidence")
            coder.get_by_role("button", name="Save annotation", exact=True).click()
            coder.locator("#annotations").get_by_text(
                "Coding evidence", exact=True
            ).wait_for()
            self.assertEqual(
                coder.locator("#document mark.annotation-preview").count(), 0
            )
            self.assertEqual(
                coder.locator("#annotation-count").inner_text(), "1 saved annotation"
            )
            mark = coder.locator("#document mark").first
            self.assertTrue(
                mark.evaluate(
                    "node => node.style.background.includes('#f08080') || node.style.background.includes('240, 128, 128')"
                )
            )
            # A second, overlapping code must produce two visible color bands.
            state = coder.evaluate(
                "fetch('/api/state/').then(response => response.json())"
            )
            assignment_id = state["assignments"][0]["id"]
            version = state["assignments"][0]["documents"][0]
            original = next(
                code
                for code in state["codebook"]["codes"]
                if code["name"] == "Human review"
            )
            result = coder.evaluate(
                """async data => {
                const response = await fetch(`/api/assignments/${data.assignment}/annotations/`, {
                    method:'POST', headers:{'Content-Type':'application/json', 'X-CSRFToken':document.querySelector('[name=csrfmiddlewaretoken]').value},
                    body:JSON.stringify(data.annotation)
                }); return response.status;
            }""",
                dict(
                    assignment=assignment_id,
                    annotation=dict(
                        revision=1,
                        codebook_revision=1,
                        document=version["id"],
                        document_hash=version["hash"],
                        start=0,
                        end=12,
                        selected_text="Contributors",
                        codes=[original["id"]],
                        memo="Overlapping evidence",
                    ),
                ),
            )
            self.assertEqual(result, 200)
            coder.reload()
            coder.wait_for_selector("#document mark")
            self.assertGreaterEqual(
                coder.locator("#document mark").first.evaluate(
                    "node => getComputedStyle(node).backgroundImage.split('color(srgb').length"
                ),
                5,
            )
            # Editing a saved annotation autosaves without an explicit save click.
            coder.locator("#annotations").get_by_role(
                "button", name="Edit", exact=True
            ).first.click()
            coder.get_by_label("Annotation memo").fill("Revised evidence")
            coder.locator("#annotations").get_by_text(
                "Revised evidence", exact=True
            ).wait_for()
            coder.once("dialog", lambda dialog: dialog.accept())
            coder.get_by_role("button", name="Submit case", exact=True).click()
            coder.wait_for_function(
                "document.querySelector('#save-status').textContent === 'Case submitted'"
            )
            admin.goto(self.live_server_url + "/")
            admin.get_by_role(
                "link", name="Review submission", exact=False
            ).first.click()
            admin.locator("select[name^=action_]").select_option("accept")
            admin.get_by_role("button", name="Apply review decisions").click()
            admin.get_by_text("Codebook reviewed.", exact=False).wait_for()
            self.assertGreater(admin.locator(".fulltext mark").count(), 0)
            self.assertEqual(errors, [])
            browser.close()
