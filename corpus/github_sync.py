"""Durable, study-scoped repository projection and optimistic Git publication."""

from copy import deepcopy
import datetime
import uuid
from django.conf import settings
from django.db import transaction, connection
from django.db.models import Q
from django.utils import timezone
from .models import (
    GitHubStudy,
    GitHubEvent,
    GitHubRun,
    Study,
    SharedCodebook,
    Case,
    DocumentVersion,
    Submission,
    MergeAudit,
)
from .github_client import GitHubError, installation_client
from .github_repository import (
    inspect_repository,
    convert_book,
    external_id,
    dump,
    digest,
    blob_sha,
    replay_codebook,
    load_yaml,
    corpus_paths,
    coding_path,
    make_patch,
)


def record_event(study_id, kind, *, revision=None, actor=""):
    binding = GitHubStudy.objects.filter(study_id=study_id).first()
    if binding:
        # This insert is part of the caller's domain transaction, never an on_commit callback.
        return GitHubEvent.objects.create(
            binding=binding, kind=kind, revision=revision, actor=str(actor)
        )


@transaction.atomic
def import_study(
    owner,
    repository,
    branch,
    installation,
    head,
    files,
    tree,
    title,
    *,
    bootstrap=False,
):
    from .studies import create_study, publish_instructions
    from .importers import put_document
    from .models import ResearchQuestion, CodingGuideline

    if bootstrap and tree:
        raise ValueError(
            "Upload initialization cannot replace existing repository data"
        )
    data = inspect_repository(files, repository["id"])
    if GitHubStudy.objects.filter(
        repository_id=repository["id"], branch=branch
    ).exists():
        raise ValueError("This repository and branch already belong to a study")
    study = create_study(
        owner, title, data["manifest"].get("study", {}).get("description", "")
    )
    cases = {
        item["key"]: Case.objects.create(study=study, **item) for item in data["cases"]
    }
    paths = {}
    for doc in data["documents"]:
        version = put_document(
            study,
            doc["path"],
            doc["name"],
            doc["text"],
            doc["metadata"],
            [cases[doc["case"]]],
        )
        paths[str(version.pk)] = {
            key: value for key, value in doc.items() if key != "text"
        }
    current = SharedCodebook.objects.create(
        study=study, content=convert_book(data["book"], paths, importing=True)
    )
    study.codebook = current
    study.save(update_fields=["codebook"])
    instructions = data["manifest"].get("instructions", {}).get("content", {})
    question_ids = {}
    for question in instructions.get("research_questions", []):
        item = ResearchQuestion.objects.create(
            study=study,
            label=question["label"],
            text=question["text"],
            position=question.get("position", 0),
        )
        question_ids[question["id"]] = item
    for guideline in instructions.get("guidelines", []):
        item = CodingGuideline.objects.create(
            study=study,
            title=guideline["title"],
            text=guideline["text"],
            position=guideline.get("position", 0),
        )
        try:
            item.questions.set(
                [question_ids[key] for key in guideline.get("questions", [])]
            )
        except KeyError:
            raise ValueError(
                "Manifest guideline refers to an unknown research question"
            ) from None
    if instructions:
        publish_instructions(study, owner)
    source_paths = {doc["path"] for doc in data["documents"]}
    binding = GitHubStudy.objects.create(
        study=study,
        bootstrap_pending=bootstrap,
        repository_id=repository["id"],
        repository=repository["full_name"],
        installation_id=installation,
        branch=branch,
        initial_commit=head["sha"],
        last_commit=head["sha"],
        initial_revision=current,
        initial_files={
            path: content
            for path, content in files.items()
            if bootstrap or path.startswith("codebook/")
        },
        corpus_blobs={
            path: (
                {"sha": blob_sha(files[path]), "mode": "100644"}
                if bootstrap
                else {"sha": tree[path]["sha"], "mode": tree[path]["mode"]}
            )
            for path in source_paths
        },
        managed_blobs={path: item["sha"] for path, item in tree.items()},
        document_paths=paths,
    )
    record_event(study.pk, "initialized", actor=owner.pk)
    return study


