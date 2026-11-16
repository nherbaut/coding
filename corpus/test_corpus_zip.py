import hashlib
import io
import stat
import zipfile
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from .upload_files import read_uploads
from .corpus_uploads import preview_upload, commit_upload
from .test_reporting import report_fixture
from .models import CorpusUploadBatch, DocumentVersion, Annotation


def archive(entries, name="texts.zip", compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as z:
        for path, content in entries:
            z.writestr(path, content)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="application/zip")


class ZipValidationTests(SimpleTestCase):
    def test_unicode_line_endings_folders_and_macos_metadata(self):
        raw = "\ufeff# Évidence 😀\r\n".encode()
        source = archive(
            [
                ("folder/", b""),
                ("folder/policy.md", raw),
                ("notes.txt", b"Notes\n"),
                ("__MACOSX/._policy.md", b"junk"),
                (".DS_Store", b"junk"),
            ]
        )
        digest = hashlib.sha256(source.read()).hexdigest()
        source.seek(0)
        files = read_uploads([source], "Batch note")
        self.assertEqual([f["name"] for f in files], ["policy.md", "notes.txt"])
        self.assertEqual(files[0]["text"].encode(), raw)
        self.assertEqual(
            files[0]["metadata"]["source_archive"],
            dict(name="texts.zip", member="folder/policy.md", sha256=digest),
        )

    def test_traversal_absolute_windows_and_nul_paths_are_rejected(self):
        for name in [
            "../bad.txt",
            "/bad.txt",
            "dir/../../bad.txt",
            "C:/bad.txt",
            "C:bad.txt",
            "dir\\bad.txt",
            "dir//bad.txt",
            "bad\nfolder/file.txt",
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_uploads([archive([(name, b"text")])], "")
        original = archive([("badX.txt", b"text")]).read()
        malicious = original.replace(b"badX.txt", b"bad\x00.txt")
        with self.assertRaises(ValueError):
            read_uploads([SimpleUploadedFile("bad.zip", malicious)], "")

    def test_symlinks_special_files_and_encryption_are_rejected(self):
        for mode in [stat.S_IFLNK, stat.S_IFIFO]:
            info = zipfile.ZipInfo("link.txt")
            info.create_system = 3
            info.external_attr = (mode | 0o777) << 16
            with self.assertRaises(ValueError):
                read_uploads([archive([(info, b"target")])], "")
        payload = bytearray(archive([("secret.txt", b"text")]).read())
        local = payload.index(b"PK\x03\x04")
        central = payload.index(b"PK\x01\x02")
        payload[local + 6] |= 1
        payload[central + 8] |= 1
        with self.assertRaisesMessage(ValueError, "Encrypted"):
            read_uploads([SimpleUploadedFile("private.zip", bytes(payload))], "")

    def test_corrupt_zip_crc_and_unsupported_compression(self):
        bad = (
            archive([("plain.txt", b"UNIQUE-CONTENT")], compression=zipfile.ZIP_STORED)
            .read()
            .replace(b"UNIQUE-CONTENT", b"BROKEN-CONTENT")
        )
        for raw in [b"not a zip", bad, archive([("plain.txt", b"text")]).read()[:-15]]:
            with self.subTest(size=len(raw)), self.assertRaisesMessage(
                ValueError, "Cannot read"
            ):
                read_uploads([SimpleUploadedFile("broken.zip", raw)], "")
        with self.assertRaises(ValueError):
            read_uploads(
                [archive([("plain.txt", b"text")], compression=zipfile.ZIP_LZMA)], ""
            )

    def test_invalid_members_and_empty_archives_fail_whole_batch(self):
        for entries in [
            [],
            [("a.txt", b"valid"), ("b.md", b"\xff")],
            [("a.txt", b"valid"), ("a.pdf", b"PDF")],
            [("nested.zip", b"PK")],
            [("empty.txt", b"")],
            [("binary.txt", b"\x00")],
            [(".DS_Store", b"junk")],
        ]:
            with self.subTest(entries=[str(e[0]) for e in entries]), self.assertRaises(
                ValueError
            ):
                read_uploads([archive(entries)], "")

    def test_expanded_size_file_count_entry_count_and_aggregate_limits(self):
        with patch("corpus.upload_files.MAX_BYTES", 1024):
            with self.assertRaisesMessage(ValueError, "20 MB"):
                read_uploads([archive([("large.txt", b"x" * 5000)])], "")
            with self.assertRaisesMessage(ValueError, "20 MB"):
                read_uploads(
                    [
                        archive([("a.txt", b"x" * 800)]),
                        archive([("b.txt", b"x" * 800)], name="other.zip"),
                    ],
                    "",
                )
        with self.assertRaisesMessage(ValueError, "100 documents"):
            read_uploads([archive([(f"{n}.txt", b"x") for n in range(101)])], "")
        with patch("corpus.upload_files.MAX_ZIP_ENTRIES", 2):
            with self.assertRaisesMessage(ValueError, "1000 entries"):
                read_uploads(
                    [archive([("a/", b""), ("b/", b""), ("a/c.txt", b"text")])], ""
                )


@override_settings(CASE_EMAIL_ENABLED=False)
class ZipWorkflowTests(TestCase):
    def setUp(self):
        self.d = report_fixture()
        self.study = self.d["study"]
        self.owner = self.d["admin"]
        self.client.force_login(self.owner)

    def test_confirm_preserves_archive_provenance_and_does_not_assign(self):
        batch = preview_upload(
            self.study,
            self.owner,
            [
                archive(
                    [("nested/extra.md", "A😀\r\n".encode()), ("notes.txt", b"Notes\n")]
                )
            ],
            str(self.d["case"].pk),
        )
        self.assertEqual(self.study.documents.count(), 1)
        result = commit_upload(
            batch.pk,
            self.study.pk,
            self.owner,
            [
                dict(url="https://example.org/source", notes="Revised")
                for f in batch.data["files"]
            ],
        )
        version = DocumentVersion.objects.get(pk=result.data["files"][0]["version"])
        self.assertEqual(version.text, "A😀\r\n")
        self.assertEqual(
            version.metadata["source_archive"]["member"], "nested/extra.md"
        )
        self.assertEqual(version.metadata["notes"], "Revised")
        self.assertEqual(Annotation.objects.count(), 4)
        self.assertEqual(self.d["assignments"][0].documents.count(), 1)
        commit_upload(batch.pk, self.study.pk, self.owner, [])
        self.assertEqual(self.study.documents.count(), 3)

    def test_collisions_and_invalid_archives_leave_no_preview_or_corpus_records(self):
        for inputs in [
            [archive([("a/same.md", b"a"), ("b/same.md", b"b")])],
            [archive([("a/policy.md", b"a"), ("b/policy.txt", b"b")])],
            [archive([("a.txt", b"a")]), SimpleUploadedFile("a.txt", b"b")],
            [archive([("good.txt", b"a"), ("bad.txt", b"\xff")])],
        ]:
            with self.assertRaises(ValueError):
                preview_upload(self.study, self.owner, inputs, str(self.d["case"].pk))
            self.assertEqual(CorpusUploadBatch.objects.count(), 0)
            self.assertEqual(self.study.documents.count(), 1)

    def test_zip_http_preview_confirmation_and_error_are_human_readable(self):
        url = reverse("corpus-upload", args=[self.study.pk])
        response = self.client.post(
            url,
            dict(
                action="preview",
                case=str(self.d["case"].pk),
                files=archive([("docs/hello.md", b"Hello\n")]),
            ),
        )
        self.assertEqual(response.status_code, 302)
        preview = self.client.get(response["Location"])
        self.assertContains(preview, "docs/hello.md")
        self.assertContains(preview, "Confirm import")
        self.assertEqual(self.study.documents.count(), 1)
        response = self.client.post(
            url,
            dict(
                action="preview",
                case=str(self.d["case"].pk),
                files=SimpleUploadedFile("bad.zip", b"broken"),
            ),
        )
        self.assertContains(response, "Cannot read bad.zip", status_code=400)
        self.assertEqual(CorpusUploadBatch.objects.count(), 1)

    def test_zip_files_and_provenance_are_published_to_github(self):
        from .test_github_sync import fixture_files, file_tree, FakeGit
        from .github_sync import import_study, synchronize_next
        from .github_repository import inspect_repository, replay_codebook

        files, _ = fixture_files()
        fake = FakeGit(files)
        study = import_study(
            self.owner,
            dict(id=199, full_name="research/zip"),
            "main",
            7,
            fake.head("", ""),
            files,
            file_tree(files),
            "ZIP study",
        )
        synchronize_next(force=True, client_factory=lambda *_: fake)
        batch = preview_upload(
            study,
            self.owner,
            [
                archive(
                    [("nested/extra.md", "A😀\r\n".encode()), ("notes.txt", b"Notes\n")]
                )
            ],
            str(study.cases.get(key="Alpha").pk),
        )
        commit_upload(
            batch.pk,
            study.pk,
            self.owner,
            [dict(url="", notes="") for f in batch.data["files"]],
        )
        synchronize_next(force=True, client_factory=lambda *_: fake)
        self.assertEqual(fake.files["corpus/Alpha/extra.md"], "A😀\r\n")
        inspected = inspect_repository(fake.files, 199)
        extra = next(
            d for d in inspected["documents"] if d["path"] == "corpus/Alpha/extra.md"
        )
        self.assertEqual(
            extra["metadata"]["source_archive"]["member"], "nested/extra.md"
        )
        replay_codebook(fake.files)
        self.assertFalse(study.github.events.filter(published=False).exists())
