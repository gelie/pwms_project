"""The NinjaAPI instance and the router objects the endpoints register on."""

from django.conf import settings
from ninja import NinjaAPI, Router

from .auth import api_auth
from .throttling import ClientRateThrottle
from .webhook_auth import webhook_auth

api = NinjaAPI(
    title="PWMS API",
    version="1.0.0",
    description=(
        "Read-only API for registered partner applications. Obtain an access "
        "token from `POST /auth/token/` with the client id/secret issued by a "
        "PWMS administrator, then send `Authorization: Bearer <access_token>`. "
        f"Access tokens live for {settings.EXTERNAL_API_ACCESS_TOKEN_TTL_MINUTES} "
        f"minutes; refresh tokens live for "
        f"{settings.EXTERNAL_API_REFRESH_TOKEN_TTL_MINUTES} minutes and rotate on "
        "use. Each key only sees the scopes it was registered for. "
        "Use `GET /search/` to search users, groups, memberships and bills in one call."
        " Use `GET /meetings/?user=` to list a member's meetings."
    ),
    urls_namespace="api",
    auth=api_auth,
    throttle=ClientRateThrottle(),
)

auth_router = Router(tags=["Authentication"], auth=None)
users_router = Router(tags=["Users"])
groups_router = Router(tags=["Groups"])
memberships_router = Router(tags=["Memberships"])
bills_router = Router(tags=["Bills"])
meetings_router = Router(tags=["Meetings"])
# Authenticated with one shared secret (see ``webhook_auth``), not a client
# access token: the sender is this application itself.
bill_updates_router = Router(tags=["Bill updates"], auth=webhook_auth)
