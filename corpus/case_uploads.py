"""Atomic new-case batches, from ZIP folders or a version 1 corpus manifest."""

from collections import OrderedDict
import datetime
import hashlib
from pathlib import PurePosixPath
import uuid
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from .models import CorpusUploadBatch, Study, GitHubStudy, GitHubEvent
from .studies import get_study
from .services import Conflict
from .upload_files import zip_members, valid_name
from .corpus_uploads import check_names, commit_upload
from .github_repository import load_yaml, safe_path

MAX_BYTES = 100 * 1024 * 1024
MAX_DOCUMENTS = 2000
MAX_CASES = 500


def document(raw, name, source, metadata, identity=None):
    if not isinstance(name, str) or not name.strip() or len(name) > 500:
        raise ValueError("Invalid document name in case archive")
    safe_path(name)
    valid_name(PurePosixPath(name).name)
    if any(ord(c) < 32 for c in name):
        raise ValueError("Document names must not contain control characters")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{name} is not UTF-8 text") from None
    if "\x00" in text or not text.strip("\ufeff \r\n\t"):
        raise ValueError(f"{name} is empty or contains binary data")
    if not isinstance(metadata, dict):
        raise ValueError("Document metadata must be an object")
    details = {**metadata, "source_archive": source}
    if identity is not None:
        details["import_document_id"] = identity
    # Stable filenames avoid flattening collisions while retaining the exact title.
    storage = (
        hashlib.sha256((identity or source["member"]).encode()).hexdigest()
        + PurePosixPath(name).suffix.lower()
    )
    return dict(
        name=name,
        storage_name=storage,
        text=text,
        bytes=len(raw),
        hash=hashlib.sha256(raw).hexdigest(),
        metadata=details,
    )


def parse_case_zip(upload, notes=""):
    if PurePosixPath(upload.name).suffix.lower() != ".zip":
        raise ValueError("Choose one ZIP archive to import multiple cases")
    raw = upload.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Case archives must not exceed 100 MB")
    entries = {}
    for _, content, source in zip_members(
        raw,
        upload.name,
        max_files=MAX_DOCUMENTS + 1,
        max_entries=5000,
        max_bytes=MAX_BYTES,
        allow_manifest=True,
    ):
        path = source["member"]
        if path in entries:
            raise ValueError(f"Duplicate ZIP member: {path}")
        entries[path] = (content, source)
    groups = OrderedDict()
    if "manifest.yaml" in entries:
        try:
            manifest = load_yaml(entries["manifest.yaml"][0].decode("utf-8"))
        except UnicodeError:
            raise ValueError("manifest.yaml must be UTF-8") from None
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            raise ValueError("Expected corpus manifest version 1")
        if not isinstance(manifest.get("cases"), list) or not isinstance(
            manifest.get("documents"), list
        ):
            raise ValueError("The manifest must contain cases and documents lists")
        for item in manifest["cases"]:
            if not isinstance(item, dict):
                raise ValueError("Invalid manifest case")
            identity = item.get("id")
            name = item.get("name")
            if (
                not isinstance(identity, str)
                or not identity
                or identity in groups
                or not isinstance(name, str)
                or not name.strip()
                or len(name) > 500
                or not isinstance(item.get("metadata", {}), dict)
            ):
                raise ValueError("Invalid or duplicate manifest case")
            groups[identity] = dict(
                name=name,
                metadata={**item.get("metadata", {}), "import_case_id": identity},
                files=[],
            )
        used_paths = set()
        used_ids = set()
        for item in manifest["documents"]:
            if not isinstance(item, dict):
                raise ValueError("Invalid manifest document")
            identity = item.get("id")
            path = item.get("path")
            case_ids = item.get("cases")
            if (
                not isinstance(identity, str)
                or not identity
                or identity in used_ids
                or not isinstance(path, str)
                or path == "manifest.yaml"
                or path not in entries
                or path in used_paths
            ):
                raise ValueError(
                    "Invalid or duplicated document identity/path in manifest"
                )
            if (
                not isinstance(case_ids, list)
                or len(case_ids) != 1
                or any(not isinstance(c, str) or c not in groups for c in case_ids)
            ):
                raise ValueError(
                    "Each manifest document must reference exactly one known case"
                )
            used_ids.add(identity)
            used_paths.add(path)
            content, source = entries[path]
            details = item.get("metadata", {})
            if not isinstance(details, dict):
                raise ValueError("Document metadata must be an object")
            result = document(
                content,
                item.get("name", PurePosixPath(path).name),
                source,
                {"notes": notes, "url": "", **details},
                identity,
            )
            expected = item.get("hash", details.get("text_sha256", result["hash"]))
            if expected != result["hash"]:
                raise ValueError(f"Document hash differs from its manifest: {path}")
            for case_id in case_ids:
                groups[case_id]["files"].append(result)
        if set(entries) - {"manifest.yaml"} != used_paths:
            raise ValueError("The ZIP contains documents not listed in manifest.yaml")
    else:
        for path, (content, source) in entries.items():
            parts = PurePosixPath(path).parts
            if parts[0] == "corpus":
                parts = parts[1:]
            if len(parts) < 2:
                raise ValueError(
                    "For multiple cases, place documents inside one folder per case (or provide manifest.yaml)"
                )
            case_name = parts[0]
            group = groups.setdefault(
                case_name, dict(name=case_name, metadata={}, files=[])
            )
            # Retain relative subfolders as document titles rather than flattening them.
            group["files"].append(
                document(
                    content,
                    str(PurePosixPath(*parts[1:])),
                    source,
                    dict(notes=notes, url=""),
                )
            )
    if (
        not groups
        or len(groups) > MAX_CASES
        or sum(len(g["files"]) for g in groups.values()) > MAX_DOCUMENTS
    ):
        raise ValueError(
            "Import between 1 and 500 cases and at most 2000 case documents"
        )
    return list(groups.values())


