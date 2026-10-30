"""Django-Ninja API for external (partner application) integrations.

Mounted at ``/api/v1/`` from :mod:`pwms.root_urls`. Three read-only resource
families are exposed — users, memberships and bills — and access to each is
gated by the scope registered on the calling :class:`ApiClient`.
"""

from __future__ import annotations

from uuid import UUID

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import NinjaAPI, Router
from ninja.errors import HttpError

from ..models import Bill, GroupMembership, User
from .auth import api_auth, require_scope
from .models import ApiClient
from .schemas import (
    BillOut,
    BillPage,
    ClientInfoOut,
    ErrorOut,
    MembershipOut,
    MembershipPage,
    RefreshRequest,
    ResourceLink,
    RevokeRequest,
    RootOut,
    SearchOut,
    TokenPairOut,
    TokenRequest,
    UserOut,
    UserPage,
)
from .scopes import Scope
from .search import (
    BILL_SEARCH_FIELDS,
    MEMBERSHIP_SEARCH_FIELDS,
    USER_SEARCH_FIELDS,
    apply_search,
    group_filter_q,
    user_filter_q,
)
from .throttling import ClientRateThrottle, TokenEndpointThrottle
from .tokens import (
    issue_access_token,
    issue_refresh_token,
    redeem_refresh_token,
    refresh_token_ttl,
    revoke_refresh_token,
)

# Used to spend the same PBKDF2 work when an unknown client_id is presented, so
# response timing does not reveal whether a client id exists.
_DUMMY_SECRET_HASH = make_password("not-a-real-secret")

api = NinjaAPI(
    title="PWMS External API",
    version="1.0.0",
    description=(
        "Read-only API for registered partner applications. Obtain an access "
        "token from `POST /auth/token/` with the client id/secret issued by a "
        "PWMS administrator, then send `Authorization: Bearer <access_token>`. "
        f"Access tokens live for {settings.EXTERNAL_API_ACCESS_TOKEN_TTL_MINUTES} "
        f"minutes; refresh tokens live for "
        f"{settings.EXTERNAL_API_REFRESH_TOKEN_TTL_MINUTES} minutes and rotate on "
        "use. Each key only sees the scopes it was registered for. "
        "Use `GET /search/` to search users, memberships and bills in one call."
    ),
    urls_namespace="api",
    auth=api_auth,
    throttle=ClientRateThrottle(),
)

