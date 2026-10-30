"""Access/refresh token issuing and verification.

Two credentials are deliberately different in kind:

* **Access tokens** are short-lived (15 minutes by default) and stateless:
  a JSON payload signed with Django's signer. Every API request presents one in
  ``Authorization: Bearer <token>``.
* **Refresh tokens** live for an hour and are opaque random strings stored as a
  SHA-256 hash in :class:`~pwms.api.models.ApiRefreshToken`. Redeeming
  one rotates it, so a stolen refresh token is usable at most once.

Access tokens are checked against the client's *current* scopes and active flag
on every request, so disabling a client or removing a scope takes effect
immediately rather than when the 15-minute token happens to expire.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone

from .models import ApiClient, ApiRefreshToken

ACCESS_TOKEN_SALT = "pwms.api.access_token"


def access_token_ttl() -> timedelta:
    return timedelta(
        minutes=getattr(settings, "EXTERNAL_API_ACCESS_TOKEN_TTL_MINUTES", 15)
    )


def refresh_token_ttl() -> timedelta:
    return timedelta(
        minutes=getattr(settings, "EXTERNAL_API_REFRESH_TOKEN_TTL_MINUTES", 1440)
    )


def _signing_key() -> str:
    return getattr(settings, "EXTERNAL_API_SIGNING_KEY", settings.SECRET_KEY)


def issue_access_token(client: ApiClient) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)`` for ``client``."""
    now = timezone.now()
    ttl = access_token_ttl()
    expires_at = now + ttl
    payload = {
        "sub": client.client_id,
        "scopes": client.enabled_scopes,
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_hex(8),
    }
    token = signing.dumps(
        payload, salt=ACCESS_TOKEN_SALT, key=_signing_key(), compress=True
    )
    return token, int(ttl.total_seconds())


def decode_access_token(token: str) -> tuple[ApiClient, list[str]] | None:
    """Validate ``token`` and return ``(client, effective_scopes)`` or ``None``.

    The signed payload's age is enforced by :func:`django.core.signing.loads`;
    the ``exp`` claim is also checked so a rotated signing key or clock skew
    cannot extend a token's life.
    """
    ttl_seconds = access_token_ttl().total_seconds()
    try:
        payload = signing.loads(
            token,
            salt=ACCESS_TOKEN_SALT,
            key=_signing_key(),
            max_age=ttl_seconds,
        )
    except signing.BadSignature, ValueError, TypeError:
        return None

    if not isinstance(payload, dict) or payload.get("typ") != "access":
        return None
    if int(payload.get("exp") or 0) <= int(timezone.now().timestamp()):
        return None

    client = ApiClient.objects.filter(
        client_id=payload.get("sub"), is_active=True
    ).first()
    if client is None:
        return None

    granted = set(payload.get("scopes") or [])
    return client, [scope for scope in client.enabled_scopes if scope in granted]


def issue_refresh_token(client: ApiClient) -> str:
    """Create and return a new opaque refresh token for ``client``."""
    raw = secrets.token_urlsafe(48)
    ApiRefreshToken.objects.create(
        client=client,
        token_hash=ApiRefreshToken.hash_token(raw),
        expires_at=timezone.now() + refresh_token_ttl(),
    )
    return raw


def redeem_refresh_token(raw_token: str) -> tuple[ApiClient, str] | None:
    """Consume ``raw_token`` and return ``(client, new_raw_refresh_token)``.

    Redemption is single-use: the presented row is revoked before the
    replacement is created, so replaying the old token fails.
    """
    token = (
        ApiRefreshToken.objects.select_related("client")
        .filter(token_hash=ApiRefreshToken.hash_token(raw_token))
        .first()
    )
    if token is None or not token.is_usable:
        return None

    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at", "updated_at"])
    return token.client, issue_refresh_token(token.client)


def revoke_refresh_token(raw_token: str) -> None:
    """Revoke a refresh token if it is still live (idempotent)."""
    ApiRefreshToken.objects.filter(
        token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now())
