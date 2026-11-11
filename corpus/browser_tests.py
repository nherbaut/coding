from .test_support import new_study
from .studies import publish_codebook

"""Optional real-browser workflow. Run manage.py test corpus.browser_tests."""
import os
import uuid
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from .importers import put_document
from .models import Case, SharedCodebook


class BrowserWorkflowTests(StaticLiveServerTestCase):

    def test_assignment_to_codebook_merge(self):
        self.study = new_study()
        from playwright.sync_api import sync_playwright

        publish_codebook(
            self.study,
            book=dict(
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
            ),
            expected_revision=(self.study)
            .__class__.objects.get(pk=(self.study).pk)
            .codebook_id,
        )
        book = SharedCodebook.objects.order_by("-pk").first()
        book.content["codes"].extend(
            (
                dict(
                    id=str(uuid.uuid4()),
                    kind="code",
                    name=f"Taxonomy leaf {index}",
                    parent=None,
                )
                for index in range(70)
            )
        )
        names = [
            "Zulu category",
            "Alpha category",
            "Zeta code",
            "Alpha parent",
            "Child code",
            "beta code",
            "apple code",
        ]
        term_ids = {name: str(uuid.uuid4()) for name in names}
        book.content["codes"].extend(
            [
                dict(
                    id=term_ids["Child code"],
                    kind="code",
                    name="Child code",
                    parent=term_ids["Alpha parent"],
                ),
                dict(
                    id=term_ids["Zulu category"],
                    kind="category",
                    name="Zulu category",
                    parent=None,
                ),
                dict(
                    id=term_ids["Zeta code"],
                    kind="code",
                    name="Zeta code",
                    parent=term_ids["Zulu category"],
                ),
                dict(
                    id=term_ids["beta code"],
                    kind="code",
                    name="beta code",
                    parent=term_ids["Alpha category"],
                ),
                dict(
                    id=term_ids["Alpha category"],
                    kind="category",
                    name="Alpha category",
                    parent=None,
                ),
                dict(
                    id=term_ids["Alpha parent"],
                    kind="code",
                    name="Alpha parent",
                    parent=term_ids["Zulu category"],
                ),
                dict(
                    id=term_ids["apple code"],
                    kind="code",
                    name="apple code",
                    parent=term_ids["Alpha category"],
                ),
            ]
        )
        publish_codebook(
            self.study,
            book=book.content,
            expected_revision=(self.study)
            .__class__.objects.get(pk=(self.study).pk)
            .codebook_id,
        )
        case = Case.objects.create(
            key="browser", name="Example OSS project", study=self.study
        )
        put_document(
            self.study,
            "browser-document",
            "AI_POLICY.md",
            "Contributors must disclose AI use. Human review is required. 😀",
            {"repository": "example/project"},
            [case],
        )
        get_user_model().objects.create_superuser(
            "test-admin", "test@example.org", "test-password"
        )
        self.study.owner = get_user_model().objects.get(username="test-admin")
        self.study.save(update_fields=["owner"])
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
            admin.goto(
                self.live_server_url
                + ("/admin/login/?next=" + f"/admin/studies/{self.study.pk}/")
            )
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
                "node => {\n                const range = document.createRange();\n                const text = document.createTreeWalker(node, NodeFilter.SHOW_TEXT).nextNode();\n                range.setStart(text, 0);\n                range.setEnd(text, 12);\n                const selection = window.getSelection();\n                selection.removeAllRanges(); selection.addRange(range);\n                node.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));\n            }"
            )
            self.assertEqual(
                coder.evaluate("window.getSelection().toString()"), "Contributors"
            )
            self.assertTrue(coder.locator("#selection").is_hidden())
            self.assertEqual(coder.locator("#code-tree input:checked").count(), 0)
            self.assertTrue(coder.locator("#save-annotation").is_disabled())
            coder.get_by_role("button", name="Add code", exact=True).click()
            options = coder.locator("#code-parent option").evaluate_all(
                "nodes => nodes.map(node => ({id:node.value,label:node.textContent,path:node.title}))"
            )
            expected = [
                "Alpha category",
                "apple code",
                "beta code",
                "Zulu category",
                "Alpha parent",
                "Child code",
                "Zeta code",
            ]
            actual = [
                option["id"] for option in options if option["id"] in term_ids.values()
            ]
            self.assertEqual(actual, [term_ids[name] for name in expected])
            child_option = next(
                (option for option in options if option["id"] == term_ids["Child code"])
            )
            self.assertTrue(child_option["label"].startswith("\xa0" * 4))
            self.assertEqual(
                child_option["path"], "Zulu category › Alpha parent › Child code"
            )
            self.assertIn(
                "(Category)",
                next(
                    (
                        option["label"]
                        for option in options
                        if option["id"] == term_ids["Zulu category"]
                    )
                ),
            )
            labels = coder.locator("#code-tree .code-row label").all_text_contents()
            self.assertEqual([name for name in labels if name in term_ids], expected)
            coder.locator("#close-code").click()
            coder.get_by_role("button", name="Edit Alpha parent", exact=True).click()
            candidates = coder.locator("#code-parent option").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            )
            self.assertNotIn(term_ids["Alpha parent"], candidates)
            self.assertNotIn(term_ids["Child code"], candidates)
            self.assertEqual(
                coder.locator("#code-parent").input_value(), term_ids["Zulu category"]
            )
            coder.locator("#close-code").click()
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
            coder.get_by_label("Find a code").fill("Human")
            coder.get_by_label("Annotation memo", exact=True).fill("Coding evidence")
            coder.get_by_role("button", name="Save annotation", exact=True).click()
            coder.locator("#annotations").get_by_text(
                "Coding evidence", exact=True
            ).wait_for()
            self.assertEqual(
                coder.locator("#document mark.annotation-preview").count(), 0
            )
            self.assertEqual(coder.locator("#code-tree input:checked").count(), 0)
            self.assertTrue(coder.locator("#code-tree input").first.is_disabled())
            coder.locator("#document").evaluate(
                "root=>{\n                const text=root.querySelector('mark').firstChild;\n                const selection=window.getSelection();\n                selection.setBaseAndExtent(text,4,text,0);\n                root.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));\n            }"
            )
            self.assertEqual(coder.evaluate("window.getSelection().toString()"), "Cont")
            self.assertEqual(coder.locator("#code-tree input:checked").count(), 0)
            self.assertTrue(coder.locator("#selection").is_hidden())
            coder.locator("#cancel-edit").click()
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
            state = coder.evaluate(
                "fetch('/api/state/').then(response => response.json())"
            )
            assignment_id = state["assignments"][0]["id"]
            version = state["assignments"][0]["documents"][0]
            original = next(
                (
                    code
                    for code in state["codebook"]["codes"]
                    if code["name"] == "Human review"
                )
            )
            result = coder.evaluate(
                "async data => {\n                const response = await fetch(`/api/assignments/${data.assignment}/annotations/`, {\n                    method:'POST', headers:{'Content-Type':'application/json', 'X-CSRFToken':document.querySelector('[name=csrfmiddlewaretoken]').value},\n                    body:JSON.stringify(data.annotation)\n                }); return response.status;\n            }",
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
            coder.locator("#document mark").first.click(button="right")
            coder.locator("#annotation-context-menu").wait_for(state="visible")
            self.assertEqual(
                coder.locator("#annotation-context-menu [role=group]").count(), 2
            )
            coder.locator("#annotation-context-menu").get_by_role(
                "menuitem", name="Edit memo", exact=True
            ).first.click()
            self.assertEqual(coder.locator("#memo-dialog textarea").count(), 1)
            coder.locator("#close-memos").click()
            coder.locator("#document mark").first.focus()
            coder.keyboard.press("Shift+F10")
            coder.locator("#annotation-context-menu").wait_for(state="visible")
            coder.keyboard.press("Escape")
            self.assertTrue(coder.locator("#annotation-context-menu").is_hidden())
            before = coder.evaluate("fetch('/api/state/').then(r => r.json())")[
                "assignments"
            ][0]["annotations"]
            coder.locator("#document mark").first.click()
            self.assertEqual(coder.locator("#memo-dialog textarea").count(), 2)
            memo_field = coder.locator("#memo-dialog textarea").first
            memo_field.fill("Memo edited from highlighted passage")
            coder.locator("#memo-dialog").get_by_role(
                "button", name="Save memo", exact=True
            ).first.click()
            coder.locator("#memo-dialog").get_by_text(
                "Memo saved", exact=True
            ).wait_for()
            after = coder.evaluate("fetch('/api/state/').then(r => r.json())")[
                "assignments"
            ][0]["annotations"]
            changed = next(
                (
                    item
                    for item in after
                    if item["memo"] == "Memo edited from highlighted passage"
                )
            )
            original = next((item for item in before if item["id"] == changed["id"]))
            self.assertEqual(changed["codes"], original["codes"])
            self.assertEqual(
                (changed["start"], changed["end"]), (original["start"], original["end"])
            )
            coder.locator("#close-memos").click()
            self.assertTrue(
                coder.get_by_role("button", name="Submit case", exact=True).is_enabled()
            )
            coder.reload()
            coder.locator("#annotations").get_by_text(
                "Memo edited from highlighted passage", exact=True
            ).wait_for()
            coder.locator("#annotations").get_by_role(
                "button", name="Edit memo", exact=True
            ).first.click()
            self.assertEqual(coder.locator("#memo-dialog textarea").count(), 1)
            coder.locator("#close-memos").click()
            coder.locator("#annotations").get_by_role(
                "button", name="Edit", exact=True
            ).first.click()
            coder.get_by_label("Annotation memo", exact=True).fill("Revised evidence")
            coder.locator("#annotations").get_by_text(
                "Revised evidence", exact=True
            ).wait_for()
            current = coder.evaluate("fetch('/api/state/').then(r => r.json())")
            human_id = next(
                (
                    code["id"]
                    for code in current["codebook"]["codes"]
                    if code["name"] == "Human review"
                )
            )
            deleted_id = next(
                (
                    item["id"]
                    for item in current["assignments"][0]["annotations"]
                    if human_id in item["codes"]
                )
            )
            kept_id = next(
                (
                    item["id"]
                    for item in current["assignments"][0]["annotations"]
                    if item["id"] != deleted_id
                )
            )
            coder.locator("#document mark").first.click(button="right")
            coder.locator(
                f'#annotation-context-menu [data-annotation-id="{deleted_id}"]'
            ).get_by_role("menuitem", name="Delete annotation", exact=True).click()
            coder.wait_for_function(
                "document.querySelector('#annotation-count').textContent === '1 saved annotation'"
            )
            coder.reload()
            coder.wait_for_function(
                "document.querySelector('#document-title').textContent.length>0"
            )
            remaining = coder.evaluate("fetch('/api/state/').then(r => r.json())")[
                "assignments"
            ][0]["annotations"]
            self.assertEqual([item["id"] for item in remaining], [kept_id])
            self.assertGreater(coder.locator("#document mark").count(), 0)
            duplicate = coder.evaluate(
                "async ()=>{\n                const state=await fetch('/api/state/').then(r=>r.json());\n                const owner=state.assignments[0];\n                const {id,...annotation}=owner.annotations[0];\n                const response=await fetch(`/api/assignments/${owner.id}/annotations/`,{\n                    method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':document.querySelector('[name=csrfmiddlewaretoken]').value},\n                    body:JSON.stringify({...annotation,revision:owner.revision,codebook_revision:state.revision,memo:'Delete from memo modal'})\n                });\n                if(!response.ok)throw Error('Unable to create modal-delete fixture');\n                return (await response.json()).annotations.find(a=>a.memo==='Delete from memo modal');\n            }"
            )
            coder.reload()
            coder.locator("#document mark").first.click()
            coder.locator(
                f"""#memo-dialog [data-annotation-id="{duplicate['id']}"]"""
            ).get_by_role("button", name="Delete annotation", exact=True).click()
            coder.locator(
                f"""#memo-dialog [data-annotation-id="{duplicate['id']}"]"""
            ).wait_for(state="detached")
            self.assertTrue(coder.locator("#memo-dialog").is_visible())
            self.assertEqual(coder.locator("#memo-dialog .memo-editor").count(), 1)
            coder.locator("#close-memos").click()
            remaining = coder.evaluate("fetch('/api/state/').then(r => r.json())")[
                "assignments"
            ][0]["annotations"]
            self.assertEqual([item["id"] for item in remaining], [kept_id])
            coder.once("dialog", lambda dialog: dialog.accept())
            coder.get_by_role("button", name="Add code", exact=True).click()
            coder.get_by_label("Name", exact=True).fill("Additional review concept")
            coder.get_by_role("button", name="Save concept", exact=True).click()
            coder.wait_for_function("!document.querySelector('#code-dialog').open")
            coder.get_by_role("button", name="Submit case", exact=True).click()
            coder.wait_for_function(
                "document.querySelector('#save-status').textContent === 'Case submitted'"
            )
            coder.wait_for_function(
                "document.querySelector('#annotation-guidance').textContent.includes('Submitted case')"
            )
            coder.locator("#document mark").first.click(button="right")
            coder.locator("#annotation-context-menu").wait_for(state="visible")
            self.assertTrue(
                coder.locator("#annotation-context-menu")
                .get_by_role("menuitem", name="Delete annotation", exact=True)
                .is_disabled()
            )
            self.assertTrue(
                coder.locator("#annotation-context-menu")
                .get_by_role("menuitem", name="Edit memo", exact=True)
                .is_disabled()
            )
            coder.keyboard.press("Escape")
            coder.locator("#document mark").first.click()
            coder.locator("#memo-dialog[open]").wait_for()
            self.assertTrue(
                coder.locator("#memo-dialog textarea").first.evaluate(
                    "node => node.readOnly"
                )
            )
            self.assertTrue(
                coder.locator("#memo-dialog")
                .get_by_role("button", name="Save memo", exact=True)
                .first.is_disabled()
            )
            self.assertTrue(
                coder.locator("#memo-dialog")
                .get_by_role("button", name="Delete annotation", exact=True)
                .first.is_disabled()
            )
            coder.locator("#close-memos").click()
            admin.goto(self.live_server_url + f"/admin/studies/{self.study.pk}/")
            admin.get_by_role(
                "link", name="Review submission", exact=False
            ).first.click()
            decisions = admin.locator("select[name^=action_]")
            decisions.first.wait_for()
            count = decisions.count()
            self.assertGreater(count, 1)
            decisions.first.select_option("reject")
            admin.get_by_role("button", name="Accept all proposals", exact=True).click()
            self.assertEqual(
                decisions.evaluate_all("nodes => nodes.map(n => n.value)"),
                ["accept"] * count,
            )
            self.assertTrue(
                admin.get_by_role("button", name="Apply review decisions").is_visible()
            )
            self.assertFalse(
                admin.get_by_text(
                    "Codebook review completed.", exact=False
                ).is_visible()
            )
            decisions.first.select_option("reject")
            self.assertEqual(decisions.first.input_value(), "reject")
            admin.get_by_role("button", name="Accept all proposals", exact=True).click()
            admin.get_by_role("button", name="Apply review decisions").click()
            admin.get_by_text("Codebook review completed.", exact=False).wait_for()
            self.assertGreater(admin.locator(".fulltext mark").count(), 0)
            self.assertEqual(errors, [])
            browser.close()
