"""User resource endpoints."""

from uuid import UUID

from django.shortcuts import get_object_or_404

from ...models import User
from ..app import users_router
from ..auth import require_scope
from ..pagination import _paginate
from ..querysets import _user_queryset
from ..schemas import ErrorOut, MeetingPage, UserOut, UserPage
from ..scopes import Scope
from ..search import USER_SEARCH_FIELDS, apply_search, group_filter_q
from .meetings import list_meetings

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


@users_router.get(
    "/{public_id}/meetings/",
    response={200: MeetingPage, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut},
    summary="List a user's meetings",
)
def list_user_meetings(
    request,
    public_id: UUID,
    limit: int = 20,
    offset: int = 0,
    search: str = "",
    search_fields: str | None = None,
    group: str | None = None,
    meeting_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    state: str | None = None,
    minutes_status: str | None = None,
    attendance_mode: str | None = None,
    ordering: str = "-start_datetime",
):
    """A user's meetings, as ``GET /meetings/?user=<public_id>``.

    A convenience alias for integrations that address the person first. The
    same member/period/group/type filters and the same response shape apply;
    the user must exist (``404`` otherwise).
    """
    require_scope(request, Scope.MEETINGS_READ)
    get_object_or_404(User, public_id=public_id)
    return list_meetings(
        request,
        limit=limit,
        offset=offset,
        search=search,
        search_fields=search_fields,
        user=str(public_id),
        group=group,
        meeting_type=meeting_type,
        start=start,
        end=end,
        state=state,
        minutes_status=minutes_status,
        attendance_mode=attendance_mode,
        ordering=ordering,
    )
