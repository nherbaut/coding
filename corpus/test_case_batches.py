import hashlib
import os
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch
import yaml
from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from .test_corpus_zip import archive
from .test_reporting import report_fixture
from .case_uploads import preview_cases, commit_cases, assign_cases
from .models import CorpusUploadBatch, Case, DocumentVersion, Annotation, Assignment
from .services import Conflict, create_session


def folder_zip():
    return archive(
        [
            ("Project A/README.md", b"A policy\r\n"),
            ("Project B/README.md", "B😀 policy\n".encode()),
            ("Project B/docs/guide.md", b"Guide\n"),
        ]
    )


def manifest_zip():
    content = b"Manifest policy\r\n"
    manifest = dict(
        version=1,
        cases=[
            dict(
                id="owner/project", name="owner/project", metadata={"origin": "SOURCE"}
            )
        ],
        documents=[
            dict(
                id="stable-doc",
                name="root/README.md",
                path="texts/a.md",
                cases=["owner/project"],
                metadata={
                    "text_sha256": hashlib.sha256(content).hexdigest(),
                    "provenance": [{"url": "https://example.org", "quote": "Evidence"}],
                },
            )
        ],
    )
    return archive(
        [("manifest.yaml", yaml.safe_dump(manifest)), ("texts/a.md", content)]
    )


