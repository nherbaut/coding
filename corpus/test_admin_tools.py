from copy import deepcopy
import json
import uuid
import yaml
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from .models import (
    Annotation,
    Assignment,
    CodingSession,
    Case,
    StudyAdmin,
    Submission,
    MergeAudit,
)
from .services import (
    create_session,
    issue_link,
    submit,
    merge_submission,
    Conflict,
    load_book,
)
from .studies import create_study, publish_codebook
from .importers import put_document
from .reporting import capture_report


class AdminToolsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", is_staff=True)
        self.member = get_user_model().objects.create_user("member", is_staff=True)
        self.other = get_user_model().objects.create_user("other", is_staff=True)
        self.study = create_study(self.owner, "Study")
        StudyAdmin.objects.create(
            study=self.study, user=self.member, granted_by=self.owner
        )
        self.category, self.code = str(uuid.uuid4()), str(uuid.uuid4())
        self.book = dict(
            version=1,
            provenance={"keep": "metadata"},
            codes=[
                dict(
                    id=self.category,
                    name="Dimension",
                    kind="category",
                    parent=None,
                    memo="Evidence",
                ),
                dict(
                    id=self.code,
                    name="Review",
                    kind="code",
                    parent=self.category,
                    color="#f0cc44",
                    memo="Quote",
                    rationale="Guidance",
                    references=[
                        dict(title="Source", location="p. 1", extra={"preserve": True})
                    ],
                ),
            ],
        )
        self.revision = publish_codebook(
            self.study, self.book, expected_revision=self.study.codebook_id
        ).pk
        self.case = Case.objects.create(study=self.study, key="one", name="One")
        self.doc = put_document(
            self.study, "doc", "Policy.md", "A😀 review", {}, [self.case]
        )
        self.client.force_login(self.owner)
        self.root = reverse("admin-coding", args=[self.study.pk])
        self.graph = reverse("codebook-state", args=[self.study.pk])
        self.publish = reverse("codebook-publish", args=[self.study.pk])

    def post(self, url, data, client=None):
        return (client or self.client).post(url, data, content_type="application/json")

    def start(self):
        self.assertEqual(self.client.get(self.root).status_code, 200)
        return CodingSession.objects.get(
            study=self.study, admin_user=self.owner, deleted_at=None
        )

    def annotate(self, session):
        a = Assignment.objects.get(session=session, case=self.case)
        response = self.post(
            self.root + f"api/assignments/{a.pk}/annotations/",
            dict(
                revision=a.revision,
                codebook_revision=session.revision,
                document=self.doc.pk,
                document_hash=self.doc.sha256,
                start=1,
                end=3,
                selected_text="😀",
                codes=[self.code],
                memo="Admin evidence",
            ),
        )
        self.assertEqual(response.status_code, 200, response.content)
        a.refresh_from_db()
        return a

    def test_admin_session_resume_new_cases_and_pinned_versions(self):
        session = self.start()
        self.assertEqual(session.token_hash, "")
        a = Assignment.objects.get(session=session)
        latest = put_document(
            self.study, "doc", "Policy.md", "changed", {}, [self.case]
        )
        case2 = Case.objects.create(study=self.study, key="two", name="Two")
        self.client.get(self.root)
        self.assertEqual(CodingSession.objects.filter(admin_user=self.owner).count(), 1)
        self.assertEqual(list(a.documents.all()), [self.doc])
        self.assertNotEqual(latest.pk, self.doc.pk)
        self.assertTrue(Assignment.objects.filter(session=session, case=case2).exists())
        with self.assertRaises(IntegrityError), transaction.atomic():
            CodingSession.objects.create(
                study=self.study,
                admin_user=self.owner,
                base=session.base,
                instructions=session.instructions,
                coder="duplicate",
                codebook=self.book,
            )

    def test_admin_and_private_workspaces_are_independent(self):
        private, link = create_session(self.study, "Private coder", [self.case])
        self.post("/access/exchange/", dict(token=link.split("#")[1]))
        session = self.start()
        self.assertEqual(
            self.client.get("/api/state/").json()["coder"], "Private coder"
        )
        self.assertEqual(
            self.client.get(self.root + "api/state/").json()["coder"], session.coder
        )
        self.assertNotEqual(private.pk, session.pk)
        with self.assertRaises(ValueError):
            issue_link(session)

    def test_members_have_separate_sessions_and_cannot_write_other_assignment(self):
        own = self.start()
        other_client = Client()
        other_client.force_login(self.member)
        other_client.get(self.root)
        theirs = CodingSession.objects.get(admin_user=self.member)
        foreign = Assignment.objects.get(session=theirs)
        response = self.post(
            self.root + f"api/assignments/{foreign.pk}/annotations/", {}
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(own.pk, theirs.pk)

    def test_study_and_membership_checks_on_every_route(self):
        self.start()
        self.client.force_login(self.member)
        self.client.get(self.root)
        StudyAdmin.objects.filter(user=self.member).update(
            revoked_at="2026-11-11T00:00:00Z"
        )
        for route in [
            self.root,
            self.root + "api/state/",
            self.root + "codebook.yaml",
            self.graph,
            reverse("study-codebook", args=[self.study.pk]),
        ]:
            self.assertEqual(self.client.get(route).status_code, 404, route)
        self.assertEqual(
            self.post(
                self.publish, {"revision": self.revision, "codebook": self.book}
            ).status_code,
            404,
        )
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.root).status_code, 404)

    def test_personal_codebook_and_instruction_snapshots_are_independent(self):
        session = self.start()
        base = session.base_id
        protocol = session.instructions_id
        changed = deepcopy(self.book)
        changed["codes"][1]["name"] = "Updated shared"
        publish_codebook(self.study, changed, expected_revision=self.revision)
        self.client.get(self.root)
        session.refresh_from_db()
        self.assertEqual(session.codebook, self.book)
        self.assertEqual(session.base_id, base)
        self.assertEqual(session.instructions_id, protocol)
        personal = deepcopy(self.book)
        personal["codes"][1]["name"] = "Personal"
        result = self.post(
            self.root + "api/codebook/",
            dict(revision=session.revision, codebook=personal),
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.get(self.graph).json()["codebook"], changed)

    def test_admin_submission_unsubmission_report_and_review(self):
        session = self.start()
        a = self.annotate(session)
        response = self.post(
            self.root + f"api/assignments/{a.pk}/submit/", dict(revision=a.revision)
        )
        self.assertEqual(response.status_code, 200, response.content)
        submission = Submission.objects.get(assignment=a)
        self.assertEqual(submission.snapshot["annotations"][0]["selected_text"], "😀")
        self.assertIn("(admin)", submission.snapshot["coder"])
        report = capture_report(self.study, dict(source="submitted", mode="original"))
        self.assertEqual(len(report["records"]), 1)
        self.assertEqual(report["records"][0]["session"], str(session.pk))
        self.assertEqual(
            self.client.get(
                reverse("review", args=[self.study.pk, submission.pk])
            ).status_code,
            200,
        )
        merge_submission(submission.pk, self.owner, self.revision, {})
        a.refresh_from_db()
        self.assertEqual(
            self.post(
                self.root + f"api/assignments/{a.pk}/unsubmit/",
                dict(revision=a.revision),
            ).status_code,
            200,
        )
        self.assertEqual(
            Submission.objects.get(pk=submission.pk).snapshot, submission.snapshot
        )

    def test_graph_atomic_revision_conflict_yaml_and_provenance_round_trip(self):
        state = self.client.get(self.graph).json()
        book = deepcopy(state["codebook"])
        book["codes"][1]["name"] = "New name"
        result = self.post(
            self.publish, dict(revision=state["revision"], codebook=book)
        )
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(
            self.post(
                self.publish, dict(revision=state["revision"], codebook=self.book)
            ).status_code,
            409,
        )
        self.assertEqual(self.client.get(self.graph).json()["codebook"], book)
        self.assertEqual(load_book(yaml.safe_dump(book)), book)
        upload = SimpleUploadedFile("book.yaml", yaml.safe_dump(self.book).encode())
        response = self.client.post(
            reverse("dashboard", args=[self.study.pk]),
            {"action": "book", "revision": state["revision"], "file": upload},
        )
        self.assertEqual(response.status_code, 409)
        exported = self.client.get(
            reverse("shared-export", args=[self.study.pk])
        ).content.decode()
        self.assertEqual(load_book(exported), book)

    def test_invalid_hierarchy_duplicate_and_referenced_deletion_and_type(self):
        session = self.start()
        a = self.annotate(session)
        for modify in ["cycle", "self", "missing", "duplicate", "delete", "kind"]:
            book = deepcopy(self.book)
            if modify == "cycle":
                book["codes"][0]["parent"] = self.code
            if modify == "self":
                book["codes"][1]["parent"] = self.code
            if modify == "missing":
                book["codes"][1]["parent"] = str(uuid.uuid4())
            if modify == "duplicate":
                book["codes"].append(
                    dict(
                        id=str(uuid.uuid4()),
                        name="review",
                        parent=self.category,
                        kind="code",
                    )
                )
            if modify == "delete":
                book["codes"].pop()
            if modify == "kind":
                book["codes"][1]["kind"] = "category"
            self.assertEqual(
                self.post(
                    self.publish, dict(revision=self.revision, codebook=book)
                ).status_code,
                400,
                modify,
            )
        book = deepcopy(self.book)
        book["codes"][1]["archived"] = True
        self.assertEqual(
            self.post(
                self.publish, dict(revision=self.revision, codebook=book)
            ).status_code,
            200,
        )
        self.assertTrue(Annotation.objects.filter(assignment=a).exists())

    def test_graph_add_move_and_remove_unreferenced_concepts(self):
        book = deepcopy(self.book)
        added = str(uuid.uuid4())
        book["codes"].append(
            dict(id=added, name="New root", parent=None, kind="category")
        )
        book["codes"][1]["parent"] = added
        result = self.post(
            self.publish, dict(revision=self.revision, codebook=book)
        ).json()
        book["codes"] = [c for c in book["codes"] if c["id"] != self.category]
        result = self.post(
            self.publish, dict(revision=result["revision"], codebook=book)
        )
        self.assertEqual(result.status_code, 200)
        book["codes"][0]["parent"] = None
        self.assertEqual(
            self.post(
                self.publish, dict(revision=result.json()["revision"], codebook=book)
            ).status_code,
            200,
        )

    def test_logout_csrf_private_reentry_and_no_cache(self):
        private, link = create_session(self.study, "Private coder", [self.case])
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.owner)
        response = secure.get(reverse("dashboard", args=[self.study.pk]))
        self.assertIn("no-store", response["Cache-Control"])
        token = secure.cookies["csrftoken"].value
        self.assertEqual(secure.post("/logout/").status_code, 403)
        self.assertEqual(secure.get("/logout/").status_code, 405)
        secure.post(
            "/access/exchange/",
            json.dumps(dict(token=link.split("#")[1])),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        response = secure.post("/logout/", HTTP_X_CSRFTOKEN=token)
        self.assertRedirects(response, "/signed-out/")
        self.assertNotIn("_auth_user_id", secure.session)
        self.assertNotIn("coder_session", secure.session)
        self.assertEqual(secure.get("/api/state/").status_code, 404)
        landing = secure.get("/signed-out/")
        self.assertContains(landing, "Authelia remains signed in")
        token = (
            secure.get("/access/").cookies.get("csrftoken")
            or secure.cookies["csrftoken"]
        )
        response = secure.post(
            "/access/exchange/",
            json.dumps(dict(token=link.split("#")[1])),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token.value,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(secure.get("/api/state/").json()["coder"], "Private coder")


from django.test import TransactionTestCase, skipUnlessDBFeature
from django.db import connections
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


class AdminConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_shared_publications_have_exactly_one_winner(self):
        owner = get_user_model().objects.create_user("concurrent-owner", is_staff=True)
        study = create_study(owner, "Concurrency")
        expected = study.codebook_id
        barrier = Barrier(2)

        def publish(name):
            try:
                copy = deepcopy(study.codebook.content)
                copy["codes"] = [
                    dict(id=str(uuid.uuid4()), name=name, parent=None, kind="code")
                ]
                barrier.wait(timeout=10)
                try:
                    return publish_codebook(study, copy, expected_revision=expected).pk
                except Conflict:
                    return "conflict"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(publish, ["one", "two"]))
        self.assertEqual(outcomes.count("conflict"), 1)
        study.refresh_from_db()
        self.assertIn(study.codebook_id, outcomes)

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_admin_workspace_creation_resumes_same_session(self):
        from .admin_tools import admin_session

        owner = get_user_model().objects.create_user("session-owner", is_staff=True)
        study = create_study(owner, "Session concurrency")
        barrier = Barrier(2)

        def start(_):
            try:
                barrier.wait(timeout=10)
                return admin_session(study, owner).pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(start, range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(
            CodingSession.objects.filter(study=study, admin_user=owner).count(), 1
        )
