"""Preview, atomic append-only corpus import, then explicit assignment."""

import datetime
import hashlib
import uuid
from django.core.exceptions import PermissionDenied
from django.core.validators import URLValidator
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from . import models as m
from .corpus_scope import append_scope
from .github_repository import safe_path, corpus_paths
from .importers import put_document
from .services import Conflict, assign_case
from .studies import get_study

from .upload_files import MAX_FILES, valid_name, read_uploads

AUTO_BINDING = object()


def check_names(study, case_id, names, storage_names=None):
    existing = (
        list(
            m.Document.objects.filter(study=study, cases__pk=case_id).values_list(
                "name", flat=True
            )
        )
        if case_id
        else []
    )
    used = {name.casefold() for name in existing}
    for name in names:
        if storage_names:
            safe_path(name)
            if not name.strip() or len(name) > 500 or any(ord(c) < 32 for c in name):
                raise ValueError("Invalid document title in manifest")
        else:
            valid_name(name)
        if name.casefold() in used:
            raise ValueError(
                f"A document named {name} already exists in this case or upload. Rename the new file; existing texts are never replaced."
            )
        used.add(name.casefold())
    # Reserve each sibling .coding directory even for studies not yet on GitHub.
    corpus_paths(
        {f"corpus/case/{name}": "" for name in existing + (storage_names or names)}
    )


def preview_upload(study, user, uploads, case_id="", new_case="", notes=""):
    get_study(user, study.pk)
    if not uploads or len(uploads) > MAX_FILES:
        raise ValueError("Choose between 1 and 100 files")
    if case_id:
        if new_case.strip():
            raise ValueError("Choose an existing case or provide a new case, not both")
        case = get_object_or_404(m.Case, pk=case_id, study=study)
        target = dict(id=case.pk, key=case.key, name=case.name)
    else:
        name = new_case.strip()
        if not name or len(name) > 500:
            raise ValueError(
                "Choose a case or provide a new case name of at most 500 characters"
            )
        if study.cases.filter(name__iexact=name).exists():
            raise ValueError("This case already exists. Select it from the case list.")
        target = dict(id=None, key=uuid.uuid4().hex, name=name)
    files = read_uploads(uploads, notes)
    check_names(study, target["id"], [f["name"] for f in files])
    from .management.commands.prune_corpus_uploads import prune

    prune()
    if (
        m.CorpusUploadBatch.objects.filter(
            study=study, created_by=user, committed_at__isnull=True
        ).count()
        >= 5
    ):
        raise ValueError(
            "You already have five pending previews. Confirm or cancel one before uploading more."
        )
    return m.CorpusUploadBatch.objects.create(
        study=study, created_by=user, data=dict(case=target, files=files)
    )


