from copy import deepcopy
import hashlib
import json
import secrets
import uuid
import yaml
from django.db import transaction
from .colors import HEX_COLOR
from .models import (
    Assignment,
    CodingSession,
    SharedCodebook,
    Submission,
    MergeAudit,
)


class Conflict(ValueError):
    pass


def validate_book(book):
    if (
        not isinstance(book, dict)
        or book.get("version") != 1
        or not isinstance(book.get("codes"), list)
    ):
        raise ValueError("Expected codebook version 1 and a codes list")
    ids, names = {}, set()
    for code in book["codes"]:
        if not isinstance(code, dict):
            raise ValueError("Invalid code")
        if not isinstance(code.get("id"), str):
            raise ValueError("Each concept needs a UUID")
        identity = str(uuid.UUID(code["id"]))
        if identity != code["id"] or identity in ids:
            raise ValueError("Duplicate or invalid code UUID")
        if (
            code.get("kind") not in ("code", "category")
            or not isinstance(code.get("name"), str)
            or not code["name"].strip()
        ):
            raise ValueError("Each concept needs a kind and name")
        if not isinstance(code.get("parent"), (str, type(None))):
            raise ValueError("Parent must be a UUID or null")
        if not isinstance(code.get("memo", ""), str) or not isinstance(
            code.get("references", []), list
        ):
            raise ValueError("Memo must be text and references must be a list")
        if not isinstance(code.get("rationale", ""), str) or not isinstance(
            code.get("archived", False), bool
        ):
            raise ValueError("Invalid rationale or archive flag")
        if "color" in code and (
            not isinstance(code["color"], str) or not HEX_COLOR.fullmatch(code["color"])
        ):
            raise ValueError("Code colors must use #RRGGBB format")
        name = (code.get("parent"), code["name"].strip().casefold())
        if name in names:
            raise ValueError("Duplicate names under the same parent")
        names.add(name)
        if any(
            not isinstance(reference, dict) for reference in code.get("references", [])
        ):
            raise ValueError("References must be structured mappings")
        ids[identity] = code
    for code in ids.values():
        seen = {code["id"]}
        parent = code.get("parent")
        while parent:
            if parent not in ids or parent in seen:
                raise ValueError("Missing parent or hierarchy cycle")
            seen.add(parent)
            parent = ids[parent].get("parent")
    json.dumps(book)
    return book


def load_book(text):
    return validate_book(yaml.safe_load(text))


def issue_link(session):
    token = secrets.token_urlsafe(32)
    session.token_hash = hashlib.sha256(token.encode()).hexdigest()
    session.save(update_fields=["token_hash"])
    return "/access/#" + token


@transaction.atomic
def create_session(coder, cases):
    book = SharedCodebook.objects.select_for_update().order_by("-pk").first()
    if not book:
        raise ValueError("Import a shared codebook first")
    if not isinstance(coder, str) or not coder.strip():
        raise ValueError("Provide a coder name")
    session = CodingSession.objects.create(
        coder=coder.strip(), base=book, codebook=deepcopy(book.content)
    )
    for case in cases:
        assign_case(session, case)
    return session, issue_link(session)


@transaction.atomic
def assign_case(session, case):
    assignment, created = Assignment.objects.get_or_create(session=session, case=case)
    if created:
        versions = []
        for document in case.document_set.all():
            latest = document.documentversion_set.order_by("-pk").first()
            if latest:
                versions.append(latest)
        assignment.documents.set(versions)
    return assignment


def span(text, start, end):
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError("Select a nonempty text passage")
    encoded = text.encode("utf-16-le")
    if end * 2 > len(encoded):
        raise ValueError("Passage extends beyond document")
    try:
        return encoded[start * 2 : end * 2].decode("utf-16-le")
    except UnicodeDecodeError as error:
        raise ValueError("Passage splits a Unicode character") from error


def serialize_annotations(assignment):
    return [
        dict(
            id=str(a.id),
            document=a.document_id,
            document_key=a.document.document.key,
            document_name=a.document.document.name,
            document_hash=a.document.sha256,
            start=a.start,
            end=a.end,
            selected_text=a.selected_text,
            codes=a.codes,
            memo=a.memo,
        )
        for a in assignment.annotation_set.select_related(
            "document__document"
        ).order_by("start", "id")
    ]


