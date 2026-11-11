from copy import deepcopy
import datetime
import io
import json
from pathlib import Path
import tempfile
import uuid
from unittest.mock import patch
import yaml
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction, IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .models import (
    GitHubStudy,
    GitHubRun,
    GitHubEvent,
    Case,
    DocumentVersion,
    StudyAdmin,
    Assignment,
    Annotation,
    SharedCodebook,
    Study,
)
from .github_repository import (
    dump,
    digest,
    blob_sha,
    inspect_repository,
    replay_codebook,
    convert_book,
    coding_path,
    verify_directory,
)
from .github_client import GitHubClient, GitHubError
from .github_sync import (
    import_study,
    export_files,
    record_event,
    claim_binding,
    prepare_run,
    synchronize_next,
)
from .services import (
    create_session,
    submit,
    withdraw_submission,
    remove_assignment,
    delete_coder_session,
    merge_submission,
)
from .studies import publish_codebook, publish_instructions
from .importers import put_document


def fixture_files():
    code = str(uuid.uuid4())
    book = dict(
        version=1,
        provenance={"source": "Keep this metadata"},
        codes=[
            dict(
                id=code,
                name="Review",
                kind="code",
                parent=None,
                memo="Verified quote",
                rationale="Researcher guidance",
                references=[],
            )
        ],
    )
    files = {
        "codebook/codebook.yaml": dump(book),
        "corpus/Alpha/policy.md": "A😀 review\r\n",
        "corpus/Beta/README": "Other policy\n",
    }
    return files, code


def file_tree(files):
    return {
        p: dict(
            path=p,
            type="blob",
            mode="100644",
            sha=blob_sha(content),
            size=len(content.encode()),
        )
        for p, content in files.items()
    }


class FakeGit:
    def __init__(self, files):
        self.files = deepcopy(files)
        self.sha = "a" * 40
        self.commits = []
        self.lose_response = False
        self.fail_before = False

    def head(self, repo, branch):
        return dict(sha=self.sha, tree="tree")

    def tree(self, repo, tree):
        return file_tree(
            {
                p: c
                for p, c in self.files.items()
                if p.startswith(("corpus/", "codebook/"))
            }
        )

    def read_files(self, repo, tree):
        return {path: self.files[path] for path in tree}

    def commit_files(self, repo, branch, head, files, message, heartbeat):
        heartbeat()
        if self.fail_before:
            raise GitHubError("GitHub unavailable")
        if self.sha != head["sha"]:
            raise GitHubError("Branch advanced", conflict=True)
        self.files.update(files)
        self.commits.append(
            dict(files=deepcopy(files), parent=self.sha, message=message)
        )
        self.sha = f"{len(self.commits):040x}"
        if self.lose_response:
            self.lose_response = False
            raise GitHubError("Response lost after successful publication")
        return self.sha


