"""Study-scoped SSO workspaces, shared editing, and app-only logout."""

from copy import deepcopy
from functools import wraps
import json
from django.contrib.auth import logout as auth_logout
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_POST, require_GET
from .models import CodingSession, Study, Case
from .permissions import admin_required
from .studies import get_study, publish_codebook, referenced_concepts
from .colors import code_color
from .services import assign_case
from . import views


class PrivateResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.path == "/"
            or request.path.startswith(
                ("/admin/", "/api/", "/work/", "/access/", "/logout/", "/signed-out/")
            )
            or request.user.is_authenticated
            or request.session.get("coder_session")
        ):
            add_never_cache_headers(response)
        return response


@require_POST
def logout(request):
    auth_logout(request)
    return redirect("signed-out")


@require_GET
def signed_out(request):
    return render(request, "corpus/signed_out.html")


@transaction.atomic
def admin_session(study, user):
    # Creation serializes on the study; release before taking assignment locks.
    study = Study.objects.select_for_update().get(pk=study.pk)
    session, _ = CodingSession.objects.get_or_create(
        study=study,
        admin_user=user,
        deleted_at__isnull=True,
        defaults=dict(
            coder=f"{user.get_full_name() or user.username.removeprefix('authelia:')} (admin)",
            email=user.email,
            base=study.codebook,
            instructions=study.instructions,
            codebook=deepcopy(study.codebook.content),
        ),
    )
    return session


def coding_endpoint(view, *, initialize=False, sync=False):
    @admin_required
    @wraps(view)
    def endpoint(request, study_id, **kwargs):
        study = get_study(request.user, study_id)
        session = (
            admin_session(study, request.user)
            if initialize
            else get_object_or_404(
                CodingSession,
                study=study,
                admin_user=request.user,
                deleted_at__isnull=True,
            )
        )
        if sync:
            for case in Case.objects.filter(study=study):
                assign_case(session, case)
        request.admin_coding_session = session
        return view(request, **kwargs)

    return endpoint


coding_workspace = coding_endpoint(views.workspace, initialize=True, sync=True)
coding_state = coding_endpoint(views.state, sync=True)
coding_annotation = coding_endpoint(views.annotation)
coding_codebook = coding_endpoint(views.codebook)
coding_complete = coding_endpoint(views.complete)
coding_unsubmit = coding_endpoint(views.unsubmit)
coding_export = coding_endpoint(views.personal_export)
coding_book_view = coding_endpoint(views.codebook_view)
coding_book_view_state = coding_endpoint(views.codebook_view_state)


@admin_required
@require_GET
def graph(request, study_id):
    study = get_study(request.user, study_id)
    return render(request, "corpus/graph.html", {"study": study})


@admin_required
@require_GET
def graph_state(request, study_id):
    study = get_study(request.user, study_id)
    return JsonResponse(
        dict(
            revision=study.codebook_id,
            codebook=study.codebook.content,
            referenced=sorted(referenced_concepts(study)),
            colors={c["id"]: code_color(c) for c in study.codebook.content["codes"]},
        )
    )


@admin_required
@require_POST
@views.api_errors
def graph_publish(request, study_id):
    study = get_study(request.user, study_id)
    body = json.loads(request.body)
    revision = publish_codebook(
        study,
        body["codebook"],
        expected_revision=body["revision"],
        actor=request.user.pk,
        origin="graph-publication",
    )
    return JsonResponse(
        dict(
            revision=revision.pk,
            codebook=revision.content,
            referenced=sorted(referenced_concepts(study)),
            colors={c["id"]: code_color(c) for c in revision.content["codes"]},
        )
    )
