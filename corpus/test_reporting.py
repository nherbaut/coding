from .test_support import new_study
from .studies import publish_codebook
from copy import deepcopy
import csv
import io
import json
import uuid
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from .importers import put_document
from .models import (
    Annotation,
    Assignment,
    Case,
    CodingReport,
    MergeAudit,
    SharedCodebook,
)
from .reporting import capture_report, comparison_view, report_evidence
from .services import create_session, span, submit


def report_fixture():
    study = new_study()
    category, review, disclosure = [str(uuid.uuid4()) for _ in range(3)]
    book = dict(
        version=1,
        codes=[
            dict(
                id=category,
                kind="category",
                name="Social",
                parent=None,
                memo="Category evidence",
            ),
            dict(
                id=review,
                kind="code",
                name="Review",
                parent=category,
                memo="Review evidence",
                color="#ffcc00",
                references=[dict(title="Policy source", location="Section 1")],
            ),
            dict(
                id=disclosure,
                kind="code",
                name="Disclosure",
                parent=category,
                memo="Disclosure evidence",
            ),
        ],
    )
    publish_codebook(study, book=book)
    case = Case.objects.create(key="project", name="Research project", study=study)
    document = put_document(
        study,
        "policy",
        "AI_POLICY.md",
        "A😀 human review and disclosure\n" + "Uncoded policy text.\n" * 100,
        {"source": "https://example.org/policy"},
        [case],
    )
    alice, link = create_session(study, "Alice", [case])
    bob, _ = create_session(study, "Bob", [case])
    assignments = [Assignment.objects.get(session=s) for s in (alice, bob)]
    for assignment, specs in zip(
        assignments,
        [
            [(1, 9, [review, disclosure]), (4, 9, [disclosure])],
            [(1, 3, [review]), (4, 9, [review])],
        ],
    ):
        for start, end, codes in specs:
            Annotation.objects.create(
                assignment=assignment,
                document=document,
                start=start,
                end=end,
                selected_text=span(document.text, start, end),
                codes=codes,
                memo=assignment.session.coder + " evidence",
            )
    submissions = [submit(a.pk, a.session_id, 0) for a in assignments]
    admin = get_user_model().objects.create_superuser(
        "report-admin", "admin@example.org", "test-password"
    )
    study.owner = admin
    study.save(update_fields=["owner"])
    return dict(
        category=category,
        review=review,
        disclosure=disclosure,
        book=book,
        case=case,
        document=document,
        alice=alice,
        bob=bob,
        assignments=assignments,
        submissions=submissions,
        admin=admin,
        link=link,
        study=study,
    )


