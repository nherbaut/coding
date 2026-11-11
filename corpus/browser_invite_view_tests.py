"""Duplicate invitations and inspect a personal codebook without editing."""

import os
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse
from .test_reporting import report_fixture
from .models import CodingSession, InvitationEmail
from .invitations import invitation_cipher


@override_settings(
    CASE_EMAIL_ENABLED=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class InviteViewBrowserTests(StaticLiveServerTestCase):
    def test_duplicate_request_and_read_only_graph(self):
        from playwright.sync_api import sync_playwright

        d = report_fixture()
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=os.environ.get(
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/usr/bin/google-chrome"
                ),
            )
            errors = []
            context = browser.new_context(viewport={"width": 1500, "height": 1000})
            owner = context.new_page()
            owner.on("pageerror", lambda e: errors.append(str(e)))
            dashboard = reverse("dashboard", args=[d["study"].pk])
            owner.goto(self.live_server_url + "/admin/login/?next=" + dashboard)
            owner.get_by_label("Username:", exact=True).fill("report-admin")
            owner.get_by_label("Password:", exact=True).fill("test-password")
            owner.get_by_role("button", name="Log in", exact=True).click()
            session = owner.locator(".session").filter(
                has=owner.get_by_role("heading", name="Alice", exact=True)
            )
            session.get_by_text(
                "Duplicate request for another coder", exact=True
            ).click()
            session.get_by_label("New coder name", exact=True).fill("Carol")
            session.get_by_label("New coder email", exact=True).fill(
                "carol@example.org"
            )
            session.get_by_role(
                "button", name="Duplicate and send invitation", exact=True
            ).click()
            owner.get_by_text(
                "New coding request queued for carol@example.org.", exact=False
            ).wait_for()
            owner.get_by_role("heading", name="Carol", exact=True).wait_for()
            coder = browser.new_page(viewport={"width": 1500, "height": 1000})
            coder.on("pageerror", lambda e: errors.append(str(e)))
            coder.goto(self.live_server_url + d["link"])
            coder.get_by_text("Alice", exact=True).wait_for()
            with coder.expect_popup() as popup:
                coder.get_by_role("link", name="View codebook", exact=True).click()
            viewer = popup.value
            viewer.on("pageerror", lambda e: errors.append(str(e)))
            viewer.get_by_text("Read-only personal codebook.", exact=False).wait_for()
            posts = []
            viewer.on(
                "request",
                lambda req: posts.append(req.url) if req.method == "POST" else None,
            )
            viewer.get_by_role("button", name="● Review", exact=True).click()
            self.assertEqual(
                viewer.locator("#node-memo").input_value(), "Review evidence"
            )
            self.assertTrue(viewer.locator("#node-memo").evaluate("el=>el.readOnly"))
            self.assertTrue(viewer.locator("#node-name").evaluate("el=>el.readOnly"))
            self.assertTrue(viewer.locator("#node-kind").is_disabled())
            self.assertIn(
                "Policy source", viewer.locator("#node-references").inner_text()
            )
            self.assertTrue(viewer.locator("#publish-book").is_hidden())
            self.assertTrue(viewer.locator("#add-node").is_hidden())
            self.assertTrue(viewer.locator("#delete-node").is_hidden())
            self.assertTrue(viewer.locator("#draw-edge").is_hidden())
            self.assertEqual(
                viewer.get_by_role(
                    "link", name="Settings and admins", exact=True
                ).count(),
                0,
            )
            viewer.get_by_role("button", name="Automatic layout", exact=True).click()
            viewer.get_by_role("button", name="Fit", exact=True).click()
            viewer.get_by_label("Find a concept", exact=True).fill("Disclosure")
            self.assertEqual(viewer.locator("#hierarchy-list .concept").count(), 1)
            viewer.get_by_role("button", name="● Disclosure", exact=True).click()
            self.assertEqual(viewer.locator("#node-name").input_value(), "Disclosure")
            self.assertEqual(posts, [])
            viewer.screenshot(path="/tmp/coding-read-only-codebook.png", full_page=True)
            self.assertEqual(errors, [])
            browser.close()
        target = CodingSession.objects.get(coder="Carol")
        self.assertFalse(target.assignment_set.get().annotation_set.exists())
        self.assertEqual(target.email, "carol@example.org")
        job = InvitationEmail.objects.get(session=target)
        self.assertIn(
            "Research project", invitation_cipher().decrypt(job.body.encode()).decode()
        )
