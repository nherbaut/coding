import uuid
from django.contrib import messages
from .permissions import admin_required
from django.db import transaction
from django.db.models.functions import Lower
from django.http import Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.core.validators import validate_email
from .models import (
    Study,
    StudyAdmin,
    StudyAdminInvitation,
    StudyAdminEmail,
    ResearchQuestion,
    CodingGuideline,
    Case,
)
from .studies import (
    accessible_studies,
    ldap_username,
    get_study,
    create_study,
    invite_admin,
    accept_invitation,
    publish_instructions,
)
from .views import api_errors
from .importers import put_document


@admin_required
@api_errors
def study_list(request):
    if request.method == "POST":
        if request.POST.get("action") == "accept_invitation":
            study = accept_invitation(request.POST["invitation"], request.user)
        else:
            study = create_study(
                request.user,
                request.POST.get("title", ""),
                request.POST.get("description", ""),
            )
        return redirect("dashboard", study_id=study.pk)
    return render(
        request,
        "corpus/studies.html",
        dict(
            studies=accessible_studies(request.user)
            .select_related("owner")
            .order_by(Lower("title"), "pk"),
            invitations=StudyAdminInvitation.objects.filter(
                username=ldap_username(request.user),
                accepted_at__isnull=True,
                revoked_at__isnull=True,
            ).select_related("study__owner"),
        ),
    )


@admin_required
@api_errors
def study_settings(request, study_id):
    study = get_study(request.user, study_id)
    notice = ""
    if request.method == "POST":
        action = request.POST["action"]
        if action == "save":
            title = request.POST.get("title", "").strip()
            if not title or len(title) > 200:
                raise ValueError("Provide a study title of at most 200 characters")
            emails = [
                e.strip()
                for e in request.POST.get("notification_emails", "")
                .replace(";", ",")
                .replace("\n", ",")
                .split(",")
                if e.strip()
            ]
            for email in emails:
                validate_email(email)
            study.title = title
            study.description = request.POST.get("description", "")
            study.notification_emails = list(dict.fromkeys(emails))
            study.save(update_fields=["title", "description", "notification_emails"])
            notice = "Study settings saved"
        else:
            study = get_study(request.user, study_id, owner=True)
            if action == "invite_admin":
                email = request.POST.get("email", "").strip()
                if email:
                    validate_email(email)
                invite_admin(study, request.user, request.POST["username"], email)
                notice = (
                    "Admin invited. They can accept after signing in through Authelia."
                )
            elif action == "revoke_admin":
                with transaction.atomic():
                    Study.objects.select_for_update().get(pk=study.pk)
                    membership = get_object_or_404(
                        StudyAdmin.objects.select_for_update(),
                        study=study,
                        pk=request.POST["membership"],
                        revoked_at__isnull=True,
                    )
                    membership.revoked_at = timezone.now()
                    membership.save(update_fields=["revoked_at"])
                notice = "Study admin access revoked"
            elif action == "cancel_invitation":
                with transaction.atomic():
                    Study.objects.select_for_update().get(pk=study.pk)
                    invitation = get_object_or_404(
                        StudyAdminInvitation.objects.select_for_update(),
                        study=study,
                        pk=request.POST["invitation"],
                        accepted_at__isnull=True,
                        revoked_at__isnull=True,
                    )
                    invitation.revoked_at = timezone.now()
                    invitation.save(update_fields=["revoked_at"])
                    StudyAdminEmail.objects.filter(
                        invitation=invitation, sent_at__isnull=True
                    ).update(
                        cancelled_at=timezone.now(),
                        body="",
                        lease_token=None,
                        locked_until=None,
                    )
                notice = "Invitation canceled"
            else:
                raise ValueError("Unknown settings action")
    if request.method == "POST":
        messages.success(request, notice)
        return redirect("study-settings", study_id=study.pk)
    return render(
        request,
        "corpus/study_settings.html",
        dict(
            study=study,
            notice=notice,
            is_owner=study.owner_id == request.user.pk,
            members=study.admins.filter(revoked_at__isnull=True).select_related("user"),
            invitations=study.admin_invitations.filter(
                accepted_at__isnull=True, revoked_at__isnull=True
            ),
        ),
    )


