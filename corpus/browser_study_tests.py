"""Study ownership, admin invitation and coder workflow through the browser."""

import os
import uuid
import yaml
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase


class StudyBrowserTests(StaticLiveServerTestCase):
    def test_owner_coadmin_and_coder_from_study_creation_to_review(self):
        from playwright.sync_api import sync_playwright

        for username in ("study-owner", "collaborator"):
            get_user_model().objects.create_user(
                username, password="test-password", is_staff=True
            )
        code = str(uuid.uuid4())
        book = {
            "version": 1,
            "codes": [
                dict(
                    id=code,
                    kind="code",
                    parent=None,
                    name="Review",
                    memo="Verified corpus quote",
                    references=[],
                )
            ],
        }
        with sync_playwright() as p:
            options = {"headless": True}
            if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
                options["executable_path"] = os.environ[
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE"
                ]
            browser = p.chromium.launch(**options)
            owner = browser.new_page(viewport={"width": 1500, "height": 1000})
            member = browser.new_page(viewport={"width": 1500, "height": 1000})
            errors = []
            for page in (owner, member):
                page.on("pageerror", lambda e: errors.append(str(e)))

            def login(page, user):
                page.goto(self.live_server_url + "/admin/login/?next=/")
                page.get_by_label("Username:", exact=True).fill(user)
                page.get_by_label("Password:", exact=True).fill("test-password")
                page.get_by_role("button", name="Log in", exact=True).click()
                page.get_by_role("heading", name="My studies", exact=True).wait_for()

            login(owner, "study-owner")
            owner.get_by_label("Study title", exact=True).fill("OSS sustainability")
            owner.get_by_label("Description", exact=True).fill(
                "A collaborative empirical study"
            )
            owner.get_by_role("button", name="Create study", exact=True).click()
            owner.get_by_role(
                "heading", name="OSS sustainability", exact=True
            ).wait_for()
            study_url = owner.url
            owner.get_by_role("link", name="Cases and texts", exact=True).click()
            owner.get_by_label("Case name", exact=True).fill("Example project")
            owner.get_by_role("button", name="Create case", exact=True).click()
            owner.get_by_text("Case created", exact=True).wait_for()
            owner.get_by_label("Document title", exact=True).fill("POLICY.md")
            owner.get_by_label("Or paste text", exact=True).fill(
                "Contributors **must review 😀** before merging."
            )
            owner.get_by_role("button", name="Add text", exact=True).click()
            owner.get_by_text("Text added to case", exact=True).wait_for()
            owner.get_by_role(
                "link", name="Questions and guidelines", exact=True
            ).click()
            owner.get_by_text("Add a research question", exact=True).click()
            form = owner.locator("details").filter(
                has=owner.locator("input[name=label]")
            )
            form.get_by_label("Question label", exact=True).fill("RQ1")
            form.get_by_label("Question text", exact=True).fill(
                "How is human review required?"
            )
            form.get_by_role("button", name="Add question", exact=True).click()
            owner.get_by_text("Add a coding guideline", exact=True).click()
            form = owner.locator("details").filter(
                has=owner.locator("input[name=title]")
            )
            form.get_by_label("Guideline title", exact=True).fill(
                "Code explicit requirements"
            )
            form.get_by_label("Guideline text (Markdown)", exact=True).fill(
                "Read the **surrounding paragraph**."
            )
            form.locator("select[name=questions]").select_option(label="RQ1")
            form.get_by_role("button", name="Add guideline", exact=True).click()
            owner.get_by_role("link", name="Overview", exact=True).click()
            owner.get_by_label("Codebook YAML", exact=True).set_input_files(
                {
                    "name": "book.yaml",
                    "mimeType": "application/yaml",
                    "buffer": yaml.safe_dump(book).encode(),
                }
            )
            owner.get_by_role(
                "button", name="Publish shared version", exact=True
            ).click()
            owner.get_by_text("Study codebook version published", exact=True).wait_for()
            owner.get_by_label("Coder name", exact=True).fill("Research coder")
            owner.get_by_label("Assigned cases").select_option(label="Example project")
            owner.get_by_role("button", name="Create private link", exact=True).click()
            owner.locator(".notice").wait_for()
            link = owner.locator(".notice").inner_text()
            coder = browser.new_page(viewport={"width": 1500, "height": 1000})
            coder.on("pageerror", lambda e: errors.append(str(e)))
            coder.goto(link)
            coder.locator("#document strong").wait_for()
            coder.get_by_role(
                "button", name="Questions and guidelines", exact=True
            ).click()
            coder.locator("#instructions-dialog[open]").wait_for()
            self.assertTrue(coder.get_by_text("RQ1", exact=True).is_visible())
            self.assertTrue(
                coder.locator("#study-instructions strong")
                .filter(has_text="surrounding paragraph")
                .is_visible()
            )
            coder.locator("#instructions-dialog").get_by_role(
                "button", name="Close", exact=True
            ).click()
            coder.locator("#document").evaluate(
                """root=>{const node=root.querySelector('strong').firstChild;window.getSelection().setBaseAndExtent(node,0,node,node.length);root.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));}"""
            )
            coder.locator("#code-tree").get_by_label("Review", exact=True).check()
            coder.get_by_role("button", name="Save annotation", exact=True).click()
            coder.wait_for_function(
                "document.querySelector('#save-annotation').disabled"
            )
            coder.once("dialog", lambda d: d.accept())
            coder.get_by_role("button", name="Submit case", exact=True).click()
            coder.get_by_role("button", name="Unsubmit case", exact=True).wait_for()
            owner.reload()
            owner.get_by_role("link", name="Review submission", exact=True).wait_for()
            owner.get_by_role("link", name="Settings and admins", exact=True).click()
            owner.get_by_label("LDAP username", exact=True).fill("collaborator")
            owner.get_by_role("button", name="Invite study admin", exact=True).click()
            owner.get_by_text("Admin invited.", exact=False).wait_for()
            login(member, "collaborator")
            self.assertEqual(member.request.get(study_url).status, 404)
            member.get_by_role("button", name="Accept invitation", exact=True).click()
            member.get_by_role(
                "heading", name="OSS sustainability", exact=True
            ).wait_for()
            member.get_by_role("link", name="Review submission", exact=True).click()
            member.locator(".annotation blockquote").wait_for()
            self.assertIn(
                "must review",
                member.locator(".annotation blockquote").first.inner_text(),
            )
            member.goto(study_url + "settings/")
            self.assertEqual(
                member.get_by_role(
                    "button", name="Invite study admin", exact=True
                ).count(),
                0,
            )
            owner.reload()
            owner.get_by_role("button", name="Revoke admin access", exact=True).click()
            owner.get_by_text("Study admin access revoked", exact=True).wait_for()
            self.assertEqual(member.request.get(study_url).status, 404)
            owner.goto(study_url)
            owner.screenshot(path="/tmp/coding-study-overview.png", full_page=False)
            self.assertEqual(errors, [])
            browser.close()