class ReportingTests(TestCase):

    def setUp(self):
        self.data = report_fixture()
        self.study = self.data["study"]
        self.client.force_login(self.data["admin"])
        self.study = self.data["study"]

    def capture(self, **filters):
        return capture_report(
            self.study, dict(source="submitted", mode="original", **filters)
        )

    def test_category_counts_deduplicate_multicoded_passages_and_keep_coders_separate(
        self,
    ):
        payload = self.capture()
        concepts, rows = report_evidence(payload)
        category = next(
            (c for c in concepts if c["code"]["id"] == self.data["category"])
        )
        review = next((c for c in concepts if c["code"]["id"] == self.data["review"]))
        self.assertEqual(len(rows), 4)
        self.assertEqual(category["direct_count"], 0)
        self.assertEqual(
            category["counts"],
            dict(passages=4, documents=1, document_versions=1, cases=1, coders=2),
        )
        self.assertEqual(review["direct_count"], 3)
        comparison = comparison_view(payload, rows, {})
        self.assertTrue(comparison["same_version"])
        self.assertIn(
            "Coded by one coder", [r["status"] for r in comparison["comparisons"]]
        )
        self.assertIn(
            "Different code identities",
            [r["status"] for r in comparison["comparisons"]],
        )
        self.assertEqual(comparison["comparisons"][0]["text"], "😀")
        self.assertEqual(
            "".join((s["text"] for s in comparison["panes"][0]["segments"])),
            self.data["document"].text,
        )

    def test_latest_submission_only_and_personal_book_snapshot(self):
        a = self.data["assignments"][0]
        Assignment.objects.filter(pk=a.pk).update(locked=False)
        a.annotation_set.update(memo="Resubmitted memo")
        submit(a.pk, a.session_id, 1)
        self.data["alice"].codebook["codes"][1]["name"] = "Later personal name"
        self.data["alice"].save()
        payload = self.capture()
        _, rows = report_evidence(payload)
        self.assertEqual(len(rows), 4)
        self.assertTrue(
            all(
                (e["memo"] == "Resubmitted memo" for e in rows if e["coder"] == "Alice")
            )
        )
        self.assertNotIn("Later personal name", json.dumps(payload))
        self.assertEqual(len(payload["records"]), 2)

    def test_drafts_are_explicit_and_filters_limit_the_scope(self):
        coder, _ = create_session(self.study, "Carol", [self.data["case"]])
        payload = self.capture()
        self.assertEqual(
            payload["missing"], [dict(coder="Carol", case="Research project")]
        )
        current = capture_report(
            self.study, dict(source="current", mode="original", coder=str(coder.pk))
        )
        self.assertEqual(len(current["records"]), 1)
        self.assertIsNone(current["records"][0]["submission"])
        self.assertEqual(current["records"][0]["source"], "current")
        self.assertEqual(
            len(self.capture(coder=str(self.data["alice"].pk))["records"]), 1
        )
        self.assertEqual(self.capture(document="not-in-scope")["records"], [])

    def test_submitted_documents_use_snapshot_versions_and_mismatch_is_explicit(self):
        doc = put_document(
            self.study,
            "policy",
            "AI_POLICY.md",
            "A😀 changed policy",
            {},
            [self.data["case"]],
        )
        alice = self.data["assignments"][0]
        alice.documents.set([doc])
        payload = self.capture()
        self.assertEqual(
            payload["records"][0]["documents"][0]["hash"], self.data["document"].sha256
        )
        bob = self.data["assignments"][1]
        bob.documents.set([doc])
        bob.annotation_set.all().delete()
        Assignment.objects.filter(pk=bob.pk).update(locked=False)
        submit(bob.pk, bob.session_id, 1)
        payload = self.capture()
        _, rows = report_evidence(payload)
        comparison = comparison_view(payload, rows, {})
        self.assertFalse(comparison["same_version"])
        self.assertEqual(comparison["comparisons"], [])
        response = self.client.get(
            f"/admin/studies/{self.study.pk}/reports/", {"view": "compare"}
        )
        self.assertContains(response, "Different document versions")

    def test_same_names_do_not_map_and_accepted_mappings_do(self):
        incoming = []
        for coder, assignment in zip(
            (self.data["alice"], self.data["bob"]), self.data["assignments"]
        ):
            identity = str(uuid.uuid4())
            incoming.append(identity)
            coder.codebook["codes"].append(
                dict(
                    id=identity,
                    kind="code",
                    name="Personal concept",
                    parent=None,
                    memo="Corpus evidence",
                )
            )
            coder.save()
            assignment.annotation_set.update(codes=[identity])
            Assignment.objects.filter(pk=assignment.pk).update(locked=False)
            submit(assignment.pk, coder.pk, 1)
        payload = capture_report(self.study, dict(source="submitted", mode="shared"))
        concepts, rows = report_evidence(payload)
        self.assertEqual(
            len([c for c in concepts if c["name"] == "Personal concept"]), 2
        )
        self.assertTrue(all((e["direct"][0].startswith("unmapped:") for e in rows)))
        alice_sub = self.data["assignments"][0].submission_set.latest("pk")
        shared = SharedCodebook.objects.latest("pk")
        MergeAudit.objects.create(
            submission=alice_sub,
            admin=self.data["admin"],
            previous=shared,
            result=shared,
            decisions={},
            mappings={incoming[0]: self.data["review"]},
        )
        concepts, rows = report_evidence(
            capture_report(self.study, dict(source="submitted", mode="shared"))
        )
        self.assertTrue(
            all(
                (
                    e["direct"] == ["shared:" + self.data["review"]]
                    for e in rows
                    if e["coder"] == "Alice"
                )
            )
        )
        self.assertTrue(
            all(
                (
                    e["direct"][0].startswith("unmapped:")
                    for e in rows
                    if e["coder"] == "Bob"
                )
            )
        )
        current = capture_report(self.study, dict(source="current", mode="shared"))
        self.assertTrue(all((not r["mappings"] for r in current["records"])))

    def test_report_pages_exports_and_saved_scope_are_frozen(self):
        for view in ("codes", "compare"):
            response = self.client.get(
                f"/admin/studies/{self.study.pk}/reports/", {"view": view}
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Alice")
            self.assertContains(response, "Bob")
        response = self.client.post(
            f"/admin/studies/{self.study.pk}/reports/",
            dict(name="Final evidence", source="submitted", mode="original"),
        )
        self.assertEqual(response.status_code, 302)
        saved = CodingReport.objects.get()
        before = deepcopy(saved.payload)
        self.data["assignments"][0].annotation_set.update(memo="Changed later")
        new_book = deepcopy(self.data["book"])
        new_book["codes"][1]["name"] = "Changed shared name"
        publish_codebook(self.study, book=new_book)
        response = self.client.get(response.url, {"format": "json"})
        data = json.loads(response.content)
        self.assertEqual(data["payload"], before)
        self.assertEqual(data["counts"]["passages"], 4)
        self.assertNotIn("Changed later", response.content.decode())
        self.assertNotIn("token_hash", response.content.decode())
        self.assertNotIn("email", response.content.decode())
        saved.name = "Mutation"
        with self.assertRaises(ValueError):
            saved.save()
        for format in ("csv", "summary-csv", "html"):
            export = self.client.get(
                f"/admin/studies/{self.study.pk}/reports/{saved.pk}/",
                {"format": format},
            )
            self.assertEqual(export.status_code, 200)
            self.assertIn("attachment", export["Content-Disposition"])
        csv_export = self.client.get(
            f"/admin/studies/{self.study.pk}/reports/{saved.pk}/",
            {"format": "csv"},
        )
        rows = list(csv.DictReader(io.StringIO(csv_export.content.decode())))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["document_hash"], self.data["document"].sha256)
        self.assertIn("😀", rows[0]["selected_text"])
        html = self.client.get(
            f"/admin/studies/{self.study.pk}/reports/{saved.pk}/",
            {"format": "html"},
        ).content.decode()
        self.assertNotIn('<link rel="stylesheet"', html)
        self.assertIn("Policy source", html)
        self.assertIn("Category evidence", html)
        self.assertEqual(
            self.client.post(
                f"/admin/studies/{self.study.pk}/reports/{saved.pk}/",
                {},
            ).status_code,
            400,
        )

    def test_concept_filter_and_csv_escaping(self):
        response = self.client.get(
            f"/admin/studies/{self.study.pk}/reports/",
            {"code": "original:" + self.data["review"], "format": "json"},
        )
        self.assertEqual(json.loads(response.content)["counts"]["passages"], 3)
        assignment = self.data["assignments"][0]
        assignment.annotation_set.update(
            memo='=HYPERLINK("https://example.org") <script>alert(1)</script>'
        )
        response = self.client.get(
            f"/admin/studies/{self.study.pk}/reports/",
            {"source": "current", "view": "compare"},
        )
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        response = self.client.get(
            f"/admin/studies/{self.study.pk}/reports/",
            {"source": "current", "format": "csv"},
        )
        rows = list(csv.DictReader(io.StringIO(response.content.decode())))
        self.assertTrue(
            next((r["memo"] for r in rows if r["coder"] == "Alice")).startswith(
                "'=HYPERLINK"
            )
        )

    def test_admin_only_including_coder_private_cookie(self):
        coder = Client()
        coder.post(
            "/access/exchange/",
            {"token": self.data["link"].split("#")[1]},
            content_type="application/json",
        )
        self.assertEqual(
            coder.get(f"/admin/studies/{self.study.pk}/reports/").status_code, 302
        )
        self.assertEqual(
            coder.post(
                f"/admin/studies/{self.study.pk}/reports/", {"name": "Unauthorized"}
            ).status_code,
            302,
        )
        self.assertEqual(CodingReport.objects.count(), 0)
        user = get_user_model().objects.create_user("regular", password="password")
        coder.force_login(user)
        self.assertEqual(
            coder.get(f"/admin/studies/{self.study.pk}/reports/").status_code, 302
        )
        with override_settings(
            AUTHELIA_AUTH_ENABLED=True,
            AUTHELIA_TRUSTED_PROXIES=["192.168.1.60/32"],
            AUTHELIA_PROXY_SECRET="test-secret-with-at-least-thirty-two-characters",
        ):
            self.assertEqual(
                self.client.get(f"/admin/studies/{self.study.pk}/reports/").status_code,
                403,
            )

    def test_personal_names_and_moves_remain_on_their_own_excerpts(self):
        alice = self.data["alice"]
        alice.codebook["codes"][1].update(
            name="Maintainer capacity", parent=None, memo="Alice source quote"
        )
        alice.save()
        self.data["assignments"][0].annotation_set.update(memo="Alice memo")
        payload = capture_report(self.study, dict(source="current", mode="original"))
        concepts, rows = report_evidence(payload)
        review = next((c for c in concepts if c["code"]["id"] == self.data["review"]))
        self.assertTrue(review["variants"])
        for row in rows:
            names = {c["name"] for c in row["codes"]}
            if self.data["review"] in row["original_codes"]:
                self.assertIn(
                    "Maintainer capacity" if row["coder"] == "Alice" else "Review",
                    names,
                )
        category = next(
            (c for c in concepts if c["code"]["id"] == self.data["category"])
        )
        self.assertEqual(category["counts"]["passages"], 4)

    def test_saved_report_survives_serialization_and_later_mapping_changes(self):
        from django.core import serializers

        response = self.client.post(
            f"/admin/studies/{self.study.pk}/reports/",
            dict(name="Before review", source="submitted", mode="shared"),
        )
        saved = CodingReport.objects.get()
        original = deepcopy(saved.payload)
        shared = SharedCodebook.objects.latest("pk")
        MergeAudit.objects.create(
            submission=self.data["submissions"][0],
            admin=self.data["admin"],
            previous=shared,
            result=shared,
            decisions={},
            mappings={self.data["review"]: self.data["disclosure"]},
        )
        restored = next(
            serializers.deserialize("json", serializers.serialize("json", [saved]))
        ).object
        self.assertEqual(restored.payload, original)
        saved_json = json.loads(
            self.client.get(response.url, {"format": "json"}).content
        )
        self.assertEqual(saved_json["payload"], original)
        self.assertTrue(
            all((not r["mappings"] for r in saved_json["payload"]["records"]))
        )

    def test_empty_scope_and_invalid_inputs(self):
        case = Case.objects.create(key="empty", name="Empty case", study=self.study)
        self.assertContains(
            self.client.get(
                f"/admin/studies/{self.study.pk}/reports/", {"case": case.pk}
            ),
            "No coding versions match",
        )
        self.assertEqual(
            self.client.get(
                f"/admin/studies/{self.study.pk}/reports/", {"source": "invalid"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                f"/admin/studies/{self.study.pk}/reports/", {"coder": "not-a-uuid"}
            ).status_code,
            400,
        )