def preview_cases(study, user, uploads, notes=""):
    get_study(user, study.pk)
    if len(uploads) != 1:
        raise ValueError("Choose exactly one ZIP archive for a batch of cases")
    groups = parse_case_zip(uploads[0], notes)
    names = set()
    existing = {name.casefold() for name in study.cases.values_list("name", flat=True)}
    files = []
    cases = []
    for group in sorted(groups, key=lambda g: g["name"].casefold()):
        if (
            not group["name"].strip()
            or group["name"] != group["name"].strip()
            or len(group["name"]) > 500
            or any(ord(c) < 32 for c in group["name"])
        ):
            raise ValueError("Invalid case name in archive")
        imported_identity = group["metadata"].get("import_case_id")
        if (
            imported_identity
            and study.cases.filter(metadata__import_case_id=imported_identity).exists()
        ):
            raise Conflict(
                "A case with this imported identity already exists. No cases were replaced."
            )
        folded = group["name"].casefold()
        if folded in names or folded in existing:
            raise Conflict(
                f'A case named {group["name"]} already exists in this study or batch. No existing case will be replaced.'
            )
        names.add(folded)
        target = dict(
            id=None,
            key=uuid.uuid4().hex,
            name=group["name"],
            metadata=group["metadata"],
        )
        check_names(
            study,
            None,
            [f["name"] for f in group["files"]],
            [f["storage_name"] for f in group["files"]],
        )
        indices = list(range(len(files), len(files) + len(group["files"])))
        files.extend(group["files"])
        cases.append(dict(case=target, indices=indices))
    from .management.commands.prune_corpus_uploads import prune

    prune()
    if (
        study.corpus_uploads.filter(created_by=user, committed_at__isnull=True).count()
        >= 5
    ):
        raise ValueError(
            "Confirm or cancel an existing preview before creating another"
        )
    return CorpusUploadBatch.objects.create(
        study=study,
        created_by=user,
        data=dict(
            mode="cases",
            case=dict(id=None, name=f"{len(cases)} cases"),
            files=files,
            groups=cases,
        ),
    )


@transaction.atomic
def commit_cases(batch_id, study_id, user):
    Study.objects.select_for_update().get(pk=study_id)
    get_study(user, study_id)
    batch = get_object_or_404(
        CorpusUploadBatch.objects.select_for_update(),
        pk=batch_id,
        study_id=study_id,
        created_by=user,
    )
    if batch.committed_at:
        return batch
    if batch.data.get(
        "mode"
    ) != "cases" or batch.created < timezone.now() - datetime.timedelta(hours=1):
        raise ValueError(
            "This case preview is invalid or expired. Upload the archive again."
        )
    binding = GitHubStudy.objects.select_for_update().filter(study_id=study_id).first()
    receipts = []
    results = []
    for group in batch.data["groups"]:
        children = [batch.data["files"][i] for i in group["indices"]]
        child = CorpusUploadBatch.objects.create(
            study_id=study_id,
            created_by=user,
            data=dict(case=group["case"], files=children),
        )
        metadata = [
            dict(url=f["metadata"].get("url", ""), notes=f["metadata"].get("notes", ""))
            for f in children
        ]
        child = commit_upload(child.pk, study_id, user, metadata, _binding=binding)
        child.data["parent_batch"] = str(batch.pk)
        child.save(update_fields=["data"])
        receipts.append(
            dict(case=child.data["case"], batch=str(child.pk), count=len(children))
        )
        results.extend(child.data["files"])
    if binding:
        binding.save(update_fields=["initial_files", "document_paths"])
        GitHubEvent.objects.create(
            binding=binding, kind="corpus-added", actor=str(user.pk)
        )
    batch.data = dict(
        mode="cases", case=batch.data["case"], files=results, receipts=receipts
    )
    batch.committed_at = timezone.now()
    batch.save(update_fields=["data", "committed_at"])
    return batch


@transaction.atomic
def assign_cases(batch_id, study_id, user, session_ids):
    from .models import CodingSession
    from .corpus_uploads import assign_upload

    batch = get_object_or_404(
        CorpusUploadBatch,
        pk=batch_id,
        study_id=study_id,
        created_by=user,
        committed_at__isnull=False,
    )
    get_study(user, study_id)
    ids = {str(uuid.UUID(value)) for value in session_ids}
    if not ids or batch.data.get("mode") != "cases":
        raise ValueError("Select coders for an imported case batch")
    sessions = list(
        CodingSession.objects.select_for_update()
        .filter(study_id=study_id, pk__in=ids, deleted_at__isnull=True)
        .order_by("pk")
    )
    if len(sessions) != len(ids):
        raise ValueError("A selected coder is unavailable")
    return sum(
        assign_upload(item["batch"], study_id, user, list(ids))
        for item in batch.data["receipts"]
    )
