"""Models for externally registered API clients and their audit trail.

The external API has its own credential model rather than reusing the project's
user accounts because its callers are *applications*, not people: each
registration carries a name, a description, a human contact, a set of scopes and
a rate limit, and every request is attributed to it in :class:`ApiUsageLog`.
"""

from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from ..models import BaseModel
from .scopes import ALL_SCOPES, Scope


def generate_client_id() -> str:
    """A public, non-secret identifier for an app registration."""
    return secrets.token_hex(16)


def default_rate_limit() -> int:
    """Requests per minute for a new client (overridable in the admin)."""
    return getattr(settings, "EXTERNAL_API_DEFAULT_RATE_LIMIT_PER_MINUTE", 60)


class ApiClient(BaseModel):
    """An application authorised to call the external API.

    ``client_id`` is public (it travels in the token request and appears in
    documentation). Only a one-way hash of ``client_secret`` is stored, so a
    secret can be shown exactly once — when it is generated or rotated. Use the
    admin's *Rotate client secret* action or
    ``manage.py rotate_api_secret <client_id>`` to issue a new one.
    """

    name = models.CharField(
        max_length=150,
        unique=True,
        help_text=_("Human-readable application name, e.g. 'Parking System'."),
    )
    description = models.TextField(
        blank=True,
        help_text=_("What the client uses the API for."),
    )
    contact_name = models.CharField(max_length=150, blank=True)
    contact_email = models.EmailField(
        help_text=_("Owner/administrator of the integration."),
    )
    client_id = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        default=generate_client_id,
    )
    client_secret_hash = models.CharField(
        max_length=256,
        blank=True,
        editable=False,
        help_text=_("PBKDF2 hash of the client secret. Never displayed."),
    )
    scopes = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Subset of the API's read scopes this client may use."),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_(
            "Inactive clients can neither obtain nor use tokens. Existing "
            "access tokens stop working immediately."
        ),
    )
    rate_limit_per_minute = models.PositiveIntegerField(
        default=default_rate_limit,
        help_text=_("Maximum requests per minute across all endpoints."),
    )
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    secret_rotated_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["name"]
        verbose_name = "API client"
        verbose_name_plural = "API clients"

    def __str__(self) -> str:
        return str(self.name)

    @property
    def has_secret(self) -> bool:
        return bool(self.client_secret_hash)

    @property
    def enabled_scopes(self) -> list[str]:
        """The scopes this client may use, in canonical order and deduplicated."""
        configured = set(self.scopes or [])
        return [scope for scope in ALL_SCOPES if scope in configured]

    def has_scope(self, scope: str | Scope) -> bool:
        return str(scope) in self.enabled_scopes

    def set_client_secret(self, raw_secret: str | None = None) -> str:
        """Set a new secret and return the plaintext for one-time display."""
        raw = raw_secret or secrets.token_urlsafe(48)
        self.client_secret_hash = make_password(raw)
        self.secret_rotated_at = timezone.now()
        return raw

    def rotate_secret(self) -> str:
        """Replace the stored secret and return the new plaintext once."""
        raw = self.set_client_secret()
        self.save(
            update_fields=["client_secret_hash", "secret_rotated_at", "updated_at"]
        )
        return raw

    def check_client_secret(self, raw_secret: str) -> bool:
        if not self.client_secret_hash or not raw_secret:
            return False
        return check_password(raw_secret, self.client_secret_hash)

    def clean(self) -> None:
        unknown = sorted(set(self.scopes or []) - set(ALL_SCOPES))
        if unknown:
            raise ValidationError(
                {"scopes": f"Unknown scope(s): {', '.join(unknown)}."}
            )

    def save(self, *args, **kwargs):
        # Reject unknown scopes rather than silently dropping them, then
        # normalise so a hand-edited row cannot smuggle duplicates or an
        # obsolete scope into a token.
        self.clean()
        self.scopes = self.enabled_scopes
        if not self.client_id:
            self.client_id = generate_client_id()
        super().save(*args, **kwargs)


class ApiRefreshToken(BaseModel):
    """A server-side refresh token, stored as a hash.

    Refresh tokens are opaque random strings rather than signed payloads so
    they can be rotated and revoked: redeeming one marks it used and issues a
    new one, and a leaked token therefore has a single use.
    """

    client = models.ForeignKey(
        ApiClient,
        on_delete=models.CASCADE,
        related_name="refresh_tokens",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "API refresh token"
        verbose_name_plural = "API refresh tokens"

    def __str__(self) -> str:
        return f"{self.client.name} (expires {self.expires_at:%Y-%m-%d %H:%M})"

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None and not self.is_expired and self.client.is_active


class ApiUsageLog(BaseModel):
    """One request made through the external API.

    ``client`` is null for requests that failed before authentication (for
    example a token request with a bad secret). The log is intentionally
    append-only and registered read-only in the admin.
    """

    client = models.ForeignKey(
        ApiClient,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="usage_logs",
    )
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=500)
    endpoint = models.CharField(max_length=200, blank=True)
    status_code = models.PositiveSmallIntegerField()
    duration_ms = models.PositiveIntegerField(default=0)
    remote_addr = models.CharField(max_length=45, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    was_throttled = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["client", "-created_at"]),
            models.Index(fields=["-created_at"]),
        ]
        verbose_name = "API usage log"
        verbose_name_plural = "API usage logs"

    def __str__(self) -> str:
        who = self.client.name if self.client_id else "anonymous"
        return f"{who}: {self.method} {self.path} -> {self.status_code}"
