from .test_support import new_study
from .studies import publish_codebook
from copy import deepcopy
import hashlib
import io
import json
import uuid
import zipfile
import yaml
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, Client
from .importers import import_zip, put_document
from .models import (
    Annotation,
    Assignment,
    Case,
    CodingSession,
    DocumentVersion,
    SharedCodebook,
    Submission,
)
from .services import (
    Conflict,
    create_session,
    differences,
    load_book,
    merge_submission,
    span,
    submit,
)


class WorkflowTests(TestCase):

    def setUp(self):
        self.study = new_study()
        self.identity = str(uuid.uuid4())
        self.book = dict(
            version=1,
            codes=[
                dict(
                    id=self.identity,
                    name="Review",
                    kind="code",
                    parent=None,
                    memo="Quoted evidence",
                    references=[],
                )
            ],
        )
        publish_codebook(self.study, book=self.book)
        self.case = Case.objects.create(key="case", name="Project", study=self.study)
        self.document = put_document(
            self.study,
            "doc",
            "Policy",
            "A😀 human review\n",
            {"source": "AI.md"},
            [self.case],
        )
        self.alice, self.link = create_session(self.study, "Alice", [self.case])
        self.bob, self.bob_link = create_session(self.study, "Bob", [self.case])
        self.assignment = Assignment.objects.get(session=self.alice)
        self.admin = get_user_model().objects.create_superuser(
            "admin", "admin@example.org", "password"
        )
        self.study.owner = self.admin
        self.study.save(update_fields=["owner"])
        self.client = Client()
        self.enter(self.client, self.link)

    def enter(self, client, link):
        return client.post(
            "/access/exchange/",
            json.dumps({"token": link.split("#")[1]}),
            content_type="application/json",
        )

    def post(self, url, body, client=None):
        return (client or self.client).post(
            url, json.dumps(body), content_type="application/json"
        )

    def annotate(self, **overrides):
        body = dict(
            revision=0,
            codebook_revision=0,
            document=self.document.pk,
            document_hash=self.document.sha256,
            start=1,
            end=3,
            selected_text="😀",
            codes=[self.identity],
            memo="Evidence",
        )
        body.update(overrides)
        return self.post(f"/api/assignments/{self.assignment.pk}/annotations/", body)

    def test_isolation_unicode_overlap_and_stale_save(self):
        self.assertEqual(self.annotate().status_code, 200)
        self.assertEqual(self.annotate().status_code, 409)
        self.assertEqual(
            self.annotate(revision=1, start=0, end=3, selected_text="A😀").status_code,
            200,
        )
        self.assertEqual(Annotation.objects.count(), 2)
        bob = Client()
        self.enter(bob, self.bob_link)
        state = bob.get("/api/state/").json()
        self.assertEqual(state["assignments"][0]["annotations"], [])
        self.assertEqual(
            self.post(
                f"/api/assignments/{self.assignment.pk}/annotations/",
                {"revision": 2},
                bob,
            ).status_code,
            404,
        )
        self.assertEqual(self.annotate(revision=2, start=2).status_code, 400)
        self.assertEqual(
            self.annotate(revision=2, document_hash="wrong").status_code, 400
        )

    def test_submission_locks_snapshot_and_reopen(self):
        self.annotate()
        response = self.post(
            f"/api/assignments/{self.assignment.pk}/submit/", {"revision": 1}
        )
        self.assertEqual(response.status_code, 200)
        submission = Submission.objects.get()
        self.assertEqual(self.annotate(revision=2).status_code, 409)
        book = deepcopy(self.book)
        book["codes"][0]["name"] = "Renamed"
        self.assertEqual(
            self.post("/api/codebook/", {"revision": 0, "codebook": book}).status_code,
            200,
        )
        submission.refresh_from_db()
        self.assertEqual(submission.snapshot["codebook"]["codes"][0]["name"], "Review")
        admin = Client()
        admin.force_login(self.admin)
        self.assertEqual(
            admin.post(
                f"/admin/studies/{self.study.pk}/",
                {"action": "reopen", "assignment": self.assignment.pk},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.annotate(revision=3, codebook_revision=1).status_code, 200
        )
        self.assertEqual(
            self.post(
                f"/api/assignments/{self.assignment.pk}/submit/", {"revision": 4}
            ).status_code,
            200,
        )
        self.assertEqual(Submission.objects.count(), 2)

    def test_revoke_and_csrf(self):
        admin = Client()
        admin.force_login(self.admin)
        admin.post(
            f"/admin/studies/{self.study.pk}/",
            {"action": "revoke", "session": self.alice.pk},
        )
        self.assertEqual(self.client.get("/api/state/").status_code, 404)
        self.assertEqual(self.enter(Client(), self.link).status_code, 404)
        csrf = Client(enforce_csrf_checks=True)
        self.assertEqual(
            csrf.post(
                "/access/exchange/",
                json.dumps({"token": "x"}),
                content_type="application/json",
            ).status_code,
            403,
        )

    def test_frozen_document_versions_yaml_and_references(self):
        changed = put_document(self.study, "doc", "Policy", "New text", {}, [self.case])
        self.assertNotEqual(changed.pk, self.document.pk)
        self.assertEqual(
            list(self.assignment.documents.values_list("pk", flat=True)),
            [self.document.pk],
        )
        exported = self.client.get("/work/codebook.yaml").content.decode()
        from .colors import colored_book

        self.assertEqual(load_book(exported), colored_book(self.book))
        book = deepcopy(self.book)
        book["codes"][0]["references"] = [
            dict(
                type="corpus",
                document=self.document.pk,
                hash=self.document.sha256,
                start=1,
                end=3,
                quote="😀",
            )
        ]
        self.assertEqual(
            self.post("/api/codebook/", {"revision": 0, "codebook": book}).status_code,
            200,
        )
        self.assertEqual(
            self.post("/api/codebook/", {"revision": 0, "codebook": book}).status_code,
            409,
        )
        book["codes"][0]["references"][0]["quote"] = "wrong"
        self.assertEqual(
            self.post("/api/codebook/", {"revision": 1, "codebook": book}).status_code,
            400,
        )

    def test_referenced_concept_deletion_and_archive(self):
        self.annotate()
        self.assertEqual(
            self.post(
                "/api/codebook/", {"revision": 0, "codebook": dict(version=1, codes=[])}
            ).status_code,
            400,
        )
        book = deepcopy(self.book)
        book["codes"][0]["archived"] = True
        self.assertEqual(
            self.post("/api/codebook/", {"revision": 0, "codebook": book}).status_code,
            200,
        )
        annotation = Annotation.objects.get()
        self.assertEqual(
            self.annotate(
                revision=1,
                codebook_revision=1,
                id=str(annotation.pk),
                memo="Archived history",
            ).status_code,
            200,
        )
        self.assertEqual(
            self.annotate(revision=2, codebook_revision=1).status_code, 400
        )

    def make_submission(self, book):
        self.alice.codebook = book
        self.alice.save()
        return submit(self.assignment.pk, self.alice.pk, 0)

    def test_three_way_merge_conflict_mapping_and_idempotency(self):
        proposal = deepcopy(self.book)
        proposal["codes"][0]["name"] = "Alice name"
        extra = str(uuid.uuid4())
        proposal["codes"].append(
            dict(id=extra, name="Extra", kind="code", parent=None, memo="")
        )
        submission = self.make_submission(proposal)
        current = deepcopy(self.book)
        current["codes"][0]["name"] = "Admin name"
        shared = publish_codebook(self.study, book=current)
        self.assertTrue(
            next(
                (c for c in differences(submission, shared) if c["id"] == self.identity)
            )["conflict"]
        )
        audit = merge_submission(
            submission.pk,
            self.admin,
            shared.pk,
            {
                self.identity: {"action": "reject"},
                extra: {"action": "map", "target": self.identity},
            },
        )
        self.assertEqual(audit.mappings[extra], self.identity)
        self.assertEqual(len(audit.result.content["codes"]), 1)
        with self.assertRaises(Conflict):
            merge_submission(submission.pk, self.admin, audit.result_id, {})
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.codebook, self.book)

    def test_hierarchy_and_duplicate_merge_fail_atomically(self):
        proposal = deepcopy(self.book)
        extra = str(uuid.uuid4())
        proposal["codes"].append(
            dict(id=extra, name="Another", kind="code", parent=None, memo="")
        )
        submission = self.make_submission(proposal)
        current = deepcopy(self.book)
        current["codes"][0]["name"] = "Another"
        shared = publish_codebook(self.study, book=current)
        count = SharedCodebook.objects.count()
        with self.assertRaises(ValueError):
            merge_submission(
                submission.pk, self.admin, shared.pk, {extra: {"action": "accept"}}
            )
        self.assertEqual(SharedCodebook.objects.count(), count)
        with self.assertRaises(Conflict):
            merge_submission(
                submission.pk, self.admin, shared.pk - 1, {extra: {"action": "reject"}}
            )

    def test_admin_review_exports_and_database_roundtrip(self):
        self.annotate()
        submission = submit(self.assignment.pk, self.alice.pk, 1)
        admin = Client()
        admin.force_login(self.admin)
        self.assertContains(admin.get(f"/admin/studies/{self.study.pk}/"), "Alice")
        self.assertContains(
            admin.get(f"/admin/studies/{self.study.pk}/reviews/{submission.pk}/"),
            "😀",
        )
        self.assertEqual(
            admin.post(
                f"/admin/studies/{self.study.pk}/reviews/{submission.pk}/",
                {"revision": self.alice.base_id},
            ).status_code,
            302,
        )
        result = admin.get(
            f"/admin/studies/{self.study.pk}/exports/submissions/{submission.pk}/"
        ).json()
        self.assertEqual(result["results"][0]["document_hash"], self.document.sha256)
        self.assertEqual(result["results"][0]["shared_codes"], [self.identity])
        self.assertIn(
            "selected_text",
            admin.get(
                f"/admin/studies/{self.study.pk}/exports/submissions/{submission.pk}/?format=csv"
            ).content.decode(),
        )
        self.assertEqual(
            self.client.get(
                f"/admin/studies/{self.study.pk}/reviews/{submission.pk}/"
            ).status_code,
            302,
        )
        stream = io.StringIO()
        call_command("dumpdata", "corpus", stdout=stream)
        self.assertIn(self.document.sha256, stream.getvalue())

    def test_zip_import_is_repeatable(self):
        archive = io.BytesIO()
        manifest = dict(
            version=1,
            cases=[dict(id="other", name="Other")],
            documents=[
                dict(
                    id="other-doc", name="Other text", path="text.txt", cases=["other"]
                )
            ],
        )
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("manifest.yaml", yaml.safe_dump(manifest))
            z.writestr("text.txt", "Hello")
        archive.seek(0)
        import_zip(self.study, archive)
        archive.seek(0)
        import_zip(self.study, archive)
        self.assertEqual(
            DocumentVersion.objects.filter(document__key="other-doc").count(), 1
        )

    def test_codebook_moves_and_deletion_merge(self):
        category = str(uuid.uuid4())
        original = deepcopy(self.book)
        original["codes"].append(
            dict(id=category, name="Group", kind="category", parent=None, memo="")
        )
        base = publish_codebook(self.study, book=original)
        self.alice.base = base
        self.alice.save()
        proposal = deepcopy(original)
        proposal["codes"][0]["parent"] = category
        submission = self.make_submission(proposal)
        audit = merge_submission(
            submission.pk, self.admin, base.pk, {self.identity: {"action": "accept"}}
        )
        self.assertEqual(audit.result.content["codes"][0]["parent"], category)
        other, link = create_session(self.study, "Next", [self.case])
        a = Assignment.objects.get(session=other)
        other.codebook = deepcopy(other.codebook)
        other.codebook["codes"] = []
        other.save()
        deletion = submit(a.pk, other.pk, 0)
        audit = merge_submission(
            deletion.pk,
            self.admin,
            audit.result_id,
            {c["id"]: {"action": "accept"} for c in other.base.content["codes"]},
        )
        self.assertEqual(audit.result.content["codes"], [])

    def test_invalid_cycle_and_reference_deletion_block_merge(self):
        proposal = deepcopy(self.book)
        proposal["codes"][0]["parent"] = self.identity
        from .services import validate_book

        with self.assertRaises(ValueError):
            validate_book(proposal)
        self.annotate()
        proposal = deepcopy(self.book)
        proposal["codes"] = []
        self.alice.codebook = proposal
        self.alice.save()
        submission = submit(self.assignment.pk, self.alice.pk, 1)
        with self.assertRaises(ValueError):
            merge_submission(
                submission.pk,
                self.admin,
                self.alice.base_id,
                {self.identity: {"action": "accept"}},
            )

    def test_mapping_does_not_move_unaccepted_shared_children(self):
        child, target = (str(uuid.uuid4()), str(uuid.uuid4()))
        original = deepcopy(self.book)
        original["codes"].extend(
            [
                dict(id=child, name="Child", kind="code", parent=self.identity),
                dict(id=target, name="Existing target", kind="code", parent=None),
            ]
        )
        base = publish_codebook(self.study, book=original)
        self.alice.base = base
        self.alice.save()
        proposal = deepcopy(original)
        proposal["codes"][0]["name"] = "Coder alternative"
        submission = self.make_submission(proposal)
        audit = merge_submission(
            submission.pk,
            self.admin,
            base.pk,
            {self.identity: {"action": "map", "target": target}},
        )
        resulting_child = next(
            (c for c in audit.result.content["codes"] if c["id"] == child)
        )
        self.assertEqual(resulting_child["parent"], self.identity)


