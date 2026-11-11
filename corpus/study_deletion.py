"""Explicit, atomic owner deletion. No remote repository operations."""

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction, OperationalError
from django.utils import timezone
from django.shortcuts import get_object_or_404
from . import models as m
from .services import Conflict


def locked(query):
    return query.select_for_update(
        nowait=connection.features.has_select_for_update_nowait
    )


def delete_study(study_id, owner, confirmation):
    try:
        with transaction.atomic():
            study = get_object_or_404(locked(m.Study.objects), pk=study_id)
            if not owner.is_active or not owner.is_staff or study.owner_id != owner.pk:
                raise PermissionDenied("Only the study owner can delete this study")
            if confirmation != study.title:
                raise ValueError(
                    "Type the exact current study title to confirm deletion"
                )
            # Fail immediately rather than invert the session -> study lock order
            # used by submission/review operations and wait in a deadlock.
            list(locked(m.CodingSession.objects.filter(study=study)).order_by("pk"))
            list(
                locked(m.Assignment.objects.filter(session__study=study)).order_by("pk")
            )
            binding = locked(m.GitHubStudy.objects.filter(study=study)).first()
            now = timezone.now()
            if (
                binding
                and binding.lease_token
                and binding.locked_until
                and binding.locked_until > now
            ):
                raise Conflict(
                    "A GitHub synchronization is running. Pause publication, wait for this run to finish, then retry deletion."
                )
            jobs = [
                m.SubmissionEmail.objects.filter(
                    submission__assignment__session__study=study
                ),
                m.InvitationEmail.objects.filter(session__study=study),
                m.StudyAdminEmail.objects.filter(invitation__study=study),
            ]
            for query in jobs:
                for job in locked(query).order_by("pk"):
                    if (
                        not job.sent_at
                        and job.lease_token
                        and job.locked_until
                        and job.locked_until > now
                    ):
                        raise Conflict(
                            "An email delivery is running. Wait for it to finish, then retry deletion."
                        )
            # Deleting the binding cancels durable work and releases repo/branch
            # ownership. Its leases were checked above; nothing is sent to GitHub.
            m.GitHubStudy.objects.filter(study=study).delete()
            for query in jobs:
                query.delete()
            m.MergeAudit.objects.filter(
                submission__assignment__session__study=study
            ).delete()
            m.Submission.objects.filter(assignment__session__study=study).delete()
            m.Annotation.objects.filter(assignment__session__study=study).delete()
            m.Assignment.objects.filter(session__study=study).delete()
            m.CodingSession.objects.filter(study=study).delete()
            m.CodingReport.objects.filter(study=study).delete()
            m.DocumentVersion.objects.filter(document__study=study).delete()
            m.Document.objects.filter(study=study).delete()
            m.Case.objects.filter(study=study).delete()
            # Break the nullable current-revision pointers before deleting the
            # immutable revision records. The whole operation is one transaction.
            m.Study.objects.filter(pk=study.pk).update(codebook=None, instructions=None)
            m.SharedCodebook.objects.filter(study=study).delete()
            m.StudyProtocol.objects.filter(study=study).delete()
            study.delete()
    except OperationalError as error:
        cause = error.__cause__
        code = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
        if code in ("55P03", "40P01"):
            raise Conflict(
                "This study is being updated. Wait for the operation to finish, then retry deletion."
            ) from None
        raise
