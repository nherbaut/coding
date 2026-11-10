"""Request-scoped administrator identity supplied by the trusted Authelia proxy."""

import ipaddress
import secrets
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponseForbidden


class AutheliaAdminMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.AUTHELIA_AUTH_ENABLED:
            return self.get_response(request)
        # A Django login cookie alone cannot authorize an administrator in SSO mode.
        request.user = AnonymousUser()
        admin_path = request.path == "/" or request.path.startswith(
            ("/admin", "/review", "/exports")
        )
        if not admin_path:
            return self.get_response(request)
        try:
            peer = ipaddress.ip_address(request.META.get("REMOTE_ADDR", ""))
            trusted = any(
                peer in ipaddress.ip_network(network)
                for network in settings.AUTHELIA_TRUSTED_PROXIES
            )
        except ValueError:
            trusted = False
        supplied = request.META.get("HTTP_X_CODING_PROXY_SECRET", "")
        if (
            not trusted
            or not settings.AUTHELIA_PROXY_SECRET
            or not secrets.compare_digest(supplied, settings.AUTHELIA_PROXY_SECRET)
        ):
            return HttpResponseForbidden(
                "Administrator access requires the trusted authentication proxy."
            )
        username = request.META.get("HTTP_REMOTE_USER", "").strip()
        groups = {
            group.strip()
            for group in request.META.get("HTTP_REMOTE_GROUPS", "").split(",")
        }
        if (
            not username
            or len(username) > 140
            or settings.AUTHELIA_ADMIN_GROUP not in groups
        ):
            return HttpResponseForbidden(
                "Administrator access requires membership in "
                + settings.AUTHELIA_ADMIN_GROUP
                + "."
            )
        user, created = get_user_model().objects.get_or_create(
            username="authelia:" + username
        )
        if not user.is_active:
            return HttpResponseForbidden("This administrator account is disabled.")
        if (
            created
            or not user.is_staff
            or user.is_superuser
            or user.has_usable_password()
        ):
            user.is_staff = True
            user.is_superuser = False
            user.set_unusable_password()
            user.save(update_fields=["is_staff", "is_superuser", "password"])
        request.user = user
        return self.get_response(request)