def portable_instructions(study_id, instructions):
    return dict(
        id=external_id(study_id, "instructions", instructions["revision"]),
        content=deepcopy(instructions["content"]),
    )


def export_files(binding):
    study = Study.objects.select_related("codebook", "instructions").get(
        pk=binding.study_id
    )
    files = deepcopy(binding.initial_files)
    base, initial, history = replay_codebook(files)
    files.setdefault("codebook/codebook-base.yaml", dump(base))
    current = deepcopy(initial)
    events = {
        event.revision_id: event
        for event in binding.events.filter(revision__isnull=False).order_by("pk")
    }
    for revision in SharedCodebook.objects.filter(
        study=study, pk__gt=binding.initial_revision_id
    ).order_by("pk"):
        result = convert_book(revision.content, binding.document_paths)
        sequence = len(history["changes"]) + 1
        path = f"codebook/diffs/{revision.created.strftime('%Y%m%dT%H%M%S%fZ')}--{sequence:06d}/change.yaml"
        event = events.get(revision.pk)
        change = dict(
            version=1,
            sequence=sequence,
            created=revision.created.isoformat(),
            revision=external_id(study.pk, "codebook", revision.pk),
            origin=event.kind if event else "codebook-publication",
            before=digest(current),
            after=digest(result),
            operations=make_patch(current, result),
        )
        if event and event.actor:
            change["actor"] = external_id(study.pk, "admin", event.actor)
        history["changes"].append(
            {key: change[key] for key in ("sequence", "before", "after")}
            | {"path": path}
        )
        files[path] = dump(change)
        current = result
    expected = convert_book(study.codebook.content, binding.document_paths)
    if current != expected:
        raise ValueError(
            "Shared codebook revisions do not reconstruct the current codebook"
        )
    files["codebook/history.yaml"] = dump(history)
    files["codebook/codebook.yaml"] = dump(current)
    manifest = load_yaml(files.get("codebook/study.yaml", "version: 1"))
    manifest.update(
        version=1,
        study=dict(id=str(study.pk), title=study.title, description=study.description),
        initial_commit=binding.initial_commit,
        shared_codebook=dict(
            initial_revision=external_id(
                study.pk, "codebook", binding.initial_revision_id
            ),
            revision=external_id(study.pk, "codebook", study.codebook_id),
            hash=digest(current),
            initial_hash=digest(initial),
        ),
        cases=[
            dict(key=c.key, name=c.name, metadata=c.metadata)
            for c in sorted(study.cases.all(), key=lambda c: c.key)
        ],
        documents=sorted(binding.document_paths.values(), key=lambda d: d["path"]),
        instructions=portable_instructions(
            study.pk,
            dict(revision=study.instructions_id, content=study.instructions.content),
        ),
    )
    files["codebook/study.yaml"] = dump(manifest)
    submissions = list(
        Submission.objects.filter(assignment__session__study=study)
        .select_related("assignment__session", "withdrawal")
        .order_by("pk")
    )
    latest = {s.assignment_id: s.pk for s in submissions}
    reviews = {
        a.submission_id: a
        for a in MergeAudit.objects.filter(submission__in=submissions)
    }
    for doc in binding.document_paths.values():
        files[coding_path(doc["path"]) + "/document.yaml"] = dump(
            {key: doc[key] for key in ("id", "path", "name", "hash", "metadata")}
        )
    statuses = {}
    for submission in submissions:
        snapshot = submission.snapshot
        session = submission.assignment.session
        sid = str(session.pk)
        external = external_id(study.pk, "submission", submission.pk)
        book = convert_book(snapshot["codebook"], binding.document_paths)
        book_path = f"codebook/coders/{sid}/{digest(book)}.yaml"
        files[book_path] = dump(book)
        for document in snapshot["documents"]:
            doc = binding.document_paths.get(str(document["id"]))
            if not doc or doc["hash"] != document["hash"]:
                raise ValueError("A submitted document differs from the frozen corpus")
            folder = coding_path(doc["path"])
            files[folder + "/document.yaml"] = dump(
                {key: doc[key] for key in ("id", "path", "name", "hash", "metadata")}
            )
            coder_folder = f"{folder}/{sid}"
            files[coder_folder + "/coder.yaml"] = dump(
                dict(
                    id=sid,
                    name=snapshot["coder"],
                    kind=snapshot.get("coder_kind", "private"),
                )
            )
            annotations = []
            version = None
            for annotation in snapshot["annotations"]:
                if annotation["document"] != document["id"]:
                    continue
                if version is None:
                    version = DocumentVersion.objects.get(pk=document["id"])
                from .services import span

                if (
                    annotation["document_hash"] != doc["hash"]
                    or span(version.text, annotation["start"], annotation["end"])
                    != annotation["selected_text"]
                ):
                    raise ValueError(
                        "A submitted annotation does not match its frozen document"
                    )
                annotations.append(
                    {
                        key: deepcopy(annotation[key])
                        for key in (
                            "id",
                            "start",
                            "end",
                            "selected_text",
                            "codes",
                            "memo",
                        )
                    }
                )
            payload = dict(
                version=1,
                id=external,
                created=submission.created.isoformat(),
                case=dict(
                    key=snapshot["case_key"],
                    name=snapshot["case"],
                    metadata=snapshot.get("case_metadata", {}),
                ),
                document=dict(id=doc["id"], path=doc["path"], hash=doc["hash"]),
                offset_encoding="UTF-16",
                coder=dict(id=sid, name=snapshot["coder"]),
                codebook=dict(
                    path=book_path,
                    hash=digest(book),
                    base=external_id(study.pk, "codebook", snapshot["base"]),
                ),
                instructions=portable_instructions(study.pk, snapshot["instructions"]),
                annotations=annotations,
            )
            files[f"{coder_folder}/submissions/{external}.yaml"] = dump(payload)
            withdrawal = getattr(submission, "withdrawal", None)
            assignment = submission.assignment
            active = (
                latest[assignment.pk] == submission.pk
                and not withdrawal
                and not assignment.removed_at
                and not session.deleted_at
            )
            state = dict(
                id=external,
                active=active,
                withdrawn_at=withdrawal.created.isoformat() if withdrawal else None,
                assignment_archived_at=(
                    assignment.removed_at.isoformat() if assignment.removed_at else None
                ),
                session_archived_at=(
                    session.deleted_at.isoformat() if session.deleted_at else None
                ),
                superseded_by=(
                    external_id(study.pk, "submission", latest[assignment.pk])
                    if latest[assignment.pk] != submission.pk
                    else None
                ),
            )
            statuses.setdefault(coder_folder, []).append(state)
            audit = reviews.get(submission.pk)
            if audit:
                review_id = external_id(study.pk, "review", audit.pk)
                files[f"{coder_folder}/reviews/{review_id}.yaml"] = dump(
                    dict(
                        version=1,
                        id=review_id,
                        submission=external,
                        created=audit.created.isoformat(),
                        previous=external_id(study.pk, "codebook", audit.previous_id),
                        result=external_id(study.pk, "codebook", audit.result_id),
                        decisions=audit.decisions,
                        mappings=audit.mappings,
                    )
                )
    for folder, states in statuses.items():
        files[folder + "/status.yaml"] = dump(dict(version=1, submissions=states))
    replay_codebook(files)
    return files