auth_router = Router(tags=["Authentication"], auth=None)
users_router = Router(tags=["Users"])
memberships_router = Router(tags=["Memberships"])
bills_router = Router(tags=["Bills"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _max_page_size() -> int:
    return getattr(settings, "EXTERNAL_API_MAX_PAGE_SIZE", 100)


def _page_url(request, limit: int, offset: int) -> str:
    """Build the absolute URL for a page, preserving the other query params."""
    params = request.GET.copy()
    params["limit"] = str(limit)
    params["offset"] = str(offset)
    return f"{request.build_absolute_uri(request.path)}?{params.urlencode()}"


def _paginate(request, queryset, limit: int, offset: int) -> dict:
    limit = max(1, min(int(limit), _max_page_size()))
    offset = max(0, int(offset))
    count = queryset.count()
    results = list(queryset[offset : offset + limit])
    return {
        "count": count,
        "limit": limit,
        "offset": offset,
        "next": (
            _page_url(request, limit, offset + limit)
            if offset + limit < count
            else None
        ),
        "previous": (
            _page_url(request, limit, max(0, offset - limit)) if offset > 0 else None
        ),
        "results": results,
    }


def _empty_page(limit: int, offset: int) -> dict:
    return {
        "count": 0,
        "limit": limit,
        "offset": offset,
        "next": None,
        "previous": None,
        "results": [],
    }


def _clamp_limit(limit: int, *, default: int = 10) -> int:
    return max(1, min(int(limit or default), _max_page_size()))


def _user_queryset():
    return User.objects.select_related("department", "supervisor")


def _membership_queryset():
    return GroupMembership.objects.select_related("user", "group", "role")


def _bill_queryset():
    return Bill.objects.select_related(
        "sponsor",
        "responsible_committee",
        "current_state",
        "current_state__public_status",
    )


SEARCH_RESOURCES = ("users", "memberships", "bills")


def _token_pair(client: ApiClient, refresh_token: str | None = None) -> dict:
    access_token, expires_in = issue_access_token(client)
    refresh_token = refresh_token or issue_refresh_token(client)
    ApiClient.objects.filter(pk=client.pk).update(last_used_at=timezone.now())
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "refresh_expires_in": int(refresh_token_ttl().total_seconds()),
        "scope": " ".join(client.enabled_scopes),
    }


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@auth_router.post(
    "/token/",
    response={200: TokenPairOut, 400: ErrorOut, 401: ErrorOut},
    throttle=TokenEndpointThrottle(),
    summary="Exchange client credentials for an access/refresh token pair",
)
def obtain_token(request, payload: TokenRequest):
    if payload.grant_type != "client_credentials":
        raise HttpError(400, "Only grant_type=client_credentials is supported.")

    client = ApiClient.objects.filter(client_id=payload.client_id).first()
    if client is None:
        # Equalise timing with the known-client path.
        check_password(payload.client_secret, _DUMMY_SECRET_HASH)
        raise HttpError(401, "Invalid client credentials.")

    # Attribute failed attempts to the key as well as successful ones, so the
    # usage log answers "who tried what" even when authentication failed.
    request.api_client = client
    secret_ok = client.check_client_secret(payload.client_secret)
    if not client.is_active or not secret_ok:
        raise HttpError(401, "Invalid client credentials.")

    return _token_pair(client)


@auth_router.post(
    "/token/refresh/",
    response={200: TokenPairOut, 401: ErrorOut},
    throttle=TokenEndpointThrottle(),
    summary="Rotate a refresh token and return a new token pair",
)
def refresh_token(request, payload: RefreshRequest):
    redeemed = redeem_refresh_token(payload.refresh_token)
    if redeemed is None:
        raise HttpError(401, "Invalid, expired or already used refresh token.")
    client, new_refresh_token = redeemed
    # Reuse the replacement minted by the rotation instead of creating a second
    # live refresh token that the caller would never see.
    return _token_pair(client, refresh_token=new_refresh_token)


@auth_router.post(
    "/token/revoke/",
    response={204: None},
    throttle=TokenEndpointThrottle(),
    summary="Revoke a refresh token",
)
def revoke_token(request, payload: RevokeRequest):
    revoke_refresh_token(payload.refresh_token)
    return 204, None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@api.get("/", response=RootOut, summary="Describe the API for the calling key")
def api_root(request):
    principal = request.auth
    resources = []
    if principal.has_scope(Scope.USERS_READ):
        resources.append(ResourceLink(name="users", url="/api/v1/users/"))
    if principal.has_scope(Scope.MEMBERSHIPS_READ):
        resources.append(ResourceLink(name="memberships", url="/api/v1/memberships/"))
    if principal.has_scope(Scope.BILLS_READ):
        resources.append(ResourceLink(name="bills", url="/api/v1/bills/"))
    if resources:
        resources.append(ResourceLink(name="search", url="/api/v1/search/"))

    return {
        "api": "PWMS External API",
        "version": "1.0.0",
        "client": principal.client.name,
        "scopes": list(principal.scopes),
        "documentation": "/api/v1/docs",
        "resources": resources,
    }


@api.get("/me/", response=ClientInfoOut, summary="The calling client registration")
def whoami(request):
    client = request.auth.client
    return {
        "name": client.name,
        "client_id": client.client_id,
        "description": client.description,
        "contact_name": client.contact_name,
        "contact_email": client.contact_email,
        "scopes": client.enabled_scopes,
        "rate_limit_per_minute": client.rate_limit_per_minute,
        "last_used_at": client.last_used_at,
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

USER_ORDERING = {"last_name", "first_name", "username", "date_joined"}


def _user_ordering(value: str) -> str:
    field = value.removeprefix("-")
    if field not in USER_ORDERING:
        return "last_name"
    return value


@users_router.get(
    "/",
    response={200: UserPage, 400: ErrorOut, 403: ErrorOut},
    summary="List users",
)
def list_users(
    request,
    limit: int = 20,
    offset: int = 0,
    search: str = "",
    search_fields: str | None = None,
    is_active: bool | None = None,
    is_mp: bool | None = None,
    is_staff_member: bool | None = None,
    department: str | None = None,
    ordering: str = "last_name",
):
    require_scope(request, Scope.USERS_READ)

    queryset = apply_search(_user_queryset(), search, USER_SEARCH_FIELDS, search_fields)
    if is_active is not None:
        queryset = queryset.filter(is_active=is_active)
    if is_mp is not None:
        queryset = queryset.filter(is_mp=is_mp)
    if is_staff_member is not None:
        queryset = queryset.filter(is_staff_member=is_staff_member)
    if department:
        queryset = queryset.filter(group_filter_q("department", department))

    return _paginate(
        request, queryset.order_by(_user_ordering(ordering)), limit, offset
    )


@users_router.get(
    "/{public_id}/",
    response={200: UserOut, 403: ErrorOut, 404: ErrorOut},
    summary="Retrieve a user",
)
def get_user(request, public_id: UUID):
    require_scope(request, Scope.USERS_READ)
    return get_object_or_404(_user_queryset(), public_id=public_id)


# ---------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------

MEMBERSHIP_ORDERING = {"start_date", "end_date", "created_at"}


def _membership_ordering(value: str) -> str:
    field = value.removeprefix("-")
    if field not in MEMBERSHIP_ORDERING:
        return "-start_date"
    return value


@memberships_router.get(
    "/",
    response={200: MembershipPage, 400: ErrorOut, 403: ErrorOut},
    summary="List group memberships",
)
def list_memberships(
    request,
    limit: int = 20,
    offset: int = 0,
    search: str = "",
    search_fields: str | None = None,
    is_active: bool | None = None,
    user: str | None = None,
    group: str | None = None,
    role: str | None = None,
    ordering: str = "-start_date",
):
    require_scope(request, Scope.MEMBERSHIPS_READ)

    queryset = apply_search(
        _membership_queryset(), search, MEMBERSHIP_SEARCH_FIELDS, search_fields
    )
    if is_active is not None:
        queryset = queryset.filter(is_active=is_active)
    if user:
        queryset = queryset.filter(user_filter_q("user", user))
    if group:
        queryset = queryset.filter(group_filter_q("group", group))
    if role:
        queryset = queryset.filter(
            Q(role__slug__iexact=role) | Q(role__name__iexact=role)
        )

    return _paginate(
        request, queryset.order_by(_membership_ordering(ordering)), limit, offset
    )


@memberships_router.get(
    "/{public_id}/",
    response={200: MembershipOut, 403: ErrorOut, 404: ErrorOut},
    summary="Retrieve a membership",
)
def get_membership(request, public_id: UUID):
    require_scope(request, Scope.MEMBERSHIPS_READ)
    return get_object_or_404(_membership_queryset(), public_id=public_id)


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------

BILL_ORDERING = {
    "bill_number",
    "title",
    "introduced_date",
    "created_at",
    "updated_at",
}


def _bill_ordering(value: str) -> str:
    field = value.removeprefix("-")
    if field not in BILL_ORDERING:
        return "-created_at"
    return value


@bills_router.get(
    "/",
    response={200: BillPage, 400: ErrorOut, 403: ErrorOut},
    summary="List bills",
)
def list_bills(
    request,
    limit: int = 20,
    offset: int = 0,
    search: str = "",
    search_fields: str | None = None,
    bill_type: str | None = None,
    house_of_origin: str | None = None,
    public_status: str | None = None,
    responsible_committee: str | None = None,
    ordering: str = "-created_at",
):
    require_scope(request, Scope.BILLS_READ)

    queryset = apply_search(_bill_queryset(), search, BILL_SEARCH_FIELDS, search_fields)
    if bill_type:
        queryset = queryset.filter(bill_type=bill_type)
    if house_of_origin:
        queryset = queryset.filter(house_of_origin=house_of_origin)
    if public_status:
        queryset = queryset.filter(
            Q(current_state__public_status__slug__iexact=public_status)
            | Q(current_state__public_status__name__iexact=public_status)
        )
    if responsible_committee:
        queryset = queryset.filter(
            group_filter_q("responsible_committee", responsible_committee)
        )

    return _paginate(
        request, queryset.order_by(_bill_ordering(ordering)), limit, offset
    )


@bills_router.get(
    "/{public_id}/",
    response={200: BillOut, 403: ErrorOut, 404: ErrorOut},
    summary="Retrieve a bill",
)
def get_bill(request, public_id: UUID):
    require_scope(request, Scope.BILLS_READ)
    return get_object_or_404(_bill_queryset(), public_id=public_id)


# ---------------------------------------------------------------------------
# Cross-resource search
# ---------------------------------------------------------------------------


def _parse_search_resources(raw_resources: str, accessible: list[str]) -> list[str]:
    if raw_resources:
        requested = [
            item.strip().lower() for item in raw_resources.split(",") if item.strip()
        ]
    else:
        requested = list(accessible)

    requested = list(dict.fromkeys(requested))
    unknown = [name for name in requested if name not in SEARCH_RESOURCES]
    if unknown:
        raise HttpError(
            400,
            f"Unknown search resource(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(SEARCH_RESOURCES)}.",
        )

    denied = [name for name in requested if name not in accessible]
    if denied:
        raise HttpError(
            403,
            f"This API key does not grant access to: {', '.join(denied)}.",
        )

    return requested


@api.get(
    "/search/",
    response={200: SearchOut, 400: ErrorOut, 403: ErrorOut},
    summary="Search users, memberships and bills",
)
def search(request, q: str, resources: str = "", limit: int = 10, offset: int = 0):
    """Search every resource the key may read, across all of its text fields.

    ``q`` is required (at least two characters). ``resources`` optionally
    narrows the search to a comma-separated subset; asking for a resource the
    key has no scope for is a 403. ``limit``/``offset`` paginate each result
    group independently.
    """
    term = q.strip()
    if len(term) < 2:
        raise HttpError(400, "q must contain at least 2 characters.")

    accessible = []
    for name, scope in (
        ("users", Scope.USERS_READ),
        ("memberships", Scope.MEMBERSHIPS_READ),
        ("bills", Scope.BILLS_READ),
    ):
        if request.auth.has_scope(scope):
            accessible.append(name)

    selected = _parse_search_resources(resources, accessible)
    limit = _clamp_limit(limit)
    offset = max(0, int(offset))

    payload = {
        "query": term,
        "resources": selected,
        "skipped": [name for name in SEARCH_RESOURCES if name not in selected],
        "users": _empty_page(limit, offset),
        "memberships": _empty_page(limit, offset),
        "bills": _empty_page(limit, offset),
    }

    if "users" in selected:
        queryset = apply_search(_user_queryset(), term, USER_SEARCH_FIELDS)
        payload["users"] = _paginate(
            request,
            queryset.order_by("last_name", "first_name", "username"),
            limit,
            offset,
        )

    if "memberships" in selected:
        queryset = apply_search(_membership_queryset(), term, MEMBERSHIP_SEARCH_FIELDS)
        payload["memberships"] = _paginate(
            request, queryset.order_by("-start_date"), limit, offset
        )

    if "bills" in selected:
        queryset = apply_search(_bill_queryset(), term, BILL_SEARCH_FIELDS)
        payload["bills"] = _paginate(
            request, queryset.order_by("-created_at"), limit, offset
        )

    return payload


# ---------------------------------------------------------------------------
# URL wiring
# ---------------------------------------------------------------------------

api.add_router("/auth", auth_router)
api.add_router("/users", users_router)
api.add_router("/memberships", memberships_router)
api.add_router("/bills", bills_router)
