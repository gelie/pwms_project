"""Meeting resource endpoints."""

from uuid import UUID

from django.db.models import Q
from django.shortcuts import get_object_or_404
from ninja.errors import HttpError

from ..app import meetings_router
from ..auth import require_scope
from ..querysets import (
    _attach_meeting_counts,
    _meeting_queryset,
    _meetings_page,
    _parse_period,
    _resolve_user,
)
from ..schemas import ErrorOut, MeetingDetailOut, MeetingPage
from ..scopes import Scope
from ..search import MEETING_SEARCH_FIELDS, apply_search, group_filter_q

MEETING_ORDERING = {
    "start_datetime",
    "end_datetime",
    "title",
    "meeting_type",
    "created_at",
}


def _meeting_ordering(value: str) -> str:
    field = value.removeprefix("-")
    if field not in MEETING_ORDERING:
        return "-start_datetime"
    return value


@meetings_router.get(
    "/",
    response={200: MeetingPage, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut},
    summary="List meetings, optionally for one member",
)
def list_meetings(
    request,
    limit: int = 20,
    offset: int = 0,
    search: str = "",
    search_fields: str | None = None,
    user: str | None = None,
    group: str | None = None,
    meeting_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    state: str | None = None,
    minutes_status: str | None = None,
    attendance_mode: str | None = None,
    ordering: str = "-start_datetime",
):
    """Every meeting the key may read, filtered by member, group and period.

    ``user`` is the headline filter: pass a ``public_id``, username or email and
    the result is that person's meetings — the ones they sit on the attendance
    register for, chair or minute, own or are assigned, or that are convened by
    a group they actively belong to (``MeetingQuerySet.for_member``). Results
    then carry the member's own RSVP/attendance row in ``member_attendance``.

    ``start``/``end`` are inclusive ``YYYY-MM-DD`` bounds on the start date, so
    ``?user=jsmith&start=2026-01-01&end=2026-06-30&group=Health`` answers "what
    did this member attend in the first half of the year, in Health?".
    """
    require_scope(request, Scope.MEETINGS_READ)

    queryset = apply_search(
        _meeting_queryset(), search, MEETING_SEARCH_FIELDS, search_fields
    ).distinct()

    member = None
    if user:
        member = _resolve_user(user)
        if member is None:
            # Unlike the other filters, an unresolved person is almost always a
            # bad identifier rather than an empty diary, so say which formats
            # the filter accepts instead of silently returning zero rows.
            raise HttpError(
                404,
                f"No user matches '{user}'. Pass a public_id, username or email.",
            )
        queryset = queryset.for_member(member)

    if group:
        queryset = queryset.filter(group_filter_q("host_group", group))

    if meeting_type:
        types = [value.strip() for value in meeting_type.split(",") if value.strip()]
        if types:
            queryset = queryset.filter(meeting_type__in=types)

    start_date = _parse_period(start, "start")
    end_date = _parse_period(end, "end")
    if start_date is not None and end_date is not None:
        if start_date > end_date:
            raise HttpError(400, "start must be on or before end.")
        queryset = queryset.in_period(start_date, end_date)
    elif start_date is not None:
        queryset = queryset.filter(start_datetime__date__gte=start_date)
    elif end_date is not None:
        queryset = queryset.filter(start_datetime__date__lte=end_date)

    if state:
        queryset = queryset.filter(
            Q(current_state__slug__iexact=state) | Q(current_state__name__iexact=state)
        )
    if minutes_status:
        queryset = queryset.filter(minutes_status__iexact=minutes_status)
    if attendance_mode:
        queryset = queryset.filter(attendance_mode__iexact=attendance_mode)

    return _meetings_page(
        request, queryset.order_by(_meeting_ordering(ordering)), limit, offset, member
    )


@meetings_router.get(
    "/{public_id}/",
    response={200: MeetingDetailOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut},
    summary="Retrieve a meeting with its attendance register",
)
def get_meeting(request, public_id: UUID, user: str | None = None):
    """One meeting, its register and (optionally) one member's place on it."""
    require_scope(request, Scope.MEETINGS_READ)
    meeting = get_object_or_404(_meeting_queryset(), public_id=public_id)

    member = _resolve_user(user) if user else None
    _attach_meeting_counts([meeting], member=member)
    meeting.api_attendances = list(meeting.attendances.all())
    return meeting
