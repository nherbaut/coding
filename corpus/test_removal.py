from copy import deepcopy
from unittest.mock import patch
import uuid

from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .importers import put_document
from .invitations import queue_invitation_email
from .models import (
    Annotation,
    Assignment,
    Case,
    CodingReport,
    CodingSession,
    InvitationEmail,
    MergeAudit,
    SharedCodebook,
    Submission,
    SubmissionEmail,
    SubmissionWithdrawal,
)
from .reporting import capture_report, report_evidence
from .services import (
    Conflict,
    assign_case,
    delete_coder_session,
    issue_link,
    merge_submission,
    remove_assignment,
    reopen_assignment,
    submit,
    withdraw_submission,
)
from .test_reporting import report_fixture


class RemovalTests(TestCase):
    def setUp(self):
        self.data = report_fixture()
        self.client.force_login(self.data["admin"])
        self.coder = Client()
        self.coder.post(
            "/access/exchange/",
            {"token": self.data["link"].split("#")[1]},
            content_type="application/json",
        )
        self.alice = self.data["alice"]
        self.assignment = self.data["assignments"][0]
        self.assignment.refresh_from_db()
        self.submission = self.data["submissions"][0]

    def test_delete_coder_hides_dashboard_and_revokes_cookie_and_original_link(self):
        original = deepcopy(self.submission.snapshot)
        response = self.client.post(
            "/", {"action": "delete_session", "session": self.alice.pk}
        )
        self.assertContains(response, "Coder session deleted")
        self.assertNotContains(response, "<h3>Alice</h3>", html=True)
        self.alice.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertIsNotNone(self.alice.deleted_at)
        self.assertIsNotNone(self.assignment.removed_at)
        self.assertEqual(self.alice.token_hash, "")
        self.assertEqual(self.coder.get("/api/state/").status_code, 404)
        self.assertEqual(self.coder.get("/work/").status_code, 404)
        self.assertEqual(
            self.coder.post(
                "/access/exchange/",
                {"token": self.data["link"].split("#")[1]},
                content_type="application/json",
            ).status_code,
            404,
        )
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.snapshot, original)
        self.assertEqual(
            Annotation.objects.filter(assignment=self.assignment).count(), 2
        )
        self.assertContains(self.client.get("/review/archive/"), "Alice")
        self.assertEqual(
            self.client.get(f"/review/{self.submission.pk}/").status_code, 200
        )
        payload = capture_report({"source": "submitted", "mode": "original"})
        self.assertTrue(
            next(r for r in payload["records"] if r["coder"] == "Alice")[
                "session_deleted_at"
            ]
        )
        self.assertEqual(len(report_evidence(payload)[1]), 4)
        self.assertEqual(
            self.client.post(
                "/", {"action": "delete_session", "session": self.alice.pk}
            ).status_code,
            200,
        )
        self.assertEqual(Submission.objects.count(), 2)

    def test_deleted_session_cannot_be_revived_by_stale_admin_forms_or_services(self):
        stale = deepcopy(self.alice)
        delete_coder_session(self.alice.pk)
        for action in ("assign", "rotate", "revoke"):
            response = self.client.post(
                "/",
                {
                    "action": action,
                    "session": self.alice.pk,
                    "cases": [self.data["case"].pk],
                },
            )
            self.assertEqual(response.status_code, 404)
        with override_settings(CASE_EMAIL_ENABLED=True):
            self.assertEqual(
                self.client.post(
                    "/",
                    {
                        "action": "send_invitation",
                        "session": self.alice.pk,
                        "email": "coder@example.org",
                    },
                ).status_code,
                404,
            )
        with self.assertRaises(Conflict):
            issue_link(stale)
        with self.assertRaises(Conflict):
            assign_case(stale, self.data["case"])
        # Simulate a request that authenticated immediately before deletion.
        with patch("corpus.views.current_session", return_value=stale):
            self.assertEqual(
                self.coder.post(
                    "/api/codebook/",
                    {"revision": 0, "codebook": stale.codebook},
                    content_type="application/json",
                ).status_code,
                409,
            )
            self.assertEqual(
                self.coder.post(
                    f"/api/assignments/{self.assignment.pk}/annotations/",
                    {"revision": self.assignment.revision},
                    content_type="application/json",
                ).status_code,
                409,
            )

    def test_assignment_removal_preserves_other_cases_and_reassignment_restores_coding(
        self,
    ):
        other = Case.objects.create(key="second", name="Second project")
        doc = put_document("second", "SECOND.md", "Other source", {}, [other])
        retained = assign_case(self.alice, other)
        response = self.client.post(
            "/",
            {
                "action": "remove_assignment",
                "assignment": self.assignment.pk,
                "revision": self.assignment.revision,
            },
        )
        self.assertContains(response, "Assignment removed")
        state = self.coder.get("/api/state/").json()
        self.assertEqual([a["id"] for a in state["assignments"]], [retained.pk])
        self.assertEqual(state["assignments"][0]["documents"][0]["id"], doc.pk)
        self.assertEqual(
            self.coder.post(
                f"/api/assignments/{self.assignment.pk}/submit/",
                {"revision": self.assignment.revision},
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            self.coder.post(
                f"/api/assignments/{self.assignment.pk}/annotations/",
                {},
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            Annotation.objects.filter(assignment=self.assignment).count(), 2
        )
        response = self.client.post(
            "/",
            {
                "action": "assign",
                "session": self.alice.pk,
                "cases": [self.data["case"].pk],
            },
        )
        self.assertContains(response, "Existing coding is restored")
        self.assignment.refresh_from_db()
        self.assertIsNone(self.assignment.removed_at)
        self.assertTrue(self.assignment.locked)
        self.assertEqual(
            Assignment.objects.filter(
                session=self.alice, case=self.data["case"]
            ).count(),
            1,
        )
        self.assertEqual(len(self.coder.get("/api/state/").json()["assignments"]), 2)

    def test_stale_assignment_removal_fails_without_changing_records(self):
        response = self.client.post(
            "/",
            {
                "action": "remove_assignment",
                "assignment": self.assignment.pk,
                "revision": 0,
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assignment.refresh_from_db()
        self.assertIsNone(self.assignment.removed_at)
        self.assertEqual(self.client.get("/review/archive/").status_code, 200)

    def test_preserve_existing_codebook_references_but_deny_new_removed_document_references(
        self,
    ):
        book = deepcopy(self.alice.codebook)
        reference = dict(
            type="corpus",
            document=self.data["document"].pk,
            hash=self.data["document"].sha256,
            start=1,
            end=3,
            quote="😀",
        )
        book["codes"][1]["references"].append(reference)
        self.assertEqual(
            self.coder.post(
                "/api/codebook/",
                {"revision": 0, "codebook": book},
                content_type="application/json",
            ).status_code,
            200,
        )
        remove_assignment(self.assignment.pk, self.assignment.revision)
        book["codes"][1]["memo"] = "Revised source memo"
        self.assertEqual(
            self.coder.post(
                "/api/codebook/",
                {"revision": 1, "codebook": book},
                content_type="application/json",
            ).status_code,
            200,
        )
        new = deepcopy(book["codes"][1])
        new.update(id=str(uuid.uuid4()), name="New proposal")
        book["codes"].append(new)
        self.assertEqual(
            self.coder.post(
                "/api/codebook/",
                {"revision": 2, "codebook": book},
                content_type="application/json",
            ).status_code,
            400,
        )

    @override_settings(
        CASE_EMAIL_ENABLED=True,
        DEFAULT_FROM_EMAIL="no-reply@example.org",
        PUBLIC_BASE_URL="https://coding.example.org",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_delete_cancels_pending_invites_and_removed_cases_are_not_emailed(self):
        self.alice.email = "alice@example.org"
        self.alice.save(update_fields=["email"])
        first = queue_invitation_email(self.alice, self.data["link"])
        delete_coder_session(self.alice.pk)
        first.refresh_from_db()
        self.assertIsNotNone(first.cancelled_at)
        self.assertEqual(first.body, "")
        self.assertIsNone(first.lease_token)
        from .notifications import deliver_next_email

        self.assertFalse(deliver_next_email())
        self.assertEqual(len(mail.outbox), 0)
        with self.assertRaises(Conflict):
            queue_invitation_email(self.alice, self.data["link"])

    def test_delete_submitted_case_reopens_latest_retains_snapshot_and_blocks_merge(
        self,
    ):
        before = deepcopy(self.submission.snapshot)
        response = self.client.post(
            "/", {"action": "delete_submission", "submission": self.submission.pk}
        )
        self.assertContains(response, "Submitted case removed from review")
        self.assertNotContains(
            response, f'name="submission" value="{self.submission.pk}"'
        )
        self.assignment.refresh_from_db()
        self.assertFalse(self.assignment.locked)
        self.assertEqual(self.assignment.revision, 2)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.snapshot, before)
        self.assertEqual(
            SubmissionWithdrawal.objects.get(submission=self.submission).initiated_by,
            "admin",
        )
        self.assertContains(
            self.client.get(f"/review/{self.submission.pk}/"),
            "This submission was withdrawn",
        )
        self.assertNotContains(
            self.client.get(f"/review/{self.submission.pk}/"), "Apply review decisions"
        )
        with self.assertRaises(Conflict):
            merge_submission(
                self.submission.pk,
                self.data["admin"],
                SharedCodebook.objects.latest("pk").pk,
                {},
            )
        self.assertEqual(
            len(capture_report({"source": "submitted", "mode": "original"})["records"]),
            1,
        )
        new = submit(self.assignment.pk, self.alice.pk, self.assignment.revision)
        self.assertNotEqual(new.pk, self.submission.pk)
        self.assertEqual(
            len(capture_report({"source": "submitted", "mode": "original"})["records"]),
            2,
        )
        self.assertEqual(
            Submission.objects.filter(assignment=self.assignment).count(), 2
        )

    def test_deleting_older_submission_does_not_unlock_newer_submission(self):
        reopen_assignment(self.assignment.pk)
        new = submit(self.assignment.pk, self.alice.pk, 2)
        # Idempotent removal of the already-withdrawn old version.
        withdraw_submission(self.submission.pk)
        self.assignment.refresh_from_db()
        self.assertTrue(self.assignment.locked)
        self.assertFalse(SubmissionWithdrawal.objects.filter(submission=new).exists())
        self.assertEqual(self.assignment.revision, 3)

    def test_coder_unsubmit_checks_ownership_revision_and_preserves_reviewed_audit(
        self,
    ):
        admin = self.data["admin"]
        shared = SharedCodebook.objects.latest("pk")
        audit = merge_submission(self.submission.pk, admin, shared.pk, {})
        other = self.data["assignments"][1]
        self.assertEqual(
            self.coder.post(
                f"/api/assignments/{other.pk}/unsubmit/",
                {"revision": 1},
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            self.coder.post(
                f"/api/assignments/{self.assignment.pk}/unsubmit/",
                {"revision": 0},
                content_type="application/json",
            ).status_code,
            409,
        )
        response = self.coder.post(
            f"/api/assignments/{self.assignment.pk}/unsubmit/",
            {"revision": 1},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["locked"])
        self.assertEqual(response.json()["revision"], 2)
        self.assertEqual(
            SubmissionWithdrawal.objects.get(submission=self.submission).initiated_by,
            "coder",
        )
        self.assertTrue(MergeAudit.objects.filter(pk=audit.pk).exists())
        self.assertEqual(SharedCodebook.objects.latest("pk").pk, audit.result_id)

    @override_settings(
        CASE_EMAIL_ENABLED=True,
        DEFAULT_FROM_EMAIL="no-reply@example.org",
        PUBLIC_BASE_URL="https://coding.example.org",
        CASE_SUBMISSION_EMAIL_TO=["admin@example.org"],
    )
    def test_withdrawal_cancels_unsent_submission_email_only(self):
        from .notifications import queue_submission_email

        queue_submission_email(self.submission)
        sent = self.data["submissions"][1]
        queue_submission_email(sent)
        SubmissionEmail.objects.filter(submission=sent).update(sent_at=timezone.now())
        withdraw_submission(self.submission.pk)
        withdraw_submission(sent.pk)
        self.assertFalse(
            SubmissionEmail.objects.filter(submission=self.submission).exists()
        )
        self.assertTrue(SubmissionEmail.objects.filter(submission=sent).exists())

    def test_removal_actions_and_archive_require_admin_and_csrf(self):
        for body in (
            {"action": "delete_session", "session": self.alice.pk},
            {
                "action": "remove_assignment",
                "assignment": self.assignment.pk,
                "revision": 1,
            },
            {"action": "delete_submission", "submission": self.submission.pk},
        ):
            self.assertEqual(self.coder.post("/", body).status_code, 302)
        self.assertEqual(self.coder.get("/review/archive/").status_code, 302)
        admin = Client(enforce_csrf_checks=True)
        admin.force_login(self.data["admin"])
        self.assertEqual(
            admin.post(
                "/", {"action": "delete_session", "session": self.alice.pk}
            ).status_code,
            403,
        )
        self.assertEqual(
            CodingSession.objects.filter(deleted_at__isnull=False).count(), 0
        )
