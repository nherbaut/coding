"""SSO-protected GitHub App authorization and new-study import UI."""

import base64
import hashlib
import secrets
import time
import uuid
from urllib.parse import urlencode
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib import messages
from django.db import transaction, IntegrityError
from django.http import HttpResponseBadRequest
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from .permissions import admin_required
from .studies import get_study
from .models import GitHubStudy
from .github_client import (
    GitHubClient,
    GitHubError,
    configured,
    http,
    installation_client,
)
from .github_repository import inspect_repository
from .github_sync import import_study


def cipher():
    return Fernet(
        base64.urlsafe_b64encode(
            hashlib.sha256(
                (settings.SECRET_KEY + ":github-user-oauth").encode()
            ).digest()
        )
    )


def user_client(request):
    identity = request.session.get("github_authorization", {})
    if (
        identity.get("admin") != str(request.user.pk)
        or identity.get("expires", 0) <= time.time()
    ):
        raise GitHubError("Connect your GitHub account to continue")
    try:
        return GitHubClient(cipher().decrypt(identity["token"].encode()).decode())
    except (KeyError, InvalidToken):
        raise GitHubError("Reconnect your GitHub account") from None


@admin_required
@require_POST
def oauth_start(request):
    if not configured():
        messages.error(request, "The server GitHub App is not configured yet")
        return redirect("github-create")
    return_study = request.POST.get("study", "")
    if return_study:
        try:
            uuid.UUID(return_study)
        except ValueError:
            return HttpResponseBadRequest("Invalid study")
        get_study(request.user, return_study, owner=True)
    request.session.pop("github_preview", None)
    state = secrets.token_urlsafe(32)
    request.session["github_oauth_state"] = dict(
        state=state,
        admin=str(request.user.pk),
        expires=time.time() + 600,
        return_study=return_study,
    )
    callback = settings.PUBLIC_BASE_URL + reverse("github-callback")
    return redirect(
        "https://github.com/login/oauth/authorize?"
        + urlencode(
            dict(
                client_id=settings.GITHUB_CLIENT_ID, redirect_uri=callback, state=state
            )
        )
    )


@admin_required
@require_GET
def oauth_callback(request):
    expected = request.session.pop("github_oauth_state", {})
    state = request.GET.get("state", "")
    if (
        expected.get("admin") != str(request.user.pk)
        or expected.get("expires", 0) <= time.time()
        or not secrets.compare_digest(expected.get("state", ""), state)
        or not state
    ):
        messages.error(
            request,
            "GitHub authorization expired or its state was invalid. Please start again.",
        )
        return redirect("github-create")
    try:
        if request.GET.get("error") or not request.GET.get("code"):
            raise GitHubError("GitHub authorization was not completed")
        token = http(
            "https://github.com/login/oauth/access_token",
            method="POST",
            data=dict(
                client_id=settings.GITHUB_CLIENT_ID,
                client_secret=settings.GITHUB_CLIENT_SECRET,
                code=request.GET["code"],
                redirect_uri=settings.PUBLIC_BASE_URL + reverse("github-callback"),
            ),
        )
        if not token.get("access_token"):
            raise GitHubError("GitHub did not authorize this connection")
        profile = GitHubClient(token["access_token"]).api("/user")
        request.session["github_authorization"] = dict(
            admin=str(request.user.pk),
            login=profile["login"],
            expires=time.time() + min(int(token.get("expires_in", 28800)), 28800),
            token=cipher().encrypt(token["access_token"].encode()).decode(),
        )
        messages.success(
            request, "GitHub connected. Choose an installed repository and a branch."
        )
    except (GitHubError, KeyError, TypeError, ValueError) as error:
        messages.error(
            request,
            (
                str(error)
                if isinstance(error, GitHubError)
                else "GitHub authorization failed"
            ),
        )
    if expected.get("return_study"):
        get_study(request.user, expected["return_study"], owner=True)
        return redirect("github-status", study_id=expected["return_study"])
    return redirect("github-create")


def selection(request, client, selection):
    repository = client.repository_access(
        int(selection["repository_id"]), int(selection["installation"])
    )
    branch = selection["branch"]
    if not branch or len(branch) > 255 or any(ord(c) < 32 for c in branch):
        raise ValueError("Choose a valid branch")
    installation = int(selection["installation"])
    remote = installation_client(installation, repository["id"])
    head = remote.head(repository["full_name"], branch)
    tree = remote.tree(repository["full_name"], head["tree"])
    files = remote.read_files(repository["full_name"], tree)
    return repository, installation, remote, head, tree, files


