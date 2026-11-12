"""Recoverable private links for study admins; authentication still uses hashes."""

import base64
import hashlib
import hmac
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from .services import Conflict


def link_cipher():
    key = hashlib.sha256(
        b"coding-private-links:v1:" + settings.SECRET_KEY.encode()
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def private_link(session):
    if session.admin_user_id or session.deleted_at or not session.token_hash:
        raise Conflict("This session has no active private link.")
    if not session.encrypted_token:
        raise Conflict(
            "This link was not stored for later viewing. Generate a new private link to make it available here; the existing link remains valid until then."
        )
    try:
        token = link_cipher().decrypt(session.encrypted_token.encode()).decode()
    except (InvalidToken, UnicodeError, ValueError):
        raise Conflict(
            "The private link cannot be decrypted. Generate a new private link."
        ) from None
    if not hmac.compare_digest(
        hashlib.sha256(token.encode()).hexdigest(), session.token_hash
    ):
        raise Conflict(
            "The stored private link is no longer active. Generate a new private link."
        )
    return "/access/#" + token