@override_settings(CASE_EMAIL_ENABLED=False)
class GitHubSyncTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", is_staff=True)
        self.files, self.code = fixture_files()
        self.fake = FakeGit(self.files)
        self.study = import_study(
            self.owner,
            dict(id=99, full_name="research/data"),
            "main",
            7,
            self.fake.head("", ""),
            self.files,
            file_tree(self.files),
            "Research",
        )
        self.binding = self.study.github
        self.client.force_login(self.owner)

    def sync(self):
        return synchronize_next(force=True, client_factory=lambda *_: self.fake)

    def new_coder(self, name="Alice"):
        return create_session(
            self.study,
            name,
            list(self.study.cases.all()),
            email="never-export@example.org",
        )[0]

    def annotate_submit(self, session):
        assignment = session.assignment_set.get(case__key="Alpha")
        doc = assignment.documents.get()
        Annotation.objects.create(
            assignment=assignment,
            document=doc,
            start=1,
            end=3,
            selected_text="😀",
            codes=[self.code],
            memo="Evidence memo",
        )
        return submit(assignment.pk, session.pk, assignment.revision)

    def test_import_preserves_bytes_metadata_and_atomic_freeze(self):
        doc = DocumentVersion.objects.get(document__key="corpus/Alpha/policy.md")
        self.assertEqual(doc.text, "A😀 review\r\n")
        self.assertEqual(
            doc.sha256, __import__("hashlib").sha256(doc.text.encode()).hexdigest()
        )
        with self.assertRaisesMessage(ValueError, "frozen"), transaction.atomic():
            Case.objects.create(study=self.study, key="new", name="New")
        with self.assertRaisesMessage(ValueError, "frozen"), transaction.atomic():
            put_document(self.study, "new", "new", "changed", {}, [])
        with self.assertRaisesMessage(ValueError, "frozen"), transaction.atomic():
            doc.document.cases.clear()
        response = self.client.post(
            reverse("study-corpus", args=[self.study.pk]),
            dict(action="case", name="New"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(
            self.client.get(reverse("study-corpus", args=[self.study.pk])),
            "Create a case",
        )

    def test_replay_all_revisions_preserves_provenance_and_personal_coding(self):
        session = self.new_coder()
        submission = self.annotate_submit(session)
        book = deepcopy(self.study.codebook.content)
        book["codes"][0]["name"] = "Renamed"
        category = str(uuid.uuid4())
        book["codes"].append(
            dict(
                id=category,
                name="Category",
                kind="category",
                parent=None,
                memo="Category label",
            )
        )
        book["codes"][0]["parent"] = category
        first = publish_codebook(
            self.study,
            book,
            expected_revision=self.study.codebook_id,
            actor=self.owner.pk,
        )
        book["codes"][0]["archived"] = True
        publish_codebook(
            self.study, book, expected_revision=first.pk, actor=self.owner.pk
        )
        files = export_files(self.binding)
        self.assertEqual(replay_codebook(files)[1], book)
        self.assertEqual(
            len(yaml.safe_load(files["codebook/history.yaml"])["changes"]), 2
        )
        paths = [p for p in files if "/submissions/" in p]
        exported = yaml.safe_load(files[paths[0]])
        self.assertEqual(exported["annotations"][0]["selected_text"], "😀")
        self.assertEqual(exported["annotations"][0]["start"], 1)
        self.assertEqual(exported["offset_encoding"], "UTF-16")
        personal = yaml.safe_load(files[exported["codebook"]["path"]])
        self.assertEqual(personal["codes"][0]["name"], "Review")
        self.assertEqual(personal["provenance"], {"source": "Keep this metadata"})
        self.assertNotIn("never-export@example.org", "".join(files.values()))
        self.assertNotIn("token_hash", "".join(files.values()))
        self.assertEqual(files, export_files(self.binding))
        with tempfile.TemporaryDirectory() as root:
            for path, text in files.items():
                destination = Path(root) / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(text.encode())
            self.assertEqual(verify_directory(root), book)
            output = io.StringIO()
            call_command("verify_github_export", root, stdout=output)
            self.assertIn("Verified codebook", output.getvalue())

    def test_two_coders_same_name_and_withdraw_archive_keep_immutable_snapshots(self):
        alice = self.new_coder("Same")
        bob = self.new_coder("Same")
        first = self.annotate_submit(alice)
        self.annotate_submit(bob)
        original = export_files(self.binding)
        paths = [p for p in original if "/submissions/" in p]
        self.assertEqual(len(paths), 2)
        self.assertTrue(any(str(alice.pk) in p for p in paths))
        self.assertTrue(any(str(bob.pk) in p for p in paths))

    def test_withdraw_and_resubmit_status_and_review_mappings(self):
        session = self.new_coder()
        first = self.annotate_submit(session)
        previous = export_files(self.binding)
        path = next(p for p in previous if "/submissions/" in p)
        withdraw_submission(first.pk)
        second = submit(first.assignment_id, session.pk, 2)
        self.study.refresh_from_db()
        merge_submission(second.pk, self.owner, self.study.codebook_id, {})
        remove_assignment(first.assignment_id, 3)
        delete_coder_session(session.pk)
        current = export_files(self.binding)
        self.assertEqual(previous[path], current[path])
        status = yaml.safe_load(
            next(v for p, v in current.items() if p.endswith("/status.yaml"))
        )["submissions"]
        self.assertEqual(len(status), 2)
        self.assertTrue(status[0]["withdrawn_at"])
        self.assertTrue(status[0]["superseded_by"])
        self.assertFalse(any(s["active"] for s in status))
        self.assertTrue(status[1]["session_archived_at"])
        self.assertTrue(any("/reviews/" in p for p in current))

    def test_personal_changes_are_not_published_before_submission(self):
        session = self.new_coder()
        session.codebook["codes"][0]["name"] = "Draft personal concept"
        session.save(update_fields=["codebook"])
        files = export_files(self.binding)
        self.assertNotIn("Draft personal concept", "".join(files.values()))
        self.assertFalse(any("/coders/" in p for p in files))

    def test_event_insert_rolls_back_with_domain_change(self):
        count = self.binding.events.count()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                record_event(self.study.pk, "test")
                raise RuntimeError()
        self.assertEqual(self.binding.events.count(), count)
        before = self.study.codebook_id
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                publish_codebook(
                    self.study, self.study.codebook.content, expected_revision=before
                )
                raise RuntimeError()
        self.study.refresh_from_db()
        self.assertEqual(self.study.codebook_id, before)
        self.assertEqual(self.binding.events.count(), count)

    def test_debounce_lease_pause_and_no_duplicate_empty_commit(self):
        self.assertIsNone(claim_binding())
        self.assertTrue(self.sync())
        self.assertEqual(len(self.fake.commits), 1)
        self.assertFalse(self.sync())
        record_event(self.study.pk, "unchanged")
        self.assertTrue(self.sync())
        self.assertEqual(len(self.fake.commits), 1)
        self.binding.refresh_from_db()
        self.binding.paused = True
        self.binding.save(update_fields=["paused"])
        record_event(self.study.pk, "paused")
        self.assertFalse(self.sync())
        self.binding.paused = False
        self.binding.locked_until = timezone.now() + datetime.timedelta(minutes=1)
        self.binding.save(update_fields=["paused", "locked_until"])
        self.assertFalse(self.sync())

    def test_response_lost_after_commit_is_recovered_without_duplicate(self):
        self.fake.lose_response = True
        self.sync()
        self.assertEqual(len(self.fake.commits), 1)
        run = GitHubRun.objects.get()
        self.assertEqual(run.status, "retrying")
        self.binding.refresh_from_db()
        self.binding.next_attempt_at = timezone.now()
        self.binding.save(update_fields=["next_attempt_at"])
        self.sync()
        run.refresh_from_db()
        self.assertEqual(run.status, "published")
        self.assertEqual(len(self.fake.commits), 1)

    def test_external_managed_and_frozen_changes_block_but_unrelated_changes_survive(
        self,
    ):
        self.fake.files["README.md"] = "Unrelated data"
        self.sync()
        self.assertEqual(self.fake.files["README.md"], "Unrelated data")
        record_event(self.study.pk, "changed")
        self.fake.files["codebook/codebook.yaml"] = "changed externally"
        self.sync()
        self.binding.refresh_from_db()
        self.assertTrue(self.binding.blocked)
        self.assertIn("changed externally", self.binding.last_error)
        self.fake.files["codebook/codebook.yaml"] = export_files(self.binding)[
            "codebook/codebook.yaml"
        ]
        self.client.post(
            reverse("github-status", args=[self.study.pk]), dict(action="retry")
        )
        self.fake.files["corpus/Alpha/policy.md"] = "Changed corpus"
        self.sync()
        self.binding.refresh_from_db()
        self.assertTrue(self.binding.blocked)
        self.assertIn("Frozen corpus", self.binding.last_error)

    def test_events_during_prepared_run_are_published_in_the_following_run(self):
        binding = claim_binding(force=True)
        run = prepare_run(binding)
        self.annotate_submit(self.new_coder())
        GitHubStudy.objects.filter(pk=binding.pk).update(
            lease_token=None, locked_until=None
        )
        self.sync()
        self.binding.refresh_from_db()
        self.assertFalse(
            self.binding.events.filter(pk__in=run.event_ids, published=False).exists()
        )
        self.assertTrue(self.binding.events.filter(published=False).exists())
        self.assertFalse(any("/submissions/" in p for p in self.fake.files))
        self.sync()
        self.assertTrue(any("/submissions/" in p for p in self.fake.files))

    def test_permissions_and_retry_ui(self):
        other = get_user_model().objects.create_user("other", is_staff=True)
        self.client.force_login(other)
        url = reverse("github-status", args=[self.study.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        StudyAdmin.objects.create(study=self.study, user=other, granted_by=self.owner)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, dict(action="pause")).status_code, 403)
        self.assertEqual(self.client.post(url, dict(action="retry")).status_code, 302)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url, dict(action="pause")).status_code, 302)
        self.binding.refresh_from_db()
        self.assertTrue(self.binding.paused)