from concurrent.futures import ThreadPoolExecutor
from django.db import close_old_connections
from django.test import TransactionTestCase, skipUnlessDBFeature


class ConcurrentSaveTests(TransactionTestCase):

    @skipUnlessDBFeature("has_select_for_update")
    def test_simultaneous_saves_serialize_and_reject_stale_revision(self):
        self.study = new_study()
        identity = str(uuid.uuid4())
        publish_codebook(
            self.study,
            book=dict(version=1, codes=[dict(id=identity, name="Review", kind="code")]),
        )
        case = Case.objects.create(
            key="concurrent", name="Concurrent case", study=self.study
        )
        document = put_document(
            self.study, "concurrent-doc", "Text", "Human review", {}, [case]
        )
        session, link = create_session(self.study, "Coder", [case])
        assignment = Assignment.objects.get(session=session)
        from threading import Barrier

        barrier = Barrier(2)

        def save():
            self.study = new_study()
            close_old_connections()
            client = Client()
            client.post(
                "/access/exchange/",
                json.dumps({"token": link.split("#")[1]}),
                content_type="application/json",
            )
            barrier.wait(timeout=10)
            response = client.post(
                f"/api/assignments/{assignment.pk}/annotations/",
                json.dumps(
                    dict(
                        revision=0,
                        codebook_revision=0,
                        document=document.pk,
                        document_hash=document.sha256,
                        start=0,
                        end=5,
                        selected_text="Human",
                        codes=[identity],
                    )
                ),
                content_type="application/json",
            )
            close_old_connections()
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: save(), range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(Annotation.objects.count(), 1)
