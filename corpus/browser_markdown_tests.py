"""Browser selection and source-provenance checks for rendered Markdown."""

import os
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from .importers import put_document
from .models import Case, SharedCodebook
from .services import create_session


class MarkdownBrowserTests(StaticLiveServerTestCase):
    def test_formatted_coding_unicode_source_toggle_review_and_portable_report(self):
        from playwright.sync_api import sync_playwright

        code = str(uuid.uuid4())
        SharedCodebook.objects.create(
            content=dict(
                version=1,
                codes=[
                    dict(
                        id=code,
                        kind="code",
                        name="Review",
                        parent=None,
                        memo="Source evidence",
                        color="#f08080",
                    )
                ],
            )
        )
        case = Case.objects.create(key="markdown", name="Markdown project")
        source = """# Sustainability policy

Contributors **must review 😀** and [disclose use](https://example.org/policy) &amp; keep \\*context\\*.

- First requirement
- Second requirement

> Human review
> remains necessary.

| Rule | Required |
| --- | --- |
| Review | Yes |

```js
const count = 1;
```

<div><h2>HTML section</h2><p>Safe <strong>HTML</strong> &amp; text.</p><script>window.markdownAttack=true</script><img src=x onerror="window.markdownAttack=true"></div>

[Unsafe](javascript:alert(1))
"""
        document = put_document("markdown", "POLICY.md", source, {}, [case])
        _, link = create_session("Markdown coder", [case])
        get_user_model().objects.create_superuser(
            "markdown-admin", "admin@example.org", "test-password"
        )
        with sync_playwright() as playwright:
            options = {"headless": True}
            if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
                options["executable_path"] = os.environ[
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE"
                ]
            browser = playwright.chromium.launch(**options)
            coder = browser.new_page(viewport=dict(width=1500, height=1000))
            errors = []
            coder.on("pageerror", lambda error: errors.append(str(error)))
            coder.goto(self.live_server_url + link)
            coder.locator("#document h1").wait_for()
            self.assertEqual(
                coder.locator("#document h1").inner_text(), "Sustainability policy"
            )
            self.assertEqual(coder.locator("#document li").count(), 2)
            self.assertEqual(coder.locator("#document table").count(), 1)
            self.assertEqual(
                coder.locator("#document pre code").inner_text(), "const count = 1;"
            )
            self.assertEqual(coder.locator("#document h2").inner_text(), "HTML section")
            self.assertEqual(coder.locator("#document script,#document img").count(), 0)
            self.assertFalse(coder.evaluate("Boolean(window.markdownAttack)"))
            self.assertIsNone(
                coder.locator("#document a")
                .filter(has_text="Unsafe")
                .get_attribute("href")
            )

            def save_selection(script):
                coder.locator("#document").evaluate(script)
                coder.locator("#code-tree").get_by_label("Review", exact=True).check()
                coder.get_by_role("button", name="Save annotation", exact=True).click()
                coder.wait_for_function(
                    "document.querySelector('#save-annotation').disabled"
                )
                return coder.evaluate("fetch('/api/state/').then(r=>r.json())")[
                    "assignments"
                ][0]["annotations"]

            saved = save_selection(
                """root=>{const node=root.querySelector('strong').firstChild;const selection=window.getSelection();selection.setBaseAndExtent(node,0,node,node.length);root.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));}"""
            )
            self.assertEqual(saved[0]["selected_text"], "must review 😀")
            self.assertEqual(
                saved[0]["start"],
                len(source[: source.index("must review")].encode("utf-16-le")) // 2,
            )
            self.assertEqual(saved[0]["document_hash"], document.sha256)
            saved = save_selection(
                """root=>{const first=root.querySelector('strong mark').firstChild,last=root.querySelector('a').firstChild;window.getSelection().setBaseAndExtent(first,0,last,8);root.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));}"""
            )
            self.assertEqual(len(saved), 2)
            self.assertTrue(
                any("** and [disclose" in a["selected_text"] for a in saved)
            )
            self.assertNotIn("**", coder.locator("#annotations").inner_text())
            saved = save_selection(
                """root=>{const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let node;while(node=walker.nextNode()){if(node.textContent.includes(' & keep'))break;}const start=node.textContent.indexOf('&');window.getSelection().setBaseAndExtent(node,start,node,start+1);root.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));}"""
            )
            self.assertTrue(any(a["selected_text"] == "&amp;" for a in saved))
            coder.get_by_label("Reading view", exact=True).select_option("source")
            self.assertIn(
                "# Sustainability policy", coder.locator("#document").inner_text()
            )
            self.assertEqual(coder.locator("#document h1").count(), 0)
            coder.get_by_label("Reading view", exact=True).select_option("rendered")
            self.assertEqual(coder.locator("#document h1").count(), 1)
            coder.reload()
            coder.locator("#document h1").wait_for()
            self.assertGreater(coder.locator("#document strong mark").count(), 0)
            self.assertEqual(
                len(
                    coder.evaluate("fetch('/api/state/').then(r=>r.json())")[
                        "assignments"
                    ][0]["annotations"]
                ),
                3,
            )
            coder.once("dialog", lambda dialog: dialog.accept())
            coder.get_by_role("button", name="Submit case", exact=True).click()
            coder.get_by_role("button", name="Unsubmit case", exact=True).wait_for()
            admin = browser.new_page()
            admin.on("pageerror", lambda error: errors.append(str(error)))
            admin.goto(self.live_server_url + "/admin/login/?next=/")
            admin.get_by_label("Username:").fill("markdown-admin")
            admin.get_by_label("Password:").fill("test-password")
            admin.get_by_role("button", name="Log in", exact=True).click()
            admin.get_by_role("link", name="Review submission", exact=True).click()
            admin.locator(".reading-text h1").wait_for(state="attached")
            self.assertGreater(admin.locator(".fulltext mark").count(), 0)
            self.assertNotIn(
                "**", admin.locator("article.annotation").all_text_contents()[1]
            )
            admin.goto(self.live_server_url + "/review/reports/?view=compare")
            admin.locator(".comparison-text h1").wait_for()
            self.assertGreater(admin.locator(".comparison-text mark").count(), 0)
            with admin.expect_download() as info:
                admin.get_by_role("link", name="Download HTML", exact=True).click()
            exported = Path("/tmp/coding-markdown-report.html")
            exported.write_text(Path(info.value.path()).read_text())
            offline = browser.new_page()
            offline.on("pageerror", lambda error: errors.append(str(error)))
            offline.goto(exported.as_uri())
            offline.locator(".comparison-text h1").wait_for()
            self.assertFalse(offline.evaluate("Boolean(window.markdownAttack)"))
            coder.screenshot(path="/tmp/coding-markdown-rendered.png", full_page=False)
            self.assertEqual(errors, [])
            browser.close()