class RepositoryFormatTests(TestCase):
    def test_extensions_collisions_and_invalid_history(self):
        files, _ = fixture_files()
        self.assertEqual(
            coding_path("corpus/Alpha/policy.en.md"), "corpus/Alpha/policy.en.coding"
        )
        self.assertEqual(
            coding_path("corpus/Alpha/README"), "corpus/Alpha/README.coding"
        )
        files["corpus/Alpha/policy.txt"] = "Other"
        with self.assertRaisesMessage(ValueError, "collision"):
            inspect_repository(files)
        files.pop("corpus/Alpha/policy.txt")
        files["codebook/codebook-base.yaml"] = dump(dict(version=1, codes=[]))
        with self.assertRaisesMessage(ValueError, "reconstruct"):
            inspect_repository(files)

    def test_manifest_metadata_paths_and_portable_corpus_references(self):
        files, _ = fixture_files()
        book = yaml.safe_load(files["codebook/codebook.yaml"])
        text = files["corpus/Alpha/policy.md"]
        sha = __import__("hashlib").sha256(text.encode()).hexdigest()
        book["codes"][0]["references"] = [
            dict(
                type="corpus",
                document="corpus/Alpha/policy.md",
                hash=sha,
                start=1,
                end=3,
                quote="😀",
            )
        ]
        files["codebook/codebook.yaml"] = dump(book)
        files["codebook/study.yaml"] = dump(
            dict(
                version=1,
                cases=[
                    dict(
                        key="Alpha",
                        name="Named case",
                        metadata={"url": "https://example.org"},
                    )
                ],
                documents=[
                    dict(
                        path="corpus/Alpha/policy.md",
                        id="stable-document",
                        metadata={"source": "Policy"},
                    )
                ],
            )
        )
        data = inspect_repository(files)
        self.assertEqual(data["cases"][0]["name"], "Named case")
        self.assertEqual(data["documents"][0]["id"], "stable-document")
        paths = {"42": data["documents"][0]}
        internal = convert_book(book, paths, importing=True)
        self.assertEqual(internal["codes"][0]["references"][0]["document"], 42)
        self.assertEqual(convert_book(internal, paths), book)
        files["corpus/../escape.md"] = "bad"
        with self.assertRaises(ValueError):
            inspect_repository(files)

    def test_patch_hash_tampering_and_unlisted_diffs(self):
        files, _ = fixture_files()
        files["codebook/diffs/date/change.yaml"] = "{}"
        with self.assertRaisesMessage(ValueError, "missing from history"):
            replay_codebook(files)


