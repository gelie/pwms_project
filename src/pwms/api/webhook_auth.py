"""Bearer authentication for the Bill update webhook receiver.

The receiver is a testing stand-in for the partner system's endpoint, so it is
authenticated with one shared secret (``BILL_UPDATE_WEBHOOK_TOKEN``) rather than
an :class:`~pwms.api.models.ApiClient` token: the sender is this application
itself, and access tokens expire far too quickly for a settings value to hold.

An unset token closes the receiver: every post is rejected until one is
configured, so an accidentally exposed endpoint cannot accept writes.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from ninja.security import HttpBearer


class WebhookTokenAuth(HttpBearer):
    """Accept ``Authorization: Bearer <BILL_UPDATE_WEBHOOK_TOKEN>``."""

    def authenticate(self, request, token):
        expected = getattr(settings, "BILL_UPDATE_WEBHOOK_TOKEN", "") or ""
        if not expected:
            return None
        # Constant-time: the token is a secret, not a request identifier.
        if secrets.compare_digest(token, expected):
            return "bill-update-webhook"
        return None


webhook_auth = WebhookTokenAuth()
