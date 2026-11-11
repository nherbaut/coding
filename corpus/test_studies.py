from copy import deepcopy
import io
import csv
import yaml
import json
import uuid
import zipfile
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.core import mail
from django.db import transaction
from .models import (
    Study,
    StudyAdmin,
    StudyAdminInvitation,
    StudyAdminEmail,
    StudyProtocol,
    SharedCodebook,
    Case,
    Document,
    CodingSession,
    Assignment,
    Annotation,
    Submission,
    CodingReport,
    ResearchQuestion,
    CodingGuideline,
    SubmissionEmail,
)
from .studies import (
    create_study,
    publish_codebook,
    invite_admin,
    accept_invitation,
    update_session_instructions,
)
from .services import create_session, submit, merge_submission
from .importers import put_document, import_zip
from .reporting import capture_report


class StudyTests(TestCase):

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("authelia:owner", is_staff=True)
        self.member = User.objects.create_user("authelia:member", is_staff=True)
        self.other = User.objects.create_user("authelia:other", is_staff=True)
        self.study = create_study(
            self.owner, "Alpha study", "Public-to-invitees study description"
        )
        self.secret = create_study(self.other, "Private beta study")
        self.code = str(uuid.uuid4())
        book = {
            "version": 1,
            "codes": [
                {
                    "id": self.code,
                    "kind": "code",
                    "name": "Review",
                    "parent": None,
                    "memo": "Verified source excerpt",
                }
            ],
        }
        publish_codebook(
            self.study,
            book,
            expected_revision=(self.study)
            .__class__.objects.get(pk=(self.study).pk)
            .codebook_id,
        )
        publish_codebook(
            self.secret,
            book,
            expected_revision=(self.secret)
            .__class__.objects.get(pk=(self.secret).pk)
            .codebook_id,
        )
        self.case = Case.objects.create(study=self.study, key="case", name="Alpha case")
        self.foreign_case = Case.objects.create(
            study=self.secret, key="case", name="Private beta case"
        )
        self.doc = put_document(
            self.study, "doc", "POLICY.md", "# Policy\n\nReview 😀", {}, [self.case]
        )
        self.foreign_doc = put_document(
            self.secret,
            "doc",
            "PRIVATE.md",
            "Private beta source",
            {},
            [self.foreign_case],
        )
        self.coder, self.link = create_session(self.study, "Alpha coder", [self.case])
        self.foreign_coder, _ = create_session(
            self.secret, "Private beta coder", [self.foreign_case]
        )
        self.assignment = Assignment.objects.get(session=self.coder)
        self.foreign_assignment = Assignment.objects.get(session=self.foreign_coder)
        Annotation.objects.create(
            assignment=self.assignment,
            document=self.doc,
            start=10,
            end=16,
            selected_text="Review",
            codes=[self.code],
        )
        self.submission = submit(self.assignment.pk, self.coder.pk, 0)
        self.foreign_submission = submit(
            self.foreign_assignment.pk, self.foreign_coder.pk, 0
        )
        self.client.force_login(self.owner)

    def url(self, name="dashboard", study=None, **kwargs):
        return reverse(name, kwargs=dict(study_id=(study or self.study).pk, **kwargs))

    def login_coder(self, link=None):
        client = Client()
        self.assertEqual(
            client.post(
                "/access/exchange/",
                {"token": (link or self.link).split("#")[1]},
                content_type="application/json",
            ).status_code,
            200,
        )
        return client

    def test_admin_starts_with_own_studies_and_creates_more(self):
        response = self.client.get("/")
        self.assertContains(response, "Alpha study")
        self.assertNotContains(response, "Private beta study")
        response = self.client.post(
            "/", {"title": "Second owned study", "description": "Another topic"}
        )
        self.assertEqual(response.status_code, 302)
        created = Study.objects.get(title="Second owned study")
        self.assertEqual(created.owner, self.owner)
        self.assertEqual(created.codebook.study_id, created.pk)
        self.assertEqual(created.instructions.study_id, created.pk)
        self.assertEqual(created.codebook.content["codes"], [])
        self.assertEqual(Study.objects.filter(owner=self.owner).count(), 2)

    def test_owner_invites_admin_accepts_and_revokes_study_access(self):
        response = self.client.post(
            self.url("study-settings"),
            {"action": "invite_admin", "username": "MEMBER"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        invitation = StudyAdminInvitation.objects.get(study=self.study)
        member = Client()
        member.force_login(self.member)
        self.assertEqual(member.get(self.url()).status_code, 404)
        self.assertContains(member.get("/"), "Accept invitation")
        self.assertEqual(
            member.post(
                "/", {"action": "accept_invitation", "invitation": invitation.pk}
            ).status_code,
            302,
        )
        self.assertContains(member.get(self.url()), "Alpha study")
        self.assertEqual(member.get(self.url(study=self.secret)).status_code, 404)
        for name in (
            "study-corpus",
            "study-protocol",
            "coding-report",
            "coding-archive",
            "shared-export",
            "study-settings",
        ):
            self.assertEqual(member.get(self.url(name)).status_code, 200)
        for action in ("invite_admin", "revoke_admin", "cancel_invitation"):
            self.assertEqual(
                member.post(
                    self.url("study-settings"),
                    {"action": action, "username": "other"},
                    follow=True,
                ).status_code,
                403,
            )
        self.assertEqual(
            member.post("/", {"title": "Member-owned study"}).status_code, 302
        )
        self.assertEqual(
            Study.objects.get(title="Member-owned study").owner, self.member
        )
        membership = StudyAdmin.objects.get(study=self.study, user=self.member)
        self.assertEqual(
            self.client.post(
                self.url("study-settings"),
                {"action": "revoke_admin", "membership": membership.pk},
                follow=True,
            ).status_code,
            200,
        )
        self.assertEqual(member.get(self.url()).status_code, 404)
        self.assertNotContains(member.get("/"), "Alpha study")

    def test_invitations_are_identity_bound_and_owner_can_cancel(self):
        invitation = invite_admin(self.study, self.owner, "member")
        other = Client()
        other.force_login(self.other)
        self.assertEqual(
            other.post(
                "/", {"action": "accept_invitation", "invitation": invitation.pk}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                self.url("study-settings"),
                {"action": "cancel_invitation", "invitation": invitation.pk},
                follow=True,
            ).status_code,
            200,
        )
        member = Client()
        member.force_login(self.member)
        self.assertEqual(
            member.post(
                "/", {"action": "accept_invitation", "invitation": invitation.pk}
            ).status_code,
            404,
        )
        self.assertFalse(StudyAdmin.objects.exists())
        self.assertEqual(
            self.client.post(
                self.url("study-settings"),
                {"action": "invite_admin", "username": "owner"},
                follow=True,
            ).status_code,
            400,
        )
        invite_admin(self.study, self.owner, "member")
        self.assertEqual(
            self.client.post(
                self.url("study-settings"),
                {"action": "invite_admin", "username": "member"},
                follow=True,
            ).status_code,
            400,
        )

    def test_all_study_pages_reject_uninvited_admins(self):
        for name in (
            "dashboard",
            "study-settings",
            "study-corpus",
            "study-protocol",
            "coding-report",
            "coding-archive",
            "shared-export",
        ):
            for method in ("get", "post"):
                response = getattr(self.client, method)(
                    self.url(name, study=self.secret), {}
                )
                self.assertEqual(response.status_code, 404, (name, method))
                self.assertNotIn(b"Private beta source", response.content)
        self.assertEqual(
            self.client.get(
                self.url(
                    "review",
                    study=self.secret,
                    submission_id=self.foreign_submission.pk,
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                self.url(
                    "results",
                    study=self.secret,
                    submission_id=self.foreign_submission.pk,
                )
            ).status_code,
            404,
        )

    def test_foreign_object_ids_are_rejected_inside_an_owned_study(self):
        for name in ("review", "results"):
            self.assertEqual(
                self.client.get(
                    self.url(name, submission_id=self.foreign_submission.pk)
                ).status_code,
                404,
            )
        for body in [
            dict(
                action="assign",
                session=str(self.foreign_coder.pk),
                cases=[self.case.pk],
            ),
            dict(action="create", coder="Intruder", cases=[self.foreign_case.pk]),
            dict(action="delete_session", session=str(self.foreign_coder.pk)),
            dict(
                action="remove_assignment",
                assignment=self.foreign_assignment.pk,
                revision=1,
            ),
            dict(action="delete_submission", submission=self.foreign_submission.pk),
            dict(action="update_instructions", session=str(self.foreign_coder.pk)),
        ]:
            self.assertEqual(
                self.client.post(self.url(), body).status_code, 404, body["action"]
            )
        self.assertFalse(CodingSession.objects.filter(coder="Intruder").exists())
        self.assertEqual(
            self.client.get(
                self.url("shared-export"), {"revision": self.foreign_coder.base_id}
            ).status_code,
            404,
        )
        for query in ({"case": self.foreign_case.pk}, {"coder": self.foreign_coder.pk}):
            self.assertEqual(
                self.client.get(self.url("coding-report"), query).status_code, 404
            )
        self.assertEqual(
            self.client.post(
                self.url("study-corpus"),
                {
                    "action": "text",
                    "case": self.foreign_case.pk,
                    "name": "Bad",
                    "text": "Injected",
                },
                follow=True,
            ).status_code,
            404,
        )
        self.assertFalse(Document.objects.filter(name="Bad").exists())

    def test_model_and_service_relationships_cannot_cross_studies(self):
        with self.assertRaises(ValueError):
            create_session(self.study, "Wrong case", [self.foreign_case])
        with self.assertRaises(ValueError):
            Assignment.objects.create(session=self.coder, case=self.foreign_case)
        with self.assertRaises(ValueError), transaction.atomic():
            self.doc.document.cases.add(self.foreign_case)
        with self.assertRaises(ValueError), transaction.atomic():
            self.assignment.documents.add(self.foreign_doc)
        with self.assertRaises(ValueError):
            Annotation.objects.create(
                assignment=self.assignment,
                document=self.foreign_doc,
                start=0,
                end=1,
                selected_text="P",
                codes=[self.code],
            )
        with self.assertRaises(ValueError):
            CodingSession.objects.create(
                study=self.study,
                base=self.foreign_coder.base,
                instructions=self.coder.instructions,
                coder="Wrong book",
                codebook=self.coder.codebook,
            )
        with self.assertRaises(ValueError):
            CodingSession.objects.create(
                study=self.study,
                base=self.coder.base,
                instructions=self.foreign_coder.instructions,
                coder="Wrong instructions",
                codebook=self.coder.codebook,
            )
        self.study.codebook = self.foreign_coder.base
        with self.assertRaises(ValueError):
            self.study.save()

    def test_private_coder_link_exposes_only_its_study(self):
        client = self.login_coder()
        data = client.get("/api/state/").json()
        self.assertEqual(data["study"]["id"], str(self.study.pk))
        self.assertEqual(len(data["assignments"]), 1)
        self.assertNotIn("Private beta", json.dumps(data))
        self.assertEqual(
            client.post(
                f"/api/assignments/{self.foreign_assignment.pk}/unsubmit/",
                {"revision": 1},
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            client.post(
                f"/api/assignments/{self.foreign_assignment.pk}/annotations/",
                {},
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(client.get(self.url()).status_code, 302)
        book = deepcopy(self.coder.codebook)
        book["codes"][0]["references"] = [
            dict(
                type="corpus",
                document=self.foreign_doc.pk,
                hash=self.foreign_doc.sha256,
                start=0,
                end=7,
                quote="Private",
            )
        ]
        self.assertEqual(
            client.post(
                "/api/codebook/",
                {"revision": 0, "codebook": book},
                content_type="application/json",
            ).status_code,
            400,
        )

    def test_instructions_are_published_ordered_linked_and_pinned_to_sessions(self):
        protocol_url = self.url("study-protocol")
        for label, position in [("RQ2", 2), ("RQ1", 1)]:
            self.assertEqual(
                self.client.post(
                    protocol_url,
                    dict(
                        action="save_question",
                        label=label,
                        text="What is required?",
                        position=position,
                    ),
                    follow=True,
                ).status_code,
                200,
            )
        q = self.study.research_questions.get(label="RQ1")
        self.assertEqual(
            self.client.post(
                protocol_url,
                dict(
                    action="save_guideline",
                    title="Read context",
                    text="**Review** the surrounding paragraph.",
                    position=1,
                    questions=[str(q.pk)],
                ),
                follow=True,
            ).status_code,
            200,
        )
        guideline = self.study.guidelines.get()
        self.assertEqual(list(guideline.questions.all()), [q])
        coder, link = create_session(self.study, "New coder", [self.case])
        old = coder.instructions
        self.assertEqual(
            [q["label"] for q in old.content["research_questions"]], ["RQ1", "RQ2"]
        )
        self.client.post(
            protocol_url,
            dict(
                action="save_guideline",
                item=guideline.pk,
                title="Read context",
                text="Updated instructions",
                position=1,
                questions=[str(q.pk)],
            ),
            follow=True,
        )
        client = self.login_coder(link)
        self.assertIn(
            "surrounding paragraph",
            client.get("/api/state/").json()["instructions"]["guidelines"][0]["text"],
        )
        assignment = Assignment.objects.get(session=coder)
        snapshot = submit(assignment.pk, coder.pk, 0)
        response = self.client.post(
            self.url(), dict(action="update_instructions", session=coder.pk)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            client.get("/api/state/").json()["instructions"]["guidelines"][0]["text"],
            "Updated instructions",
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.snapshot["instructions"]["revision"], old.pk)
        self.assertIn(
            "surrounding paragraph",
            snapshot.snapshot["instructions"]["content"]["guidelines"][0]["text"],
        )
        with self.assertRaises(ValueError):
            old.save()

    def test_foreign_guideline_question_and_protocol_edit_ids_are_rejected(self):
        foreign = ResearchQuestion.objects.create(
            study=self.secret, label="RQsecret", text="Hidden"
        )
        response = self.client.post(
            self.url("study-protocol"),
            dict(
                action="save_guideline",
                title="Bad",
                text="Bad",
                questions=[str(foreign.pk)],
                position=0,
            ),
            follow=True,
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.study.guidelines.exists())
        self.assertEqual(
            self.client.post(
                self.url("study-protocol"),
                dict(action="delete_question", item=foreign.pk),
                follow=True,
            ).status_code,
            404,
        )
        guideline = CodingGuideline.objects.create(
            study=self.study, title="Own", text="Text"
        )
        with self.assertRaises(ValueError), transaction.atomic():
            guideline.questions.add(foreign)

    def test_same_import_keys_are_independent_and_reports_are_study_scoped(self):
        self.assertNotEqual(self.doc.document_id, self.foreign_doc.document_id)
        report = capture_report(self.study, dict(source="submitted", mode="original"))
        self.assertEqual(report["study"]["id"], str(self.study.pk))
        self.assertEqual(len(report["records"]), 1)
        self.assertNotIn("Private beta", json.dumps(report))
        saved = CodingReport.objects.create(
            study=self.secret,
            name="Secret report",
            created_by=self.other,
            payload=capture_report(
                self.secret, dict(source="submitted", mode="original")
            ),
        )
        self.assertEqual(
            self.client.get(self.url("saved-report", report_id=saved.pk)).status_code,
            404,
        )
        result = self.client.post(
            self.url("coding-report"),
            dict(name="Alpha report", source="submitted", mode="original"),
        )
        self.assertEqual(result.status_code, 302)
        saved = CodingReport.objects.get(study=self.study)
        self.assertEqual(
            saved.payload["records"][0]["instructions"]["revision"],
            self.coder.instructions_id,
        )
        self.assertContains(self.client.get(result.url), "Alpha study")
        self.assertNotContains(self.client.get(result.url), "Private beta")

    def test_codebook_merge_and_references_are_isolated_per_study(self):
        foreign_revision = self.secret.codebook_id
        self.secret.refresh_from_db()
        foreign_revision = self.secret.codebook_id
        self.coder.codebook["codes"].append(
            dict(
                id=str(uuid.uuid4()),
                kind="code",
                name="Alpha proposal",
                parent=None,
                memo="Corpus quote",
            )
        )
        self.coder.save()
        Assignment.objects.filter(pk=self.assignment.pk).update(locked=False)
        proposal = submit(self.assignment.pk, self.coder.pk, 1)
        self.study.refresh_from_db()
        identity = self.coder.codebook["codes"][-1]["id"]
        audit = merge_submission(
            proposal.pk,
            self.owner,
            self.study.codebook_id,
            {identity: dict(action="accept")},
        )
        self.assertEqual(audit.result.study_id, self.study.pk)
        self.secret.refresh_from_db()
        self.assertEqual(self.secret.codebook_id, foreign_revision)
        self.assertNotIn("Alpha proposal", json.dumps(self.secret.codebook.content))

    def test_imported_corpus_references_must_belong_to_the_target_study(self):
        self.study.refresh_from_db()
        book = deepcopy(self.study.codebook.content)
        book["codes"][0]["references"] = [
            dict(
                type="corpus",
                document=self.foreign_doc.pk,
                hash=self.foreign_doc.sha256,
                start=0,
                end=7,
                quote="Private",
            )
        ]
        with self.assertRaises(ValueError):
            publish_codebook(
                self.study,
                book,
                expected_revision=(self.study)
                .__class__.objects.get(pk=(self.study).pk)
                .codebook_id,
            )
        book["codes"][0]["references"] = [
            dict(
                type="corpus",
                document=self.doc.pk,
                hash=self.doc.sha256,
                start=10,
                end=16,
                quote="Review",
            )
        ]
        revision = publish_codebook(
            self.study,
            book,
            expected_revision=(self.study)
            .__class__.objects.get(pk=(self.study).pk)
            .codebook_id,
        )
        self.assertEqual(revision.study_id, self.study.pk)

    def test_same_zip_import_keys_are_independent_across_studies(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(
                "manifest.yaml",
                "version: 1\ncases:\n- id: imported-case\n  name: Imported case\ndocuments:\n- id: imported-doc\n  name: POLICY.md\n  path: policy.md\n  cases: [imported-case]\n",
            )
            z.writestr("policy.md", "# Same imported text")
        for study in (self.study, self.secret):
            archive.seek(0)
            self.assertEqual(import_zip(study, archive), 1)
        self.assertEqual(Case.objects.filter(key="imported-case").count(), 2)
        documents = list(Document.objects.filter(key="imported-doc"))
        self.assertEqual(len(documents), 2)
        self.assertNotEqual(documents[0].study_id, documents[1].study_id)

    def test_exports_include_study_and_instruction_provenance(self):
        from .services import load_book

        book = yaml.safe_load(self.client.get(self.url("shared-export")).content)
        self.assertEqual(book["export_context"]["study"]["id"], str(self.study.pk))
        self.assertNotIn("export_context", load_book(yaml.safe_dump(book)))
        response = self.client.get(
            self.url("results", submission_id=self.submission.pk), {"format": "csv"}
        )
        row = next(csv.DictReader(io.StringIO(response.content.decode())))
        self.assertEqual(row["study_id"], str(self.study.pk))
        self.assertEqual(int(row["instructions_revision"]), self.coder.instructions_id)
        for format in ("csv", "summary-csv"):
            response = self.client.get(self.url("coding-report"), {"format": format})
            row = next(csv.DictReader(io.StringIO(response.content.decode())))
            self.assertEqual(row["study"], "Alpha study")
            self.assertEqual(row["study_id"], str(self.study.pk))
            self.assertIn(
                (
                    "current_research_questions"
                    if format == "summary-csv"
                    else "research_questions"
                ),
                row,
            )

    @override_settings(
        CASE_EMAIL_ENABLED=True,
        DEFAULT_FROM_EMAIL="no-reply@example.org",
        PUBLIC_BASE_URL="https://coding.example.org",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_notification_recipients_and_admin_invitation_email_are_study_specific(
        self,
    ):
        from .notifications import queue_submission_email, deliver_next_email

        self.study.notification_emails = ["alpha@example.org"]
        self.study.save(update_fields=["notification_emails"])
        self.secret.notification_emails = ["beta@example.org"]
        self.secret.save(update_fields=["notification_emails"])
        queue_submission_email(Submission.objects.get(pk=self.submission.pk))
        queue_submission_email(Submission.objects.get(pk=self.foreign_submission.pk))
        self.assertEqual(
            SubmissionEmail.objects.get(submission=self.submission).recipients,
            ["alpha@example.org"],
        )
        self.assertEqual(
            SubmissionEmail.objects.get(submission=self.foreign_submission).recipients,
            ["beta@example.org"],
        )
        invitation = invite_admin(
            self.study, self.owner, "member", "member@example.org"
        )
        job = StudyAdminEmail.objects.get(invitation=invitation)
        self.assertEqual(job.recipients, ["member@example.org"])
        self.assertIn("coding-admins", job.body)
        self.assertNotIn("Private beta", job.body)
        for _ in range(3):
            self.assertTrue(deliver_next_email())
        self.assertEqual(len(mail.outbox), 3)

    @override_settings(
        AUTHELIA_AUTH_ENABLED=True,
        AUTHELIA_ADMIN_GROUP="coding-admins",
        AUTHELIA_PROXY_SECRET="test-proxy-secret-with-at-least-thirty-two-characters",
        AUTHELIA_TRUSTED_PROXIES=["192.168.1.60/32"],
    )
    def test_authelia_group_and_study_membership_are_both_required(self):
        invitation = invite_admin(self.study, self.owner, "member")
        client = Client()
        headers = dict(
            REMOTE_ADDR="192.168.1.60",
            HTTP_X_CODING_PROXY_SECRET="test-proxy-secret-with-at-least-thirty-two-characters",
            HTTP_REMOTE_USER="member",
            HTTP_REMOTE_GROUPS="coding-admins",
        )
        self.assertEqual(client.get(self.url(), **headers).status_code, 404)
        self.assertEqual(
            client.post(
                "/",
                {"action": "accept_invitation", "invitation": invitation.pk},
                **headers,
            ).status_code,
            302,
        )
        self.assertEqual(client.get(self.url(), **headers).status_code, 200)
        self.assertEqual(
            client.get(
                self.url(), **{**headers, "HTTP_REMOTE_GROUPS": "home"}
            ).status_code,
            403,
        )
        self.assertEqual(
            client.get(
                self.url(), **{**headers, "REMOTE_ADDR": "192.168.1.99"}
            ).status_code,
            403,
        )
        self.assertEqual(
            client.get(
                self.url(), **{**headers, "HTTP_X_CODING_PROXY_SECRET": "wrong"}
            ).status_code,
            403,
        )
