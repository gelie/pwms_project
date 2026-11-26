"""Membership resource endpoints."""

from uuid import UUID

from django.db.models import Q
from django.shortcuts import get_object_or_404

from ..app import memberships_router
from ..auth import require_scope
from ..pagination import _paginate
from ..querysets import _membership_queryset
from ..schemas import ErrorOut, MembershipOut, MembershipPage
from ..scopes import Scope
from ..search import (
    MEMBERSHIP_SEARCH_FIELDS,
    apply_search,
    group_filter_q,
    user_filter_q,
)

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