from django.test import TransactionTestCase, skipUnlessDBFeature
from django.db import connections
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


class GitHubWorkerConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_two_workers_claim_only_one_lease(self):
        owner = get_user_model().objects.create_user("worker-owner", is_staff=True)
        files, _ = fixture_files()
        fake = FakeGit(files)
        study = import_study(
            owner,
            dict(id=99, full_name="research/data"),
            "main",
            7,
            fake.head("", ""),
            files,
            file_tree(files),
            "Concurrent study",
        )
        barrier = Barrier(2)

        def claim(_):
            try:
                barrier.wait(timeout=10)
                value = claim_binding(force=True)
                return value.pk if value else None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(results.count(None), 1)
        self.assertIn(study.github.pk, results)

    @skipUnlessDBFeature("has_select_for_update")
    def test_prepared_export_uses_real_repeatable_read_snapshot(self):
        owner = get_user_model().objects.create_user("snapshot-owner", is_staff=True)
        files, _ = fixture_files()
        fake = FakeGit(files)
        study = import_study(
            owner,
            dict(id=99, full_name="research/data"),
            "main",
            7,
            fake.head("", ""),
            files,
            file_tree(files),
            "Snapshot study",
        )
        binding = claim_binding(force=True)
        run = prepare_run(binding)
        self.assertEqual(run.target_event, study.github.events.get().pk)
        self.assertEqual(replay_codebook(run.files)[1], study.codebook.content)


