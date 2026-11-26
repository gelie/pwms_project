"""Group resource endpoints."""

from uuid import UUID

from django.shortcuts import get_object_or_404

from ..app import groups_router
from ..auth import require_scope
from ..pagination import _paginate
from ..querysets import _group_queryset
from ..schemas import ErrorOut, GroupOut, GroupPage
from ..scopes import Scope
from ..search import GROUP_SEARCH_FIELDS, apply_search, group_filter_q

GROUP_ORDERING = {"name", "group_type", "start_date", "end_date", "created_at"}


def _group_ordering(value: str) -> str:
    field = value.removeprefix("-")
    if field not in GROUP_ORDERING:
        return "name"
    return value


@groups_router.get(
    "/",
    response={200: GroupPage, 400: ErrorOut, 403: ErrorOut},
    summary="List groups",
)
def list_groups(
    request,
    limit: int = 20,
    offset: int = 0,
    search: str = "",
    search_fields: str | None = None,
    group_type: str | None = None,
    parent: str | None = None,
    is_active: bool | None = None,
    ordering: str = "name",
):
    require_scope(request, Scope.GROUPS_READ)

    queryset = apply_search(
        _group_queryset(), search, GROUP_SEARCH_FIELDS, search_fields
    )
    if group_type:
        queryset = queryset.filter(group_type=group_type)
    if parent:
        queryset = queryset.filter(group_filter_q("parent", parent))
    if is_active is not None:
        queryset = queryset.filter(is_active=is_active)

    return _paginate(
        request, queryset.order_by(_group_ordering(ordering)), limit, offset
    )


@groups_router.get(
    "/{public_id}/",
    response={200: GroupOut, 403: ErrorOut, 404: ErrorOut},
    summary="Retrieve a group with its memberships",
)
def get_group(request, public_id: UUID):
    require_scope(request, Scope.GROUPS_READ)
    return get_object_or_404(_group_queryset(), public_id=public_id)
