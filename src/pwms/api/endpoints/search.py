"""Cross-resource search endpoint."""

from ninja.errors import HttpError

from ..app import api
from ..pagination import _clamp_limit, _empty_page, _paginate
from ..querysets import (
    _bill_queryset,
    _bills_page,
    _group_queryset,
    _membership_queryset,
    _user_queryset,
)
from ..schemas import ErrorOut, SearchOut
from ..scopes import Scope
from ..search import (
    BILL_SEARCH_FIELDS,
    GROUP_SEARCH_FIELDS,
    MEMBERSHIP_SEARCH_FIELDS,
    USER_SEARCH_FIELDS,
    apply_search,
)

SEARCH_RESOURCES = ("users", "groups", "memberships", "bills")


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
    summary="Search users, groups, memberships and bills",
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
        ("groups", Scope.GROUPS_READ),
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
        "groups": _empty_page(limit, offset),
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

    if "groups" in selected:
        queryset = apply_search(_group_queryset(), term, GROUP_SEARCH_FIELDS)
        payload["groups"] = _paginate(request, queryset.order_by("name"), limit, offset)

    if "memberships" in selected:
        queryset = apply_search(_membership_queryset(), term, MEMBERSHIP_SEARCH_FIELDS)
        payload["memberships"] = _paginate(
            request, queryset.order_by("-start_date"), limit, offset
        )

    if "bills" in selected:
        queryset = apply_search(_bill_queryset(), term, BILL_SEARCH_FIELDS)
        payload["bills"] = _bills_page(
            request, queryset.order_by("-created_at"), limit, offset
        )

    return payload