class LeaseLost(GitHubError):
    pass


def heartbeat(binding_id, token):
    if not GitHubStudy.objects.filter(pk=binding_id, lease_token=token).update(
        locked_until=timezone.now()
        + datetime.timedelta(seconds=settings.GITHUB_SYNC_LEASE_SECONDS)
    ):
        raise LeaseLost("Synchronization lease was replaced")


@transaction.atomic
def claim_binding(force=False):
    now = timezone.now()
    query = (
        GitHubStudy.objects.select_for_update()
        .filter(paused=False, blocked=False, next_attempt_at__lte=now)
        .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
        .order_by("pk")
    )
    for binding in query:
        event = binding.events.filter(published=False).order_by("-pk").first()
        prepared = binding.runs.filter(status__in=["prepared", "retrying"]).exists()
        if not event or (
            not force
            and not prepared
            and event.created > now - datetime.timedelta(seconds=10)
        ):
            continue
        binding.lease_token = uuid.uuid4()
        binding.locked_until = now + datetime.timedelta(
            seconds=settings.GITHUB_SYNC_LEASE_SECONDS
        )
        binding.save(update_fields=["lease_token", "locked_until"])
        return binding
    return None


def prepare_run(binding):
    existing = (
        binding.runs.filter(status__in=["prepared", "retrying"])
        .order_by("created")
        .first()
    )
    if existing:
        return existing
    # No network calls inside this consistent database snapshot.
    nested = connection.in_atomic_block
    with transaction.atomic():
        if connection.vendor == "postgresql" and not nested:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        fresh = GitHubStudy.objects.get(pk=binding.pk)
        event_ids = list(
            fresh.events.filter(published=False)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if not event_ids:
            raise LeaseLost("No pending synchronization events remain")
        target = max(event_ids)
        files = export_files(fresh)
        owner = GitHubStudy.objects.select_for_update().get(pk=fresh.pk)
        if owner.lease_token != binding.lease_token:
            raise LeaseLost(
                "Synchronization lease was replaced while preparing the snapshot"
            )
        run = GitHubRun.objects.create(
            binding=fresh, target_event=target, event_ids=event_ids, files=files
        )
    return run


def publish_run(binding, run, client):
    if binding.bootstrap_pending:
        head = client.initial_head(binding.repository, binding.branch)
        if not head["sha"]:
            heartbeat(binding.pk, binding.lease_token)
            client.seed_repository(
                binding.repository,
                binding.branch,
                "codebook/codebook-base.yaml",
                run.files["codebook/codebook-base.yaml"],
                f"Initialize Coding study {binding.study_id}",
            )
            head = client.initial_head(binding.repository, binding.branch)
    else:
        head = client.head(binding.repository, binding.branch)
    tree = client.tree(binding.repository, head["tree"])
    heartbeat(binding.pk, binding.lease_token)
    source_paths = set(corpus_paths(tree))
    actual_corpus = {
        path: {"sha": item["sha"], "mode": item["mode"]}
        for path, item in tree.items()
        if path in source_paths
    }
    desired = {path: blob_sha(content) for path, content in run.files.items()}
    complete = actual_corpus == binding.corpus_blobs and all(
        tree.get(path, {}).get("sha") == sha for path, sha in desired.items()
    )
    if binding.bootstrap_pending and not complete:
        seed = "codebook/codebook-base.yaml"
        if actual_corpus or any(
            path != seed or item["sha"] != desired[seed] or item.get("mode") != "100644"
            for path, item in tree.items()
        ):
            raise GitHubError(
                "Repository data appeared after the upload preview; no files were overwritten",
                conflict=True,
            )
    elif actual_corpus != binding.corpus_blobs:
        raise GitHubError(
            "Frozen corpus changed in GitHub; restore the imported files before retrying",
            conflict=True,
        )
    for path, item in tree.items():
        if path in binding.managed_blobs and item.get("mode") not in (
            "100644",
            "100755",
        ):
            raise GitHubError(
                f"Managed file type changed externally: {path}", conflict=True
            )
    for path, sha in binding.managed_blobs.items():
        if path not in desired and tree.get(path, {}).get("sha") != sha:
            raise GitHubError(f"Managed file changed externally: {path}", conflict=True)
    if all(tree.get(path, {}).get("sha") == sha for path, sha in desired.items()):
        return head["sha"], desired
    for path, sha in binding.managed_blobs.items():
        if tree.get(path, {}).get("sha") != sha:
            raise GitHubError(f"Managed file changed externally: {path}", conflict=True)
    for path in desired:
        if (
            path in tree
            and path not in binding.managed_blobs
            and not (
                binding.bootstrap_pending
                and path == "codebook/codebook-base.yaml"
                and tree[path]["sha"] == desired[path]
            )
        ):
            raise GitHubError(
                f"An external file occupies an export path: {path}", conflict=True
            )
    changed = {
        path: content
        for path, content in run.files.items()
        if tree.get(path, {}).get("sha") != desired[path]
    }
    try:
        commit = client.commit_files(
            binding.repository,
            binding.branch,
            head,
            changed,
            f"Coding study {binding.study_id}: publish {len(run.event_ids)} events (snapshot {run.pk})",
            lambda: heartbeat(binding.pk, binding.lease_token),
        )
    except GitHubError as error:
        if (
            error.conflict
            and client.head(binding.repository, binding.branch)["sha"] != head["sha"]
        ):
            raise GitHubError(
                "GitHub branch advanced during publication; retrying against the latest commit",
                retry_after=30,
            ) from None
        raise
    return commit, desired


def synchronize_next(*, force=False, client_factory=installation_client):
    binding = claim_binding(force=force)
    if not binding:
        return False
    run = None
    try:
        run = prepare_run(binding)
        run.attempts += 1
        run.save(update_fields=["attempts"])
        client = client_factory(binding.installation_id, binding.repository_id)
        commit, desired = publish_run(binding, run, client)
        with transaction.atomic():
            active = GitHubStudy.objects.select_for_update().get(pk=binding.pk)
            if active.lease_token != binding.lease_token:
                raise LeaseLost("Synchronization lease was replaced")
            active.managed_blobs.update(desired)
            active.last_commit = commit
            active.bootstrap_pending = False
            GitHubEvent.objects.filter(binding=active, pk__in=run.event_ids).update(
                published=True
            )
            active.last_error = ""
            active.locked_until = None
            active.lease_token = None
            active.save(
                update_fields=[
                    "managed_blobs",
                    "last_commit",
                    "bootstrap_pending",
                    "last_error",
                    "locked_until",
                    "lease_token",
                ]
            )
            run.status = "published"
            run.commit = commit
            run.error = ""
            run.finished = timezone.now()
            run.save(update_fields=["status", "commit", "error", "finished"])
    except LeaseLost:
        # The new owner of the lease will recover the prepared run.
        return True
    except (GitHubError, ValueError) as error:
        conflict = not isinstance(error, GitHubError) or error.conflict
        delay = max(
            getattr(error, "retry_after", 60),
            min(3600, 30 * 2 ** min((run.attempts if run else 1) - 1, 7)),
        )
        summary = str(error)[:500]
        with transaction.atomic():
            active = (
                GitHubStudy.objects.select_for_update()
                .filter(pk=binding.pk, lease_token=binding.lease_token)
                .first()
            )
            if active is None:
                return True
            active.blocked = conflict
            active.last_error = summary
            active.lease_token = None
            active.locked_until = None
            active.next_attempt_at = timezone.now() + datetime.timedelta(seconds=delay)
            active.save(
                update_fields=[
                    "blocked",
                    "last_error",
                    "lease_token",
                    "locked_until",
                    "next_attempt_at",
                ]
            )
            if run:
                GitHubRun.objects.filter(
                    pk=run.pk, status__in=["prepared", "retrying"]
                ).update(status="blocked" if conflict else "retrying", error=summary)
    return True


def require_mutable_corpus(study_id):
    if GitHubStudy.objects.filter(study_id=study_id).exists():
        raise ValueError("This study's corpus is frozen at its initial GitHub commit")