@transaction.atomic
def submit(assignment_id, session_id, expected):
    session = CodingSession.objects.select_for_update().get(pk=session_id)
    assignment = Assignment.objects.select_for_update().get(
        pk=assignment_id, session=session
    )
    check_revision(assignment, expected)
    if assignment.locked:
        raise Conflict("Case is already submitted")
    snapshot = dict(
        coder=session.coder,
        session=str(session.pk),
        case=assignment.case.name,
        case_key=assignment.case.key,
        case_metadata=assignment.case.metadata,
        base=session.base_id,
        codebook=deepcopy(session.codebook),
        annotations=serialize_annotations(assignment),
        documents=[
            dict(id=d.pk, key=d.document.key, hash=d.sha256, metadata=d.metadata)
            for d in assignment.documents.select_related("document")
        ],
    )
    submission = Submission.objects.create(assignment=assignment, snapshot=snapshot)
    assignment.locked = True
    assignment.revision += 1
    assignment.save()
    return submission


def check_revision(obj, expected):
    if type(expected) is not int or obj.revision != expected:
        raise Conflict("This workspace changed in another tab. Reload before saving.")


def differences(submission, shared):
    base = {c["id"]: c for c in submission.assignment.session.base.content["codes"]}
    incoming = {c["id"]: c for c in submission.snapshot["codebook"]["codes"]}
    current = {c["id"]: c for c in shared.content["codes"]}
    changes = []
    for identity in sorted(base.keys() | incoming.keys()):
        before, after, ours = (
            base.get(identity),
            incoming.get(identity),
            current.get(identity),
        )
        if before == after:
            continue
        operation = "add" if before is None else "delete" if after is None else "edit"
        changes.append(
            dict(
                id=identity,
                operation=operation,
                before=before,
                incoming=after,
                current=ours,
                conflict=ours != before and ours != after,
            )
        )
    return changes


@transaction.atomic
def merge_submission(submission_id, admin, revision, decisions):
    submission = Submission.objects.select_related("assignment__session__base").get(
        pk=submission_id
    )
    # Serialize all shared revisions using the singleton initial revision row.
    SharedCodebook.objects.select_for_update().order_by("pk").first()
    shared = SharedCodebook.objects.order_by("-pk").first()
    if MergeAudit.objects.filter(submission=submission).exists():
        raise Conflict("This submission has already been reviewed")
    if shared.pk != revision:
        raise Conflict("Shared codebook changed. Review the new comparison.")
    changes = differences(submission, shared)
    if set(decisions) != {c["id"] for c in changes}:
        raise ValueError("Provide a decision for every change")
    entities = {c["id"]: deepcopy(c) for c in shared.content["codes"]}
    mappings = {}
    accepted = set()
    referenced = {
        code
        for s in Submission.objects.all()
        for a in s.snapshot["annotations"]
        for code in a["codes"]
    }
    from .models import Annotation

    referenced.update(
        code
        for codes in Annotation.objects.values_list("codes", flat=True)
        for code in codes
    )
    for change in changes:
        decision = decisions[change["id"]]
        action = decision.get("action")
        identity = change["id"]
        if action == "reject":
            continue
        if action == "map":
            target = decision.get("target")
            if target not in entities or entities[target].get("archived"):
                raise ValueError("Mapping target must be an active shared concept")
            if (
                change["incoming"] is None
                or entities[target]["kind"] != change["incoming"]["kind"]
            ):
                raise ValueError("Mapping requires concepts of the same kind")
            mappings[identity] = target
        elif action == "accept":
            if change["incoming"] is None:
                if identity in referenced:
                    raise ValueError(
                        "Referenced concepts must be archived instead of deleted"
                    )
                entities.pop(identity, None)
            else:
                entities[identity] = deepcopy(change["incoming"])
                mappings[identity] = identity
                accepted.add(identity)
        else:
            raise ValueError("Choose accept, reject, or map")
    for identity in accepted:
        entity = entities[identity]
        if entity.get("parent") in mappings:
            entity["parent"] = mappings[entity["parent"]]
    for target in mappings.values():
        if target not in entities:
            raise ValueError("Mapping target was deleted")
    result = deepcopy(shared.content)
    result["codes"] = list(entities.values())
    validate_book(result)
    published = SharedCodebook.objects.create(content=result)
    return MergeAudit.objects.create(
        submission=submission,
        admin=admin,
        previous=shared,
        result=published,
        decisions=decisions,
        mappings=mappings,
    )