@admin_required
@api_errors
def study_protocol(request, study_id):
    study = get_study(request.user, study_id)
    notice = ""
    if request.method == "POST":
        with transaction.atomic():
            Study.objects.select_for_update().get(pk=study.pk)
            action = request.POST["action"]
            if action in ("save_question", "delete_question"):
                item = (
                    get_object_or_404(
                        ResearchQuestion, study=study, pk=request.POST["item"]
                    )
                    if request.POST.get("item")
                    else ResearchQuestion(study=study)
                )
                if action == "delete_question":
                    item.delete()
                else:
                    item.label = request.POST.get("label", "").strip()
                    item.text = request.POST.get("text", "").strip()
                    item.position = int(request.POST.get("position", "0"))
                    if not item.label or not item.text:
                        raise ValueError("Provide a question label and question text")
                    item.full_clean()
                    item.save()
            elif action in ("save_guideline", "delete_guideline"):
                item = (
                    get_object_or_404(
                        CodingGuideline, study=study, pk=request.POST["item"]
                    )
                    if request.POST.get("item")
                    else CodingGuideline(study=study)
                )
                if action == "delete_guideline":
                    item.delete()
                else:
                    item.title = request.POST.get("title", "").strip()
                    item.text = request.POST.get("text", "").strip()
                    item.position = int(request.POST.get("position", "0"))
                    if not item.title or not item.text:
                        raise ValueError("Provide a guideline title and text")
                    questions = list(
                        study.research_questions.filter(
                            pk__in=request.POST.getlist("questions")
                        )
                    )
                    if len(questions) != len(set(request.POST.getlist("questions"))):
                        raise Http404("Question not in study")
                    item.full_clean()
                    item.save()
                    item.questions.set(questions)
            else:
                raise ValueError("Unknown protocol action")
            publish_instructions(study, request.user)
        study.refresh_from_db()
        notice = "Study instructions published. New coder sessions use this revision."
    if request.method == "POST":
        messages.success(request, notice)
        return redirect("study-protocol", study_id=study.pk)
    return render(
        request,
        "corpus/protocol.html",
        dict(
            study=study,
            notice=notice,
            questions=study.research_questions.all(),
            guidelines=study.guidelines.prefetch_related("questions"),
        ),
    )


@admin_required
@api_errors
def study_corpus(request, study_id):
    study = get_study(request.user, study_id)
    if request.method == "POST":
        from .github_sync import require_mutable_corpus

        require_mutable_corpus(study.pk)
    notice = ""
    if request.method == "POST":
        action = request.POST["action"]
        if action == "case":
            name = request.POST.get("name", "").strip()
            if not name:
                raise ValueError("Provide a case name")
            Case.objects.create(
                study=study,
                key=uuid.uuid4().hex,
                name=name,
                metadata={"notes": request.POST.get("notes", "")},
            )
            notice = "Case created"
        elif action == "text":
            case = get_object_or_404(Case, study=study, pk=request.POST["case"])
            upload = request.FILES.get("file")
            text = (
                upload.read().decode("utf-8-sig")
                if upload
                else request.POST.get("text", "")
            )
            name = request.POST.get("name", "").strip() or (
                upload.name if upload else ""
            )
            if not name or not text.strip():
                raise ValueError("Provide a document title and text or a UTF-8 file")
            put_document(
                study,
                uuid.uuid4().hex,
                name,
                text,
                {
                    "url": request.POST.get("url", "").strip(),
                    "notes": request.POST.get("notes", ""),
                },
                [case],
            )
            notice = "Text added to case"
        else:
            raise ValueError("Unknown corpus action")
    if request.method == "POST":
        messages.success(request, notice)
        return redirect("study-corpus", study_id=study.pk)
    return render(
        request,
        "corpus/corpus.html",
        dict(
            study=study,
            notice=notice,
            cases=study.cases.prefetch_related("document_set").order_by(
                Lower("name"), "pk"
            ),
        ),
    )
