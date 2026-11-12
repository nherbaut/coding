from copy import deepcopy
import hashlib
import json
import secrets
import uuid
import yaml
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from .colors import HEX_COLOR
from .models import (
    Assignment,
    InvitationEmail,
    CodingSession,
    SharedCodebook,
    Submission,
    SubmissionWithdrawal,
    SubmissionEmail,
    MergeAudit,
    Study,
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
    book = yaml.safe_load(text)
    if isinstance(book, dict):
        book.pop("export_context", None)
    return validate_book(book)


def cancel_invitation_emails(session):
    InvitationEmail.objects.filter(
        session=session, sent_at__isnull=True, cancelled_at__isnull=True
    ).update(cancelled_at=timezone.now(), body="", lease_token=None, locked_until=None)


def lock_active_session(session_id):
    session = CodingSession.objects.select_for_update().get(pk=session_id)
    if session.deleted_at:
        raise Conflict("This coder session has been deleted.")
    return session


@transaction.atomic
def delete_coder_session(session_id):
    session = CodingSession.objects.select_for_update().get(pk=session_id)
    if session.deleted_at:
        return session
    now = timezone.now()
    session.deleted_at = now
    session.token_hash = ""
    session.encrypted_token = ""
    session.revision += 1
    session.save(
        update_fields=["deleted_at", "token_hash", "encrypted_token", "revision"]
    )
    Assignment.objects.filter(session=session, removed_at__isnull=True).update(
        removed_at=now, revision=F("revision") + 1
    )
    cancel_invitation_emails(session)
    from .github_sync import record_event

    record_event(session.study_id, "session-archived")
    return session


@transaction.atomic
def remove_assignment(assignment_id, expected):
    session_id = Assignment.objects.values_list("session_id", flat=True).get(
        pk=assignment_id
    )
    session = lock_active_session(session_id)
    assignment = Assignment.objects.select_for_update().get(
        pk=assignment_id, session=session
    )
    if assignment.removed_at:
        return assignment
    check_revision(assignment, expected)
    assignment.removed_at = timezone.now()
    assignment.revision += 1
    assignment.save(update_fields=["removed_at", "revision"])
    # Queued invitation bodies may still list this case. Do not email a stale list.
    cancel_invitation_emails(session)
    from .github_sync import record_event

    record_event(session.study_id, "assignment-archived")
    return assignment


@transaction.atomic
def issue_link(session):
    active = lock_active_session(session.pk)
    if active.admin_user_id:
        raise ValueError("Admin coding uses SSO, not private links")
    token = secrets.token_urlsafe(32)
    active.token_hash = hashlib.sha256(token.encode()).hexdigest()
    from .private_links import link_cipher

    active.encrypted_token = link_cipher().encrypt(token.encode()).decode()
    active.save(update_fields=["token_hash", "encrypted_token"])
    session.encrypted_token = active.encrypted_token
    session.token_hash = active.token_hash
    cancel_invitation_emails(active)
    return "/access/#" + token


@transaction.atomic
def create_session(study, coder, cases, email=""):
    study = Study.objects.select_for_update().get(pk=study.pk)
    book = study.codebook
    if not book:
        raise ValueError("Import a shared codebook first")
    if not isinstance(coder, str) or not coder.strip():
        raise ValueError("Provide a coder name")
    email = CodingSession._meta.get_field("email").clean(email.strip(), None)
    session = CodingSession.objects.create(
        study=study,
        instructions=study.instructions,
        coder=coder.strip(),
        email=email,
        base=book,
        codebook=deepcopy(book.content),
    )
    for case in cases:
        assign_case(session, case)
    return session, issue_link(session)


@transaction.atomic
def assign_case(session, case):
    session = lock_active_session(session.pk)
    if case.study_id != session.study_id:
        raise ValueError("Case and coder must belong to the same study")
    assignment, created = Assignment.objects.get_or_create(session=session, case=case)
    if created:
        versions = []
        for document in case.document_set.all():
            latest = document.documentversion_set.order_by("-pk").first()
            if latest:
                versions.append(latest)
        assignment.documents.set(versions)
    if assignment.removed_at:
        assignment.removed_at = None
        assignment.revision += 1
        assignment.save(update_fields=["removed_at", "revision"])
    return assignment


@transaction.atomic
def duplicate_session(source_id, coder, email):
    """Copy a request's active assignments, not its personal coding or link."""
    from django.core.validators import validate_email

    source = lock_active_session(source_id)
    if source.admin_user_id:
        raise ValueError("Only private coder requests can be duplicated")
    validate_email(email)
    originals = list(
        source.assignment_set.filter(removed_at__isnull=True)
        .select_related("case")
        .prefetch_related("documents")
    )
    session, link = create_session(
        source.study, coder, [a.case for a in originals], email=email
    )
    for original in originals:
        session.assignment_set.get(case=original.case).documents.set(
            original.documents.all()
        )
    return session, link


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
    session = lock_active_session(session_id)
    assignment = Assignment.objects.select_for_update().get(
        pk=assignment_id, session=session
    )
    if assignment.removed_at:
        raise Conflict("This case assignment has been removed.")
    check_revision(assignment, expected)
    if assignment.locked:
        raise Conflict("Case is already submitted")
    snapshot = dict(
        study=dict(id=str(session.study_id), title=session.study.title),
        instructions=dict(
            revision=session.instructions_id,
            content=deepcopy(session.instructions.content),
        ),
        coder=session.coder,
        coder_kind="admin" if session.admin_user_id else "private",
        admin_username=session.admin_user.username if session.admin_user_id else None,
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
    from .notifications import queue_submission_email

    queue_submission_email(submission)
    from .github_sync import record_event

    record_event(session.study_id, "submitted")
    return submission


@transaction.atomic
def withdraw_submission(
    submission_id, initiated_by="admin", expected=None, coder_session=None
):
    submission = Submission.objects.select_related("assignment").get(pk=submission_id)
    if coder_session is not None and submission.assignment.session_id != coder_session:
        raise Conflict("This submission belongs to another coder session.")
    session = lock_active_session(submission.assignment.session_id)
    assignment = Assignment.objects.select_for_update().get(pk=submission.assignment_id)
    if assignment.removed_at:
        raise Conflict("This case assignment has been removed.")
    if expected is not None:
        check_revision(assignment, expected)
    latest = assignment.submission_set.order_by("-pk").first()
    _, created = SubmissionWithdrawal.objects.get_or_create(
        submission=submission, defaults={"initiated_by": initiated_by}
    )
    if created:
        from .github_sync import record_event

        record_event(session.study_id, "submission-withdrawn")
        # Already-sent notifications remain part of the delivery history.
        SubmissionEmail.objects.filter(
            submission=submission, sent_at__isnull=True
        ).delete()
        if latest and latest.pk == submission.pk and assignment.locked:
            assignment.locked = False
            assignment.revision += 1
            assignment.save(update_fields=["locked", "revision"])
    return assignment


@transaction.atomic
def reopen_assignment(
    assignment_id, session_id=None, expected=None, initiated_by="admin"
):
    owner = Assignment.objects.values_list("session_id", flat=True).get(
        pk=assignment_id
    )
    if session_id is not None and owner != session_id:
        raise Conflict("This case belongs to another coder session.")
    lock_active_session(owner)
    assignment = Assignment.objects.select_for_update().get(pk=assignment_id)
    if assignment.removed_at:
        raise Conflict("This case assignment has been removed.")
    if expected is not None:
        check_revision(assignment, expected)
    if not assignment.locked:
        raise Conflict("This case is already open for editing.")
    latest = assignment.submission_set.order_by("-pk").first()
    if latest:
        return withdraw_submission(latest.pk, initiated_by, expected, session_id)
    assignment.locked = False
    assignment.revision += 1
    assignment.save(update_fields=["locked", "revision"])
    return assignment


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
    # Serialize withdrawal and review in the same session/assignment lock order.
    CodingSession.objects.select_for_update().get(pk=submission.assignment.session_id)
    Assignment.objects.select_for_update().get(pk=submission.assignment_id)
    if SubmissionWithdrawal.objects.filter(submission=submission).exists():
        raise Conflict("This submission was withdrawn and cannot be merged.")
    # Serialize all shared revisions using the singleton initial revision row.
    study = Study.objects.select_for_update().get(
        pk=submission.assignment.session.study_id
    )
    shared = study.codebook
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
        for s in Submission.objects.filter(assignment__session__study=study)
        for a in s.snapshot["annotations"]
        for code in a["codes"]
    }
    from .models import Annotation

    referenced.update(
        code
        for codes in Annotation.objects.filter(
            assignment__session__study=study
        ).values_list("codes", flat=True)
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
    from .studies import publish_codebook

    published = publish_codebook(
        study,
        result,
        expected_revision=revision,
        actor=admin.pk,
        origin="proposal-review",
    )
    return MergeAudit.objects.create(
        submission=submission,
        admin=admin,
        previous=shared,
        result=published,
        decisions=decisions,
        mappings=mappings,
    )
