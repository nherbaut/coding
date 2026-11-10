from copy import deepcopy
import csv
import hashlib
import io
import json
import secrets
import yaml
import zipfile
from django.core.exceptions import ValidationError
from functools import wraps
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Prefetch, Q
from django.db.models.functions import Lower
from django.core.validators import validate_email
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from .models import (
    Annotation,
    Assignment,
    Case,
    CodingSession,
    SharedCodebook,
    Submission,
    MergeAudit,
    InvitationEmail,
)
from .services import (
    Conflict,
    assign_case,
    check_revision,
    create_session,
    differences,
    issue_link,
    cancel_invitation_emails,
    delete_coder_session,
    remove_assignment,
    lock_active_session,
    reopen_assignment,
    withdraw_submission,
    load_book,
    merge_submission,
    serialize_annotations,
    span,
    submit,
    validate_book,
)
from .importers import import_zip
from .invitations import queue_invitation_email
from .codebook_review import describe_changes, concept_path
from .colors import code_color, colored_book, highlight_style, highlighted_segments


def api_errors(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except Conflict as error:
            return JsonResponse({"error": str(error)}, status=409)
        except (
            ValueError,
            KeyError,
            TypeError,
            ValidationError,
            yaml.YAMLError,
            zipfile.BadZipFile,
        ) as error:
            return JsonResponse({"error": str(error)}, status=400)

    return wrapped


def current_session(request):
    identity = request.session.get("coder_session")
    session = get_object_or_404(CodingSession, pk=identity) if identity else None
    if (
        not session
        or session.deleted_at
        or not session.token_hash
        or not secrets.compare_digest(
            request.session.get("coder_token_hash", ""), session.token_hash
        )
    ):
        raise Http404("Private link required")
    return session


def access(request):
    return render(request, "corpus/access.html")


@require_POST
@api_errors
def exchange(request):
    token = json.loads(request.body)["token"]
    if not isinstance(token, str) or len(token) != 43:
        raise ValueError("Invalid private link")
    digest = hashlib.sha256(token.encode()).hexdigest()
    session = get_object_or_404(
        CodingSession, token_hash=digest, deleted_at__isnull=True
    )
    request.session.cycle_key()
    request.session["coder_session"] = str(session.pk)
    request.session["coder_token_hash"] = digest
    return JsonResponse({"url": "/work/"})


def workspace(request):
    session = current_session(request)
    return render(request, "corpus/work.html", {"coder": session.coder})


def state(request):
    session = current_session(request)
    assignments = []
    for a in (
        Assignment.objects.filter(session=session, removed_at__isnull=True)
        .select_related("case")
        .order_by(Lower("case__name"), "case__name", "pk")
    ):
        assignments.append(
            dict(
                id=a.pk,
                case=a.case.name,
                metadata=a.case.metadata,
                locked=a.locked,
                revision=a.revision,
                documents=[
                    dict(
                        id=d.pk,
                        name=d.document.name,
                        hash=d.sha256,
                        text=d.text,
                        metadata=d.metadata,
                    )
                    for d in a.documents.select_related("document")
                ],
                annotations=serialize_annotations(a),
            )
        )
    return JsonResponse(
        dict(
            coder=session.coder,
            revision=session.revision,
            codebook=session.codebook,
            colors={
                code["id"]: code_color(code)
                for code in session.codebook["codes"]
                if code["kind"] == "code"
            },
            yaml=yaml.safe_dump(
                colored_book(session.codebook), sort_keys=False, allow_unicode=True
            ),
            assignments=assignments,
        )
    )


@require_POST
@api_errors
@transaction.atomic
def annotation(request, assignment_id):
    session = current_session(request)
    # Always lock session before assignment, matching submission/codebook operations.
    session = lock_active_session(session.pk)
    a = get_object_or_404(
        Assignment.objects.select_for_update(),
        pk=assignment_id,
        session=session,
        removed_at__isnull=True,
    )
    body = json.loads(request.body)
    check_revision(a, body["revision"])
    if a.locked:
        raise Conflict("Submitted case must be reopened by the admin")
    existing = (
        get_object_or_404(Annotation, pk=body["id"], assignment=a)
        if body.get("id")
        else None
    )
    if body.get("delete"):
        if not existing:
            raise ValueError("Choose an annotation to delete")
        existing.delete()
    else:
        check_revision(session, body["codebook_revision"])
        document = get_object_or_404(a.documents, pk=body["document"])
        selected = span(document.text, body["start"], body["end"])
        if (
            document.sha256 != body["document_hash"]
            or selected != body["selected_text"]
        ):
            raise ValueError("Selected passage or document version differs")
        allowed = {
            c["id"]
            for c in session.codebook["codes"]
            if c["kind"] == "code" and not c.get("archived")
        }
        if existing:
            allowed.update(existing.codes)
        codes = body.get("codes", [])
        if (
            not isinstance(codes, list)
            or not codes
            or not set(codes) <= allowed
            or len(set(codes)) != len(codes)
        ):
            raise ValueError("Select active codes from your codebook")
        item = existing or Annotation(assignment=a)
        for field, value in dict(
            document=document,
            start=body["start"],
            end=body["end"],
            selected_text=selected,
            codes=codes,
            memo=body.get("memo", ""),
        ).items():
            setattr(item, field, value)
        item.save()
    a.revision += 1
    a.save()
    return JsonResponse(
        {"revision": a.revision, "annotations": serialize_annotations(a)}
    )


@require_POST
@api_errors
@transaction.atomic
def codebook(request):
    session = current_session(request)
    session = lock_active_session(session.pk)
    body = json.loads(request.body)
    check_revision(session, body["revision"])
    book = (
        load_book(body["yaml"]) if "yaml" in body else validate_book(body["codebook"])
    )
    before = {c["id"]: c for c in session.codebook["codes"]}
    after = {c["id"]: c for c in book["codes"]}
    referenced = {
        code
        for codes in Annotation.objects.filter(assignment__session=session).values_list(
            "codes", flat=True
        )
        for code in codes
    }
    for snapshot in Submission.objects.filter(assignment__session=session).values_list(
        "snapshot", flat=True
    ):
        referenced.update(code for a in snapshot["annotations"] for code in a["codes"])
    if referenced - after.keys():
        raise ValueError("Archive referenced codes instead of deleting them")
    for identity in referenced:
        if before[identity]["kind"] != after[identity]["kind"]:
            raise ValueError("Referenced concept kind cannot change")
    # Validate explicit corpus excerpts against this coder’s assigned versions.
    versions = {
        d.pk: d
        for a in Assignment.objects.filter(session=session, removed_at__isnull=True)
        for d in a.documents.all()
    }
    for code in book["codes"]:
        for reference in code.get("references", []):
            if reference.get("type") == "corpus":
                d = versions.get(reference.get("document"))
                if not d and reference in before.get(code["id"], {}).get(
                    "references", []
                ):
                    # Retain previously validated evidence when its case is removed.
                    continue
                if (
                    not d
                    or d.sha256 != reference.get("hash")
                    or span(d.text, reference["start"], reference["end"])
                    != reference["quote"]
                ):
                    raise ValueError(
                        "Corpus reference does not match an assigned document"
                    )
    session.codebook = deepcopy(book)
    session.revision += 1
    session.save()
    return JsonResponse({"revision": session.revision, "codebook": book})


@require_POST
@api_errors
def complete(request, assignment_id):
    session = current_session(request)
    get_object_or_404(
        Assignment, pk=assignment_id, session=session, removed_at__isnull=True
    )
    result = submit(assignment_id, session.pk, json.loads(request.body)["revision"])
    return JsonResponse({"submission": result.pk})


@require_POST
@api_errors
def unsubmit(request, assignment_id):
    session = current_session(request)
    get_object_or_404(
        Assignment, pk=assignment_id, session=session, removed_at__isnull=True
    )
    assignment = reopen_assignment(
        assignment_id,
        session.pk,
        json.loads(request.body)["revision"],
        initiated_by="coder",
    )
    return JsonResponse({"revision": assignment.revision, "locked": False})


def yaml_response(book, filename):
    response = HttpResponse(
        yaml.safe_dump(colored_book(book), sort_keys=False, allow_unicode=True),
        content_type="application/yaml",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}.yaml"'
    return response


def personal_export(request):
    return yaml_response(current_session(request).codebook, "personal-codebook")


@staff_member_required
@api_errors
def dashboard(request):
    notice = ""
    if request.method == "POST":
        action = request.POST["action"]
        if action == "create":
            with transaction.atomic():
                session, link = create_session(
                    request.POST["coder"],
                    Case.objects.filter(pk__in=request.POST.getlist("cases")),
                    email=request.POST.get("email", ""),
                )
                if session.email:
                    queue_invitation_email(session, link)
                    notice = (
                        "Invitation queued for "
                        + session.email
                        + ". Private link: "
                        + request.build_absolute_uri(link)
                    )
                else:
                    notice = request.build_absolute_uri(link)
        elif action == "send_invitation":
            email = CodingSession._meta.get_field("email").clean(
                request.POST.get("email", "").strip(), None
            )
            validate_email(email)
            from django.conf import settings

            if not settings.CASE_EMAIL_ENABLED:
                raise ValueError("Transactional email is not enabled")
            with transaction.atomic():
                session = get_object_or_404(
                    CodingSession.objects.select_for_update(),
                    pk=request.POST["session"],
                    deleted_at__isnull=True,
                )
                session.email = email
                session.save(update_fields=["email"])
                link = issue_link(session)
                queue_invitation_email(session, link)
                notice = "New private link and case list queued for " + email
        elif action in ("delete_session", "delete_invitation"):
            session = get_object_or_404(CodingSession, pk=request.POST["session"])
            delete_coder_session(session.pk)
            notice = "Coder session deleted. Access revoked; coding records retained."
        elif action == "remove_assignment":
            assignment = get_object_or_404(
                Assignment,
                pk=request.POST["assignment"],
                session__deleted_at__isnull=True,
            )
            remove_assignment(assignment.pk, int(request.POST["revision"]))
            notice = "Assignment removed. Existing coding records have been retained."
        elif action == "assign":
            with transaction.atomic():
                session = get_object_or_404(
                    CodingSession.objects.select_for_update(),
                    pk=request.POST["session"],
                    deleted_at__isnull=True,
                )
                for case in Case.objects.filter(pk__in=request.POST.getlist("cases")):
                    assign_case(session, case)
                notice = "Cases assigned. Existing coding is restored when a removed case is reassigned."
        elif action in ("rotate", "revoke"):
            with transaction.atomic():
                session = get_object_or_404(
                    CodingSession.objects.select_for_update(),
                    pk=request.POST["session"],
                    deleted_at__isnull=True,
                )
                if action == "rotate":
                    notice = request.build_absolute_uri(issue_link(session))
                else:
                    session.token_hash = ""
                    session.save()
                    cancel_invitation_emails(session)
                    notice = "Private link revoked"
        elif action == "reopen":
            assignment = get_object_or_404(
                Assignment,
                pk=request.POST["assignment"],
                removed_at__isnull=True,
                session__deleted_at__isnull=True,
            )
            reopen_assignment(assignment.pk)
            notice = "Case reopened. Its previous submission remains in the archive."
        elif action == "delete_submission":
            submission = get_object_or_404(
                Submission,
                pk=request.POST["submission"],
                assignment__removed_at__isnull=True,
                assignment__session__deleted_at__isnull=True,
            )
            assignment = withdraw_submission(submission.pk)
            notice = (
                "Submitted case removed from review. Its coding history is retained. "
                + (
                    "The case is open for editing."
                    if not assignment.locked
                    else "The newer submission remains locked."
                )
            )
        elif action == "corpus":
            notice = f"Imported {import_zip(request.FILES['file'])} documents"
        elif action == "book":
            with transaction.atomic():
                SharedCodebook.objects.select_for_update().order_by("pk").first()
                book = load_book(request.FILES["file"].read().decode())
                previous = SharedCodebook.objects.order_by("-pk").first()
                if previous:
                    new_ids = {c["id"] for c in book["codes"]}
                    if {c["id"] for c in previous.content["codes"]} - new_ids:
                        raise ValueError(
                            "Archive existing shared concepts instead of removing their identities"
                        )
                SharedCodebook.objects.create(content=book)
                notice = "Shared codebook version published"
    return render(
        request,
        "corpus/dashboard.html",
        dict(
            notice=notice,
            cases=Case.objects.order_by(Lower("name"), "name", "pk"),
            sessions=CodingSession.objects.filter(
                deleted_at__isnull=True
            ).prefetch_related(
                Prefetch(
                    "invitation_emails",
                    queryset=InvitationEmail.objects.order_by("-pk").defer("body"),
                    to_attr="invitation_history",
                )
            ),
            assignments=Assignment.objects.filter(
                removed_at__isnull=True, session__deleted_at__isnull=True
            )
            .select_related("session", "case")
            .order_by(Lower("case__name"), Lower("session__coder"), "pk"),
            submissions=Submission.objects.select_related(
                "assignment__session", "assignment__case", "email_notification"
            )
            .filter(
                assignment__removed_at__isnull=True,
                assignment__session__deleted_at__isnull=True,
                withdrawal__isnull=True,
            )
            .order_by("-pk"),
            shared=SharedCodebook.objects.order_by("-pk").first(),
        ),
    )


@staff_member_required
@api_errors
def review(request, submission_id):
    submission = get_object_or_404(
        Submission.objects.select_related("assignment__session__base"), pk=submission_id
    )
    shared = SharedCodebook.objects.order_by("-pk").first()
    changes = differences(submission, shared)
    if request.method == "POST":
        decisions = {
            c["id"]: dict(
                action=request.POST["action_" + c["id"]],
                target=request.POST.get("target_" + c["id"]),
            )
            for c in changes
        }
        merge_submission(
            submission.pk, request.user, int(request.POST["revision"]), decisions
        )
        return redirect("review", submission_id=submission.pk)
    changes = describe_changes(
        changes,
        submission.assignment.session.base.content,
        submission.snapshot["codebook"],
        shared.content,
    )
    mapping_options = sorted(
        [
            dict(id=code["id"], name=concept_path(code["id"], shared.content))
            for code in shared.content["codes"]
        ],
        key=lambda item: item["name"].casefold(),
    )
    snapshot = deepcopy(submission.snapshot)
    snapshot["codebook"] = colored_book(snapshot["codebook"])
    lookup = {code["id"]: code for code in snapshot["codebook"]["codes"]}
    for annotation in snapshot["annotations"]:
        annotation["highlight_style"] = highlight_style(
            [code_color(lookup[identity]) for identity in sorted(annotation["codes"])]
        )
    for code in snapshot["codebook"]["codes"]:
        code["highlight_style"] = (
            highlight_style([code_color(code)]) if code["kind"] == "code" else ""
        )
    documents = {
        d.document.name: highlighted_segments(
            d.text,
            [a for a in snapshot["annotations"] if a["document"] == d.pk],
            snapshot["codebook"]["codes"],
        )
        for d in submission.assignment.documents.all()
    }
    return render(
        request,
        "corpus/review.html",
        dict(
            submission=submission,
            changes=changes,
            shared=shared,
            snapshot=snapshot,
            documents=documents,
            mapping_options=mapping_options,
            withdrawn=hasattr(submission, "withdrawal"),
            audit=MergeAudit.objects.filter(submission=submission).first(),
        ),
    )


@staff_member_required
def shared_export(request):
    shared = (
        get_object_or_404(SharedCodebook, pk=request.GET.get("revision"))
        if request.GET.get("revision")
        else SharedCodebook.objects.order_by("-pk").first()
    )
    if not shared:
        raise Http404
    return yaml_response(shared.content, "shared-codebook")


@staff_member_required
def results_export(request, submission_id):
    submission = get_object_or_404(Submission, pk=submission_id)
    audit = MergeAudit.objects.filter(submission=submission).first()
    mappings = audit.mappings if audit else {}
    shared_book = audit.result if audit else submission.assignment.session.base
    shared_ids = {code["id"] for code in shared_book.content["codes"]}
    rows = []
    book = {c["id"]: c for c in submission.snapshot["codebook"]["codes"]}
    for annotation in submission.snapshot["annotations"]:
        rows.append(
            dict(
                **annotation,
                coder=submission.snapshot["coder"],
                case=submission.snapshot["case"],
                case_key=submission.snapshot["case_key"],
                submission=submission.pk,
                submitted_at=submission.created.isoformat(),
                shared_revision=shared_book.pk,
                code_details=[book[c] for c in annotation["codes"]],
                shared_codes=[
                    mappings.get(
                        c,
                        (c if c in shared_ids else None),
                    )
                    for c in annotation["codes"]
                ],
            )
        )
    if request.GET.get("format") == "csv":
        stream = io.StringIO()
        fields = [
            "submission",
            "submitted_at",
            "coder",
            "case",
            "case_key",
            "document",
            "document_key",
            "document_name",
            "document_hash",
            "start",
            "end",
            "selected_text",
            "codes",
            "code_details",
            "shared_codes",
            "shared_revision",
            "memo",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        json.dumps(value, ensure_ascii=False)
                        if isinstance(value, (list, dict))
                        else value
                    )
                    for key, value in row.items()
                    if key in fields
                }
            )
        response = HttpResponse(stream.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="submission-{submission.pk}.csv"'
        )
        return response
    return JsonResponse(
        dict(
            submission=submission.pk,
            snapshot=submission.snapshot,
            mappings=mappings,
            results=rows,
        )
    )


def health(request):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})


@staff_member_required
def coding_archive(request):
    return render(
        request,
        "corpus/archive.html",
        dict(
            sessions=CodingSession.objects.filter(deleted_at__isnull=False).order_by(
                "-deleted_at"
            ),
            assignments=Assignment.objects.filter(
                Q(removed_at__isnull=False) | Q(session__deleted_at__isnull=False)
            )
            .select_related("session", "case")
            .order_by("-removed_at", "-pk"),
            submissions=Submission.objects.filter(
                Q(withdrawal__isnull=False)
                | Q(assignment__removed_at__isnull=False)
                | Q(assignment__session__deleted_at__isnull=False)
            )
            .select_related("assignment__session", "assignment__case", "withdrawal")
            .order_by("-pk"),
        ),
    )