@admin_required
def create(request):
    context = dict(configured=configured(), app_slug=settings.GITHUB_APP_SLUG)
    if not configured():
        return render(request, "corpus/github_create.html", context)
    try:
        client = user_client(request)
    except GitHubError:
        context["needs_connection"] = True
        return render(request, "corpus/github_create.html", context)
    context["login"] = request.session["github_authorization"]["login"]
    try:
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "preview":
                repository_id, installation = request.POST.get("repository", "").split(
                    ":"
                )
                choice = dict(
                    repository_id=int(repository_id),
                    installation=int(installation),
                    branch=request.POST.get("branch", "").strip(),
                    title=request.POST.get("title", "").strip(),
                )
                if not choice["title"] or len(choice["title"]) > 200:
                    raise ValueError("Enter a study title of at most 200 characters")
                repository, installation, _, head, tree, files = selection(
                    request, client, choice
                )
                if GitHubStudy.objects.filter(
                    repository_id=repository["id"], branch=choice["branch"]
                ).exists():
                    raise ValueError(
                        "This repository and branch already belong to a study"
                    )
                data = inspect_repository(files, repository["id"])
                choice.update(commit=head["sha"], nonce=secrets.token_urlsafe(32))
                request.session["github_preview"] = choice
                context.update(
                    preview=choice,
                    repository=repository["full_name"],
                    cases=data["cases"],
                    documents=data["documents"],
                    concept_count=len(data["book"]["codes"]),
                )
            elif action == "create":
                choice = request.session.get("github_preview", {})
                if not choice or not secrets.compare_digest(
                    choice["nonce"], request.POST.get("nonce", "")
                ):
                    raise ValueError("Preview the import before creating the study")
                repository, installation, _, head, tree, files = selection(
                    request, client, choice
                )
                if head["sha"] != choice["commit"]:
                    raise GitHubError(
                        "The branch changed after the preview. Preview the current branch before importing.",
                        conflict=True,
                    )
                study = import_study(
                    request.user,
                    repository,
                    choice["branch"],
                    installation,
                    head,
                    files,
                    tree,
                    choice["title"],
                )
                request.session.pop("github_preview", None)
                messages.success(
                    request,
                    "Study imported. Its corpus is frozen; automatic GitHub publication is queued.",
                )
                return redirect("github-status", study_id=study.pk)
            else:
                raise ValueError("Unknown import action")
        if not context.get("preview"):
            context["repositories"] = client.repositories()
    except (GitHubError, ValueError, KeyError, TypeError, IntegrityError) as error:
        context["error"] = (
            str(error)
            if isinstance(error, (GitHubError, ValueError))
            else "The repository manifest or import request is invalid"
        )
        return render(
            request,
            "corpus/github_create.html",
            context,
            status=409 if getattr(error, "conflict", False) else 400,
        )
    return render(request, "corpus/github_create.html", context)


@admin_required
def status(request, study_id):
    study = get_study(request.user, study_id)
    binding = GitHubStudy.objects.filter(study=study).first()
    if request.method == "POST":
        if not binding:
            messages.error(
                request, "Only new studies imported from GitHub can be synchronized"
            )
            return redirect("github-status", study_id=study.pk)
        action = request.POST.get("action")
        if action in ("pause", "resume", "reconnect"):
            get_study(request.user, study_id, owner=True)
        if action == "reconnect":
            try:
                client = user_client(request)
                candidates = [
                    item
                    for item in client.repositories()
                    if item["id"] == binding.repository_id
                ]
                if len(candidates) != 1:
                    raise GitHubError(
                        "Install the App on this repository, then reconnect your GitHub account"
                    )
                candidate = candidates[0]
                repository = client.repository_access(
                    binding.repository_id, candidate["installation"]
                )
                installation_client(
                    candidate["installation"], binding.repository_id
                ).head(repository["full_name"], binding.branch)
                GitHubStudy.objects.filter(pk=binding.pk).update(
                    repository=repository["full_name"],
                    installation_id=candidate["installation"],
                    blocked=False,
                    next_attempt_at=timezone.now(),
                )
                binding.runs.filter(status="blocked").update(status="prepared")
                messages.success(
                    request,
                    "Repository access reconnected. Its branch, frozen corpus and publication history remain unchanged.",
                )
            except GitHubError as error:
                messages.error(request, str(error))
            return redirect("github-status", study_id=study.pk)
        with transaction.atomic():
            binding = GitHubStudy.objects.select_for_update().get(pk=binding.pk)
            if action == "retry":
                binding.blocked = False
                binding.next_attempt_at = timezone.now()
                binding.runs.filter(status="blocked").update(status="prepared")
            elif action in ("pause", "resume"):
                binding.paused = action == "pause"
            else:
                return render(
                    request,
                    "corpus/github_status.html",
                    dict(
                        study=study,
                        binding=binding,
                        error="Unknown synchronization action",
                    ),
                    status=400,
                )
            binding.save(update_fields=["blocked", "next_attempt_at", "paused"])
        messages.success(
            request,
            "Synchronization settings updated. In-flight publication may finish; new jobs respect the pause setting.",
        )
        return redirect("github-status", study_id=study.pk)
    return render(
        request,
        "corpus/github_status.html",
        dict(
            study=study,
            binding=binding,
            pending=(binding.events.filter(published=False).count() if binding else 0),
            runs=binding.runs.order_by("-created")[:20] if binding else [],
            owner=study.owner_id == request.user.pk,
        ),
    )