@override_settings(CASE_EMAIL_ENABLED=False)
class CaseBatchTests(TestCase):
    def setUp(self):
        self.d = report_fixture()
        self.study = self.d["study"]
        self.owner = self.d["admin"]
        self.client.force_login(self.owner)

    def test_folder_preview_and_atomic_commit_preserve_existing_coding(self):
        batch = preview_cases(self.study, self.owner, [folder_zip()])
        self.assertEqual(len(batch.data["groups"]), 2)
        self.assertEqual(self.study.cases.count(), 1)
        old_book = self.study.codebook_id
        result = commit_cases(batch.pk, self.study.pk, self.owner)
        self.assertEqual(len(result.data["receipts"]), 2)
        self.assertEqual(self.study.cases.count(), 3)
        self.assertEqual(self.study.documents.count(), 4)
        self.assertEqual(Annotation.objects.count(), 4)
        self.assertEqual(Assignment.objects.count(), 2)
        self.assertEqual(self.study.codebook_id, old_book)
        commit_cases(batch.pk, self.study.pk, self.owner)
        self.assertEqual(self.study.cases.count(), 3)
        self.assertEqual(
            DocumentVersion.objects.get(document__name="docs/guide.md").text, "Guide\n"
        )

    def test_manifest_metadata_and_hierarchical_titles_are_retained(self):
        result = commit_cases(
            preview_cases(self.study, self.owner, [manifest_zip()]).pk,
            self.study.pk,
            self.owner,
        )
        case = Case.objects.get(name="owner/project", study=self.study)
        self.assertEqual(case.metadata["origin"], "SOURCE")
        self.assertEqual(case.metadata["import_case_id"], "owner/project")
        version = case.document_set.get().documentversion_set.get()
        self.assertEqual(version.document.name, "root/README.md")
        self.assertEqual(version.metadata["import_document_id"], "stable-doc")
        self.assertEqual(
            version.metadata["provenance"],
            [{"url": "https://example.org", "quote": "Evidence"}],
        )

    def test_existing_case_conflicts_and_late_conflict_roll_back_whole_batch(self):
        batch = preview_cases(self.study, self.owner, [folder_zip()])
        self.study.cases.create(key="later", name="Project B")
        with self.assertRaises(Conflict):
            commit_cases(batch.pk, self.study.pk, self.owner)
        self.assertFalse(self.study.cases.filter(name="Project A").exists())
        self.assertEqual(self.study.documents.count(), 1)
        self.assertEqual(CorpusUploadBatch.objects.count(), 1)
        with self.assertRaises(Conflict):
            preview_cases(self.study, self.owner, [folder_zip()])

    def test_invalid_manifests_and_root_files_leave_no_changes(self):
        for source in [
            archive([("root.md", b"No case folder")]),
            archive([("manifest.yaml", "version: 2")]),
            archive([("manifest.yaml", "version: 1\ncases: []\ndocuments: []")]),
            archive(
                [
                    (
                        "manifest.yaml",
                        "version: 1\ncases: []\ndocuments:\n- id: doc\n  path: missing.md\n  cases: [missing]",
                    )
                ]
            ),
        ]:
            with self.assertRaises(ValueError):
                preview_cases(self.study, self.owner, [source])
        self.assertEqual(self.study.cases.count(), 1)
        self.assertEqual(CorpusUploadBatch.objects.count(), 0)

    def test_explicit_batch_assignment_is_idempotent_and_scoped(self):
        coder, _ = create_session(self.study, "New coder", [])
        batch = commit_cases(
            preview_cases(self.study, self.owner, [folder_zip()]).pk,
            self.study.pk,
            self.owner,
        )
        self.assertEqual(
            assign_cases(batch.pk, self.study.pk, self.owner, [str(coder.pk)]), 2
        )
        self.assertEqual(
            assign_cases(batch.pk, self.study.pk, self.owner, [str(coder.pk)]), 0
        )
        self.assertEqual(coder.assignment_set.count(), 2)
        self.assertEqual(self.d["alice"].assignment_set.count(), 1)
        self.assertEqual(Annotation.objects.count(), 4)

    def test_http_new_case_mode_needs_no_manual_case_name_and_retains_single_mode(self):
        url = reverse("corpus-upload", args=[self.study.pk])
        page = self.client.get(url)
        self.assertContains(page, "Import multiple cases from ZIP")
        self.assertContains(page, "Add files to one case")
        response = self.client.post(
            url, dict(action="preview_cases", files=folder_zip())
        )
        self.assertEqual(response.status_code, 302)
        preview = self.client.get(response["Location"])
        self.assertContains(preview, "2 new cases")
        response = self.client.post(response["Location"], dict(action="confirm"))
        self.assertEqual(response.status_code, 302)
        self.assertContains(
            self.client.get(response["Location"]), "2 cases and 3 documents imported."
        )

    def test_github_case_batch_enqueues_one_coherent_event(self):
        from .test_github_sync import fixture_files, file_tree, FakeGit
        from .github_sync import import_study, synchronize_next
        from .github_repository import inspect_repository, replay_codebook

        files, _ = fixture_files()
        fake = FakeGit(files)
        study = import_study(
            self.owner,
            dict(id=199, full_name="research/batch"),
            "main",
            7,
            fake.head("", ""),
            files,
            file_tree(files),
            "Batch study",
        )
        synchronize_next(force=True, client_factory=lambda *_: fake)
        oldbook = fake.files["codebook/codebook.yaml"]
        batch = preview_cases(study, self.owner, [folder_zip()])
        commit_cases(batch.pk, study.pk, self.owner)
        self.assertEqual(study.github.events.filter(published=False).count(), 1)
        synchronize_next(force=True, client_factory=lambda *_: fake)
        data = inspect_repository(fake.files, 199)
        self.assertEqual(len(data["cases"]), 4)
        self.assertEqual(len(data["documents"]), 5)
        self.assertEqual(fake.files["codebook/codebook.yaml"], oldbook)
        replay_codebook(fake.files)
        self.assertFalse(study.github.events.filter(published=False).exists())

    @skipUnless(
        os.environ.get("CODING_CASE_ARCHIVE"),
        "Actual archive validation is opt-in and uses an isolated test database",
    )
    def test_actual_corpus_archive_preserves_provenance_and_existing_records(self):
        path = Path(os.environ["CODING_CASE_ARCHIVE"])
        batch = preview_cases(
            self.study, self.owner, [SimpleUploadedFile(path.name, path.read_bytes())]
        )
        expected_documents = {
            f["metadata"]["import_document_id"]: f["metadata"]
            for f in batch.data["files"]
        }
        expected_cases = {
            g["case"]["name"]: g["case"]["metadata"] for g in batch.data["groups"]
        }
        cases = len(batch.data["groups"])
        documents = len(batch.data["files"])
        snapshots = [s.snapshot for s in self.d["submissions"]]
        result = commit_cases(batch.pk, self.study.pk, self.owner)
        self.assertEqual(self.study.cases.count(), cases + 1)
        self.assertEqual(self.study.documents.count(), documents + 1)
        self.assertEqual(len(result.data["receipts"]), cases)
        self.assertEqual(Annotation.objects.count(), 4)
        self.assertEqual(Assignment.objects.count(), 2)
        for s, snapshot in zip(self.d["submissions"], snapshots):
            s.refresh_from_db()
            self.assertEqual(s.snapshot, snapshot)
        for file in result.data["files"]:
            version = DocumentVersion.objects.get(pk=file["version"])
            self.assertEqual(
                hashlib.sha256(version.text.encode()).hexdigest(), file["hash"]
            )
            self.assertEqual(
                version.metadata,
                expected_documents[version.metadata["import_document_id"]],
            )
        for case in self.study.cases.exclude(pk=self.d["case"].pk):
            self.assertEqual(case.metadata, expected_cases[case.name])
        print(
            f"Actual ZIP verified in isolated database: {cases} cases, {documents} documents; original coding preserved"
        )
