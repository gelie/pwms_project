"""API token authentication with an absolute lifetime.

``rest_framework.authtoken`` issues tokens that never expire — a leaked key is a
permanent credential until someone deletes it. :class:`ExpiringTokenAuthentication`
adds an **absolute** TTL on top of the stock model: a token is accepted only while
it is younger than ``settings.TOKEN_TTL``, measured from its ``created`` column.

The TTL is absolute, not sliding — using a token does not extend it — so a client
that needs continuity calls ``POST /api/auth/token/refresh/`` (which rotates the
key) before the TTL elapses. ``manage.py purge_tokens`` deletes the expired rows;
expiry is enforced at authentication time, so the command only reclaims storage.

Set ``TOKEN_TTL_DAYS=0`` (equivalently ``TOKEN_TTL = None``) to disable expiry and
keep the stock, non-expiring behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed


def resolve_token_ttl(ttl_days: int | None = None) -> timedelta | None:
    """
    The effective token TTL as a :class:`~datetime.timedelta`, or ``None``.

    ``None`` means "never expires". With no argument the value comes from
    ``settings.TOKEN_TTL``; ``ttl_days`` overrides it (the ``purge_tokens``
    ``--days`` option), where ``0`` or less again means "never expires".
    """
    if ttl_days is None:
        return getattr(settings, "TOKEN_TTL", None)
    return timedelta(days=ttl_days) if ttl_days > 0 else None


def token_expires_at(token: Token, ttl: timedelta | None = None) -> datetime | None:
    """When ``token`` stops being accepted, or ``None`` when expiry is disabled."""
    ttl = resolve_token_ttl() if ttl is None else ttl
    if not ttl:
        return None
    return token.created + ttl


def token_is_expired(
    token: Token, *, now: datetime | None = None, ttl: timedelta | None = None
) -> bool:
    """True when ``token`` has passed its TTL (always False if expiry is disabled)."""
    expires_at = token_expires_at(token, ttl)
    if expires_at is None:
        return False
    return (now or timezone.now()) >= expires_at


def issue_token(user) -> Token:
    """
    Return ``user``'s current valid token, rotating an expired one.

    Re-issuing must not hand back a key that authentication would immediately
    reject, so an existing-but-expired token is replaced rather than returned.
    """
    token, created = Token.objects.get_or_create(user=user)
    if not created and token_is_expired(token):
        token.delete()
        token = Token.objects.create(user=user)
    return token


class ExpiringTokenAuthentication(TokenAuthentication):
    """``TokenAuthentication`` that refuses tokens older than ``settings.TOKEN_TTL``."""

    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)
        if token_is_expired(token):
            # Same failure shape as an unknown key, so the client re-authenticates
            # through the normal obtain/refresh path.
            raise AuthenticationFailed("Token has expired.")
        return (user, token)
