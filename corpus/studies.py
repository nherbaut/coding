"""Study access, ownership and explicit publication of study revisions."""

import re
from copy import deepcopy
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from .models import (
    Study,
    StudyAdmin,
    StudyAdminInvitation,
    StudyAdminEmail,
    StudyProtocol,
    SharedCodebook,
    CodingSession,
)


def ldap_username(user):
    return user.username.removeprefix("authelia:").casefold()


def accessible_studies(user):
    return Study.objects.filter(
        Q(owner=user) | Q(admins__user=user, admins__revoked_at__isnull=True)
    ).distinct()


def get_study(user, study_id, owner=False):
    study = get_object_or_404(
        accessible_studies(user).select_related("owner", "codebook", "instructions"),
        pk=study_id,
    )
    if owner and study.owner_id != user.pk:
        raise PermissionDenied("Only the study owner can manage study admin access.")
    return study


@transaction.atomic
def create_study(owner, title, description=""):
    title = title.strip()
    if not title or len(title) > 200:
        raise ValueError("Provide a study title of at most 200 characters")
    study = Study.objects.create(owner=owner, title=title, description=description)
    study.codebook = SharedCodebook.objects.create(
        study=study, content={"version": 1, "codes": []}
    )
    study.instructions = StudyProtocol.objects.create(
        study=study,
        created_by=owner,
        content={"version": 1, "research_questions": [], "guidelines": []},
    )
    study.save(update_fields=["codebook", "instructions"])
    return study


@transaction.atomic
def publish_codebook(study, book, *, expected_revision):
    from .services import validate_book

    study = Study.objects.select_for_update().get(pk=study.pk)
    from .services import Conflict

    if study.codebook_id != expected_revision:
        raise Conflict(
            "Shared codebook changed. Reload and reconcile your draft before publishing."
        )
    validate_book(book)
    from .models import DocumentVersion
    from .services import span

    for code in book["codes"]:
        for reference in code.get("references", []):
            if reference.get("type") == "corpus":
                document = DocumentVersion.objects.filter(
                    document__study=study, pk=reference.get("document")
                ).first()
                if (
                    not document
                    or document.sha256 != reference.get("hash")
                    or span(document.text, reference["start"], reference["end"])
                    != reference["quote"]
                ):
                    raise ValueError(
                        "Codebook corpus evidence must match a document version in this study"
                    )
    previous = {c["id"]: c for c in study.codebook.content["codes"]}
    proposed = {c["id"]: c for c in book["codes"]}
    referenced = referenced_concepts(study)
    if (previous.keys() - proposed.keys()) & referenced:
        raise ValueError("Archive referenced concepts instead of deleting them")
    if any(
        previous[i]["kind"] != proposed[i]["kind"]
        for i in previous.keys() & proposed.keys() & referenced
    ):
        raise ValueError("Referenced concept types cannot change")
    revision = SharedCodebook.objects.create(study=study, content=deepcopy(book))
    study.codebook = revision
    study.save(update_fields=["codebook"])
    return revision


@transaction.atomic
def publish_instructions(study, admin):
    study = Study.objects.select_for_update().get(pk=study.pk)
    content = dict(
        version=1,
        research_questions=[
            dict(id=str(q.pk), label=q.label, text=q.text, position=q.position)
            for q in study.research_questions.all()
        ],
        guidelines=[
            dict(
                id=str(g.pk),
                title=g.title,
                text=g.text,
                position=g.position,
                questions=[str(q.pk) for q in g.questions.all()],
            )
            for g in study.guidelines.prefetch_related("questions")
        ],
    )
    revision = StudyProtocol.objects.create(
        study=study, created_by=admin, content=content
    )
    study.instructions = revision
    study.save(update_fields=["instructions"])
    return revision


@transaction.atomic
def invite_admin(study, owner, username, email=""):
    study = Study.objects.select_for_update().get(pk=study.pk)
    if study.owner_id != owner.pk:
        raise PermissionDenied("Only the study owner can invite admins")
    username = username.strip().casefold()
    if email:
        from django.core.validators import validate_email

        validate_email(email)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@-]{0,139}", username):
        raise ValueError("Enter the LDAP username without an authelia: prefix")
    if username == ldap_username(owner):
        raise ValueError("You already own this study")
    if study.admins.filter(
        user__username__in=[username, "authelia:" + username], revoked_at__isnull=True
    ).exists():
        raise ValueError("This user is already a study admin")
    invitation, created = StudyAdminInvitation.objects.get_or_create(
        study=study,
        username=username,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        defaults={"email": email, "invited_by": owner},
    )
    if not created:
        raise ValueError("An invitation is already pending for this user")
    if email:
        from .notifications import queue_admin_invitation

        queue_admin_invitation(invitation)
    return invitation


@transaction.atomic
def accept_invitation(invitation_id, user):
    identity = get_object_or_404(
        StudyAdminInvitation, pk=invitation_id, username=ldap_username(user)
    ).study_id
    Study.objects.select_for_update().get(pk=identity)
    invitation = get_object_or_404(
        StudyAdminInvitation.objects.select_for_update(),
        pk=invitation_id,
        username=ldap_username(user),
        revoked_at__isnull=True,
        accepted_at__isnull=True,
    )
    StudyAdmin.objects.update_or_create(
        study=invitation.study,
        user=user,
        defaults={"granted_by": invitation.invited_by, "revoked_at": None},
    )
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["accepted_at"])
    StudyAdminEmail.objects.filter(invitation=invitation, sent_at__isnull=True).update(
        cancelled_at=timezone.now(), body="", lease_token=None, locked_until=None
    )
    return invitation.study


@transaction.atomic
def update_session_instructions(study, session_id):
    session = get_object_or_404(
        CodingSession.objects.select_for_update(),
        study=study,
        pk=session_id,
        deleted_at__isnull=True,
    )
    if session.instructions_id != study.instructions_id:
        session.instructions = study.instructions
        session.revision += 1
        session.save(update_fields=["instructions", "revision"])
    return session


def referenced_concepts(study):
    from .models import Annotation, Submission, MergeAudit

    referenced = {
        identity
        for codes in Annotation.objects.filter(
            assignment__session__study=study
        ).values_list("codes", flat=True)
        for identity in codes
    }
    for snapshot in Submission.objects.filter(
        assignment__session__study=study
    ).values_list("snapshot", flat=True):
        referenced.update(
            identity for a in snapshot["annotations"] for identity in a["codes"]
        )
    for audit in MergeAudit.objects.filter(result__study=study).select_related(
        "submission"
    ):
        used = {
            identity
            for a in audit.submission.snapshot["annotations"]
            for identity in a["codes"]
        }
        referenced.update(
            target for source, target in audit.mappings.items() if source in used
        )
    return referenced
