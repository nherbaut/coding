"""Browser coverage of shared graph drafts, admin coding, and app logout."""

import os
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from .test_reporting import report_fixture
from .models import CodingSession, Annotation


class AdminToolsBrowserTests(StaticLiveServerTestCase):
    def test_graph_drafts_and_real_admin_coding(self):
        from playwright.sync_api import sync_playwright

        d = report_fixture()
        study = d["study"]
        graph_path = reverse("study-codebook", args=[study.pk])
        coding_path = reverse("admin-coding", args=[study.pk])
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=os.environ.get(
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/usr/bin/google-chrome"
                ),
            )
            context = browser.new_context(viewport={"width": 1600, "height": 1100})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(self.live_server_url + "/admin/login/?next=" + graph_path)
            page.get_by_label("Username:", exact=True).fill("report-admin")
            page.get_by_label("Password:", exact=True).fill("test-password")
            page.get_by_role("button", name="Log in", exact=True).click()
            page.get_by_text("Shared codebook saved.", exact=True).wait_for()
            page.get_by_role("button", name="● Review", exact=True).click()
            self.assertEqual(
                page.locator("#node-memo").input_value(), "Review evidence"
            )
            page.locator("#node-name").fill("Review practices")
            page.get_by_role("button", name="Apply to draft", exact=True).click()
            page.get_by_role("button", name="Undo", exact=True).click()
            self.assertEqual(page.locator("#node-name").input_value(), "Review")
            page.get_by_role("button", name="Redo", exact=True).click()
            self.assertEqual(
                page.locator("#node-name").input_value(), "Review practices"
            )
            # Layout and viewport changes do not change the published revision.
            revision = page.evaluate(
                "fetch(location.href+'state/').then(r=>r.json()).then(d=>d.revision)"
            )
            page.get_by_role("button", name="Automatic layout", exact=True).click()
            page.get_by_role("button", name="Fit", exact=True).click()
            page.get_by_role("button", name="Save codebook", exact=True).click()
            page.get_by_text("Shared codebook saved.", exact=True).wait_for()
            self.assertNotEqual(
                page.evaluate(
                    "fetch(location.href+'state/').then(r=>r.json()).then(d=>d.revision)"
                ),
                revision,
            )
            # Referenced identity removal is offered as archive.
            page.get_by_role("button", name="Delete concept…", exact=True).click()
            self.assertTrue(page.locator("#confirm-delete").is_disabled())
            page.get_by_role("button", name="Archive instead", exact=True).click()
            self.assertTrue(page.locator("#node-archived").is_checked())
            page.get_by_role("button", name="Undo", exact=True).click()
            # Add a new root and connect an existing node using draw mode.
            page.get_by_role("button", name="Add concept", exact=True).click()
            page.locator("#node-name").fill("Extra dimension")
            page.locator("#node-kind").select_option("category")
            page.locator("#node-parent").select_option("")
            page.get_by_role("button", name="Apply to draft", exact=True).click()
            page.get_by_role(
                "button", name="Draw parent connection", exact=True
            ).click()
            positions = page.locator("#codebook-graph").evaluate(
                "node=>{const cy=node._cyreg.cy;const rect=node.getBoundingClientRect();return ['Extra dimension','Review practices'].map(name=>{const n=cy.nodes().filter(n=>n.data('label')===name);const p=n.renderedPosition();return {x:rect.x+p.x,y:rect.y+p.y};});}"
            )
            page.mouse.move(**positions[0])
            page.mouse.down()
            page.mouse.move(**positions[1], steps=25)
            page.mouse.up()
            page.get_by_role(
                "button", name="Draw parent connection", exact=True
            ).click()
            page.get_by_role("button", name="● Review practices", exact=True).click()
            self.assertEqual(
                page.locator("#node-parent option:checked").inner_text(),
                "Extra dimension",
            )
            page.get_by_role("button", name="Save codebook", exact=True).click()
            page.get_by_text("Shared codebook saved.", exact=True).wait_for()
            # Edge deletion makes the child a root; undo restores its parent.
            midpoint = page.locator("#codebook-graph").evaluate(
                "node=>{const cy=node._cyreg.cy;const edge=cy.edges().filter(e=>e.target().data('label')==='Review practices');const p=edge.renderedMidpoint(),r=node.getBoundingClientRect();return {x:r.x+p.x,y:r.y+p.y};}"
            )
            page.mouse.click(**midpoint)
            page.get_by_role(
                "button", name="Delete selected connection", exact=True
            ).click()
            self.assertEqual(page.locator("#node-parent").input_value(), "")
            page.get_by_role("button", name="Undo", exact=True).click()
            self.assertEqual(
                page.locator("#node-parent option:checked").inner_text(),
                "Extra dimension",
            )
            # Node-only deletion reparents children; undo restores the hierarchy.
            page.get_by_role("button", name="▰ Extra dimension", exact=True).click()
            page.get_by_role("button", name="Delete concept…", exact=True).click()
            page.locator("#confirm-delete").click()
            page.get_by_role("button", name="● Review practices", exact=True).click()
            self.assertEqual(page.locator("#node-parent").input_value(), "")
            page.get_by_role("button", name="Undo", exact=True).click()
            page.get_by_role("button", name="● Review practices", exact=True).click()
            self.assertTrue(page.locator("#publish-book").is_disabled())
            page.screenshot(path="/tmp/coding-codebook-graph.png", full_page=True)
            # Stale second tab retains its draft and shows the conflict.
            stale = context.new_page()
            stale.goto(self.live_server_url + graph_path)
            stale.get_by_text("Shared codebook saved.", exact=True).wait_for()
            stale.get_by_role("button", name="● Review practices", exact=True).click()
            stale.locator("#node-rationale").fill("Unpublished rationale")
            page.locator("#node-rationale").fill("Published rationale")
            page.get_by_role("button", name="Save codebook", exact=True).click()
            page.get_by_text("Shared codebook saved.", exact=True).wait_for()
            stale.get_by_role("button", name="Save codebook", exact=True).click()
            stale.get_by_text("Your local draft is preserved.", exact=False).wait_for()
            self.assertEqual(
                stale.locator("#node-rationale").input_value(), "Unpublished rationale"
            )
            # Browser warning protects a dirty graph on logout.
            dialogs = []
            stale.on(
                "dialog", lambda dialog: (dialogs.append(dialog.type), dialog.dismiss())
            )
            stale.get_by_role("button", name="Log out", exact=True).click(
                no_wait_after=True
            )
            stale.wait_for_timeout(250)
            self.assertIn("beforeunload", dialogs)
            stale.close(run_before_unload=False)
            # Full-corpus workspace creates a personal real-coding session.
            page.get_by_role("link", name="Code full corpus", exact=True).click()
            page.locator("#document").get_by_text(
                "A😀 human review and disclosure", exact=False
            ).wait_for()
            self.assertContainsText(
                page, "These are real annotations included in study reports."
            )
            page.locator("#document").evaluate(
                "node=>{const text=document.createTreeWalker(node,NodeFilter.SHOW_TEXT).nextNode();const range=document.createRange();range.setStart(text,1);range.setEnd(text,3);const sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);node.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));}"
            )
            page.locator("#code-tree input[type=checkbox]").first.check()
            annotation_dialogs = []
            page.once(
                "dialog",
                lambda dialog: (
                    annotation_dialogs.append(dialog.type),
                    dialog.dismiss(),
                ),
            )
            page.get_by_role("button", name="Log out", exact=True).click(
                no_wait_after=True
            )
            page.wait_for_timeout(250)
            self.assertIn("beforeunload", annotation_dialogs)
            self.assertTrue(page.url.endswith(coding_path))
            page.get_by_role("button", name="Save annotation", exact=True).click()
            page.get_by_text("All changes saved", exact=True).wait_for()
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Submit case", exact=True).click()
            page.get_by_role("button", name="Unsubmit case", exact=True).wait_for()
            page.screenshot(path="/tmp/coding-admin-workspace.png", full_page=True)
            page.get_by_role("button", name="Log out", exact=True).click()
            page.get_by_role(
                "heading", name="You are signed out", exact=True
            ).wait_for()
            page.goto(
                self.live_server_url + "/access/" + d["link"].split("/access/")[1]
            )
            page.get_by_text("Alice", exact=True).wait_for()
            self.assertEqual(errors, [])
            browser.close()
        session = CodingSession.objects.get(admin_user=d["admin"])
        self.assertEqual(
            Annotation.objects.filter(assignment__session=session).count(), 1
        )

    def assertContainsText(self, page, text):
        self.assertIn(text, page.locator("body").inner_text())
