import datetime
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from .permissions import admin_required
from .studies import get_study
from .models import CorpusUploadBatch, CodingSession, Assignment
from .services import Conflict
from .corpus_uploads import preview_upload, commit_upload, assign_upload, cancel_upload


@admin_required
@require_http_methods(["GET", "POST"])
def upload(request, study_id, batch_id=None):
    study = get_study(request.user, study_id)
    batch = (
        get_object_or_404(
            CorpusUploadBatch, pk=batch_id, study=study, created_by=request.user
        )
        if batch_id
        else None
    )
    error, status = "", 200
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            if action == "preview" and batch is None:
                batch = preview_upload(
                    study,
                    request.user,
                    request.FILES.getlist("files"),
                    request.POST.get("case", ""),
                    request.POST.get("new_case", ""),
                    request.POST.get("notes", ""),
                )
                return redirect(
                    "corpus-upload-batch", study_id=study.pk, batch_id=batch.pk
                )
            elif action == "confirm" and batch:
                metadata = [
                    dict(
                        url=request.POST.get(f"url_{i}", "").strip(),
                        notes=request.POST.get(f"notes_{i}", ""),
                    )
                    for i in range(len(batch.data["files"]))
                ]
                batch = commit_upload(batch.pk, study.pk, request.user, metadata)
                messages.success(
                    request,
                    "Files imported. Existing assignments have not been changed.",
                )
                return redirect(
                    "corpus-upload-batch", study_id=study.pk, batch_id=batch.pk
                )
            elif action == "cancel" and batch and not batch.committed_at:
                cancel_upload(batch.pk, study.pk, request.user)
                messages.success(
                    request, "Upload cancelled. No corpus files were added."
                )
                return redirect("study-corpus", study_id=study.pk)
            elif action == "assign" and batch and batch.committed_at:
                count = assign_upload(
                    batch.pk, study.pk, request.user, request.POST.getlist("sessions")
                )
                messages.success(
                    request, f"Imported texts added to {count} coder assignment(s)."
                )
                return redirect(
                    "corpus-upload-batch", study_id=study.pk, batch_id=batch.pk
                )
            else:
                raise ValueError("Unknown upload action")
        except Conflict as exc:
            error, status = str(exc), 409
        except (ValueError, ValidationError) as exc:
            error, status = (
                "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else str(exc)
            ), 400
    coders = []
    if batch and batch.committed_at:
        assignments = {
            a.session_id: a
            for a in Assignment.objects.filter(
                case_id=batch.data["case"]["id"], session__study=study
            ).prefetch_related("documents")
        }
        imported = {item["version"] for item in batch.data["files"]}
        for session in CodingSession.objects.filter(
            study=study, deleted_at__isnull=True
        ).order_by(Lower("coder"), "pk"):
            assignment = assignments.get(session.pk)
            coders.append(
                dict(
                    session=session,
                    submitted=bool(assignment and assignment.locked),
                    included=bool(
                        assignment
                        and not assignment.removed_at
                        and imported.issubset(
                            {v.pk for v in assignment.documents.all()}
                        )
                    ),
                )
            )
    return render(
        request,
        "corpus/corpus_upload.html",
        dict(
            study=study,
            batch=batch,
            error=error,
            coders=coders,
            cases=study.cases.order_by(Lower("name"), "pk"),
            recent=study.corpus_uploads.filter(created_by=request.user).order_by(
                "-created"
            )[:10],
            expired=bool(
                batch
                and not batch.committed_at
                and batch.created < timezone.now() - datetime.timedelta(hours=1)
            ),
        ),
        status=status,
    )
