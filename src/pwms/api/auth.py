"""Bearer authentication and scope enforcement for the external API."""

from __future__ import annotations

from dataclasses import dataclass

from ninja.errors import HttpError
from ninja.security import HttpBearer

from .models import ApiClient
from .scopes import Scope
from .tokens import decode_access_token


@dataclass(frozen=True)
class ApiPrincipal:
    """What Ninja places on ``request.auth`` after a successful token check."""

    client: ApiClient
    scopes: tuple[str, ...]

    def has_scope(self, scope: str | Scope) -> bool:
        return str(scope) in self.scopes


class BearerTokenAuth(HttpBearer):
    """Validate ``Authorization: Bearer <access token>``.

    Ninja calls this before throttling, so ``request.auth`` is available to
    :class:`~pwms.api.throttling.ClientRateThrottle`. It also sets
    ``request.api_client`` for the usage-log middleware, which runs outside
    Ninja's request cycle.
    """

    def authenticate(self, request, token: str):
        decoded = decode_access_token(token)
        if decoded is None:
            return None
        client, scopes = decoded
        principal = ApiPrincipal(client=client, scopes=tuple(scopes))
        request.api_client = client
        request.api_scopes = principal.scopes
        return principal


api_auth = BearerTokenAuth()


def require_scope(request, scope: str | Scope) -> None:
    """Raise ``403`` unless the authenticated client was granted ``scope``."""
    principal = getattr(request, "auth", None)
    if principal is None or not principal.has_scope(scope):
        raise HttpError(
            403,
            f"This API key does not grant the required scope: {scope}.",
        )