class MoreGitHubSyncTests(TestCase):
    setUp = GitHubSyncTests.setUp
    sync = GitHubSyncTests.sync

    def test_unreferenced_add_delete_and_imported_history_are_replayable(self):
        book = deepcopy(self.study.codebook.content)
        temporary = dict(
            id=str(uuid.uuid4()), name="Temporary", parent=None, kind="code"
        )
        book["codes"].append(temporary)
        revision = publish_codebook(
            self.study, book, expected_revision=self.study.codebook_id
        )
        book["codes"].pop()
        publish_codebook(self.study, book, expected_revision=revision.pk)
        files = export_files(self.binding)
        self.assertEqual(replay_codebook(files)[1], book)
        imported = import_study(
            self.owner,
            dict(id=100, full_name="research/other"),
            "main",
            8,
            self.fake.head("", ""),
            {**self.files, **files},
            file_tree({**self.files, **files}),
            "Replayed study",
        )
        self.assertEqual(imported.codebook.content, book)
        self.assertEqual(replay_codebook(export_files(imported.github))[1], book)

    def test_concurrent_unrelated_ref_update_is_retried_without_force(self):
        original = self.fake.commit_files

        def raced(repo, branch, head, files, message, heartbeat):
            self.fake.sha = "b" * 40
            self.fake.files["README.md"] = "Concurrent external edit"
            self.fake.commit_files = original
            raise GitHubError("Branch advanced", conflict=True)

        self.fake.commit_files = raced
        self.sync()
        self.binding.refresh_from_db()
        self.assertFalse(self.binding.blocked)
        self.binding.next_attempt_at = timezone.now()
        self.binding.save(update_fields=["next_attempt_at"])
        self.sync()
        self.assertEqual(len(self.fake.commits), 1)
        self.assertEqual(self.fake.files["README.md"], "Concurrent external edit")

    def test_late_committing_event_with_lower_id_is_not_lost(self):
        self.sync()
        first = self.binding.events.get()
        late = GitHubEvent.objects.create(
            pk=first.pk - 1, binding=self.binding, kind="late-commit"
        )
        self.assertTrue(self.sync())
        late.refresh_from_db()
        self.assertTrue(late.published)
        self.assertEqual(self.binding.runs.count(), 2)
        self.assertEqual(len(self.fake.commits), 1)

    def test_expired_worker_cannot_overwrite_a_new_owners_completed_run(self):
        def stale_failure(repo, branch, head, files, message, heartbeat):
            GitHubStudy.objects.filter(pk=self.binding.pk).update(
                lease_token=uuid.uuid4(),
                locked_until=timezone.now() + datetime.timedelta(minutes=1),
            )
            GitHubRun.objects.filter(binding=self.binding).update(
                status="published", commit="b" * 40
            )
            raise GitHubError("Late failure from expired worker")

        self.fake.commit_files = stale_failure
        self.sync()
        run = self.binding.runs.get()
        self.assertEqual(run.status, "published")
        self.assertEqual(run.commit, "b" * 40)
        self.assertEqual(run.error, "")

    def test_only_one_prepared_run_can_exist_per_study(self):
        binding = claim_binding(force=True)
        prepare_run(binding)
        with self.assertRaises(IntegrityError), transaction.atomic():
            GitHubRun.objects.create(binding=binding, target_event=1, files={})