@transaction.atomic
def commit_upload(batch_id, study_id, user, metadata, *, _binding=AUTO_BINDING):
    study = m.Study.objects.select_for_update().get(pk=study_id)
    get_study(user, study.pk)
    batch = get_object_or_404(
        m.CorpusUploadBatch.objects.select_for_update(),
        pk=batch_id,
        study=study,
        created_by=user,
    )
    if batch.committed_at:
        return batch
    if batch.created < timezone.now() - datetime.timedelta(hours=1):
        raise ValueError("This preview has expired. Upload the files again.")
    data = batch.data
    if len(metadata) != len(data["files"]):
        raise ValueError("Document metadata does not match the preview")
    for item in metadata:
        if not isinstance(item["url"], str) or not isinstance(item["notes"], str):
            raise ValueError("Source URLs and notes must be text")
        if item["url"]:
            URLValidator(schemes=["https", "http"])(item["url"])
        if len(item["url"]) > 2000 or len(item["notes"]) > 10000:
            raise ValueError("Source URL or document notes are too long")
    target = data["case"]
    case = (
        get_object_or_404(m.Case, pk=target["id"], study=study)
        if target["id"]
        else None
    )
    if not case and study.cases.filter(name__iexact=target["name"]).exists():
        raise Conflict(
            "A case with this name was created after the preview. Upload again and select that case."
        )
    check_names(
        study,
        target["id"],
        [f["name"] for f in data["files"]],
        (
            [f.get("storage_name", f["name"]) for f in data["files"]]
            if any("storage_name" in f for f in data["files"])
            else None
        ),
    )
    binding = (
        m.GitHubStudy.objects.select_for_update().filter(study=study).first()
        if _binding is AUTO_BINDING
        else _binding
    )
    new_paths = {}
    if binding:
        for item in data["files"]:
            path = str(
                safe_path(
                    f"corpus/{target['key']}/{item.get('storage_name', item['name'])}"
                )
            )
            if len(path) > 500:
                raise ValueError("The repository path is too long")
            new_paths[item["name"]] = path
        existing = {doc["path"]: "" for doc in binding.document_paths.values()}
        if set(new_paths.values()) & (set(existing) | set(binding.managed_blobs)):
            raise Conflict("An upload path already exists in the repository export")
        corpus_paths({**existing, **{p: "" for p in new_paths.values()}})
    results = []
    with append_scope(study.pk):
        if not case:
            case = m.Case.objects.create(
                study=study,
                key=target["key"],
                name=target["name"],
                metadata=target.get("metadata", {}),
            )
        for item, details in zip(data["files"], metadata):
            details = {**item["metadata"], **details}
            if hashlib.sha256(item["text"].encode("utf-8")).hexdigest() != item["hash"]:
                raise ValueError("Uploaded text does not match its preview")
            version = put_document(
                study, uuid.uuid4().hex, item["name"], item["text"], details, [case]
            )
            results.append(
                dict(
                    name=item["name"],
                    version=version.pk,
                    hash=version.sha256,
                    bytes=item["bytes"],
                    metadata=details,
                )
            )
            if binding:
                path = new_paths[item["name"]]
                binding.initial_files[path] = version.text
                binding.document_paths[str(version.pk)] = dict(
                    id=str(uuid.uuid4()),
                    path=path,
                    case=case.key,
                    name=item["name"],
                    hash=version.sha256,
                    metadata=details,
                )
    if binding and _binding is AUTO_BINDING:
        binding.save(update_fields=["initial_files", "document_paths"])
        from .github_sync import record_event

        record_event(study.pk, "corpus-added", actor=user.pk)
    batch.data = dict(
        case=dict(id=case.pk, key=case.key, name=case.name), files=results
    )
    batch.committed_at = timezone.now()
    batch.save(update_fields=["data", "committed_at"])
    return batch


@transaction.atomic
def assign_upload(batch_id, study_id, user, session_ids):
    batch = get_object_or_404(
        m.CorpusUploadBatch,
        pk=batch_id,
        study_id=study_id,
        created_by=user,
        committed_at__isnull=False,
    )
    ids = set(str(uuid.UUID(value)) for value in session_ids)
    if not ids:
        raise ValueError("Select at least one coder")
    sessions = list(
        m.CodingSession.objects.select_for_update()
        .filter(study_id=study_id, deleted_at__isnull=True, pk__in=ids)
        .order_by("pk")
    )
    if len(sessions) != len(ids):
        raise PermissionDenied("A selected coder is not available in this study")
    case_id = batch.data["case"]["id"]
    assignments = list(
        m.Assignment.objects.select_for_update()
        .filter(session__in=sessions, case_id=case_id)
        .order_by("pk")
    )
    if any(a.locked for a in assignments):
        raise Conflict(
            "A selected coder has submitted this case. Reopen it explicitly before adding documents."
        )
    m.Study.objects.select_for_update().get(pk=study_id)
    study = get_study(user, study_id)
    case = get_object_or_404(m.Case, pk=case_id, study=study)
    versions = list(
        m.DocumentVersion.objects.filter(
            pk__in=[f["version"] for f in batch.data["files"]],
            document__study=study,
            document__cases=case,
        )
    )
    if len(versions) != len(batch.data["files"]):
        raise Conflict("Some imported documents are no longer available")
    changed = 0
    for session in sessions:
        assignment = next((a for a in assignments if a.session_id == session.pk), None)
        if not assignment:
            assignment = assign_case(session, case)
            # A newly assigned case includes its entire current corpus.
            changed += 1
        else:
            missing = [
                v for v in versions if not assignment.documents.filter(pk=v.pk).exists()
            ]
            if missing or assignment.removed_at:
                assignment.documents.add(*missing)
                if assignment.removed_at:
                    assignment.removed_at = None
                assignment.revision += 1
                assignment.save(update_fields=["removed_at", "revision"])
                changed += 1
    return changed


@transaction.atomic
def cancel_upload(batch_id, study_id, user):
    m.Study.objects.select_for_update().get(pk=study_id)
    get_study(user, study_id)
    batch = get_object_or_404(
        m.CorpusUploadBatch.objects.select_for_update(),
        pk=batch_id,
        study_id=study_id,
        created_by=user,
    )
    if batch.committed_at:
        raise Conflict("This upload has already been imported and cannot be cancelled.")
    batch.delete()
