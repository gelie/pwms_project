"""Permission-agnostic querysets and page-shaping helpers for the API."""

from uuid import UUID

from django.contrib.contenttypes.models import ContentType
from django.db.models import DateField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce, TruncDate
from django.utils.dateparse import parse_date
from ninja.errors import HttpError

from ..models import (
    Attachment,
    Bill,
    BillVersion,
    Group,
    GroupMembership,
    Meeting,
    MeetingAttendance,
    TransitionLog,
    User,
    WorkflowNote,
    WorkflowReferral,
)
from .pagination import _paginate


def _user_queryset():
    return User.with_party_affiliation().select_related("department", "supervisor")


def _membership_queryset():
    return GroupMembership.objects.select_related("user", "group", "role")


def _bill_queryset():
    """Bills with their related rows and the date the current stage was entered.

    ``bill_stage_date`` is the local date of the bill's latest recorded
    transition; ``created_at``'s date for bills that never left the initial
    stage. Computed in SQL so list responses stay one query per page.
    """
    latest_stage_entry = (
        TransitionLog.objects.filter(
            content_type=ContentType.objects.get_for_model(Bill),
            object_id=OuterRef("pk"),
        )
        .order_by("-timestamp")
        .annotate(entry_day=TruncDate("timestamp"))
        .values("entry_day")[:1]
    )
    return (
        Bill.objects.annotate(
            bill_stage_date=Coalesce(
                Subquery(latest_stage_entry, output_field=DateField()),
                TruncDate("created_at"),
                output_field=DateField(),
            )
        )
        .select_related(
            "sponsor",
            "responsible_committee",
            "current_state",
            "current_state__public_status",
        )
        # current_version / introduced_version / *_date are derived from the
        # version history on the model; one query per page instead of per bill.
        # The history and each attachment's version mirror are also serialised.
        .prefetch_related(
            Prefetch(
                "versions",
                queryset=BillVersion.objects.select_related("recorded_by"),
            ),
            Prefetch(
                "attachments",
                queryset=Attachment.objects.select_related(
                    "uploaded_by"
                ).prefetch_related("versions"),
            ),
        )
    )


def _attach_bill_relations(bills):
    """Bulk-load each bill's note log and referrals (one query per kind).

    Both are generic-FK rows with no reverse relation, so they cannot be
    prefetched through ``prefetch_related``. Fetching them for the whole page in
    two queries and attaching the lists keeps list responses free of N+1
    queries; the schema reads ``api_notes`` / ``api_referrals``.
    """
    bills = list(bills)
    if not bills:
        return bills
    content_type = ContentType.objects.get_for_model(Bill)
    object_ids = [bill.pk for bill in bills]

    notes_by_bill = {}
    for note in (
        WorkflowNote.objects.filter(content_type=content_type, object_id__in=object_ids)
        .select_related("author")
        .order_by("-created_at", "-id")
    ):
        notes_by_bill.setdefault(note.object_id, []).append(note)

    referrals_by_bill = {}
    for referral in (
        WorkflowReferral.objects.filter(
            content_type=content_type, object_id__in=object_ids
        )
        .select_related("referred_to", "referred_by", "responded_by", "recalled_by")
        .order_by("-referred_at", "-id")
    ):
        referrals_by_bill.setdefault(referral.object_id, []).append(referral)

    for bill in bills:
        bill.api_notes = notes_by_bill.get(bill.pk, [])
        bill.api_referrals = referrals_by_bill.get(bill.pk, [])
    return bills


def _group_queryset():
    memberships = GroupMembership.objects.select_related("user", "role").order_by(
        "-start_date"
    )
    return Group.objects.select_related("parent").prefetch_related(
        Prefetch("members", queryset=memberships, to_attr="api_memberships")
    )


def _meeting_queryset():
    """Meetings with everything the API serialises, in one query per relation.

    The attendance register is prefetched because each meeting's tally is derived
    from it; without the prefetch every row on a page would re-query its
    register. ``attendance_counts()`` and the member lookup then read the cache.
    """
    return Meeting.objects.select_related(
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
        "host_group",
        "chaired_by",
        "secretary",
        "venue",
        "venue__building",
    ).prefetch_related(
        Prefetch(
            "attendances",
            queryset=MeetingAttendance.objects.select_related("user"),
        )
    )


def _resolve_user(value: str):
    """Find a user by ``public_id``, username or email (``None`` when absent)."""
    condition = Q(username__iexact=value) | Q(email__iexact=value)
    try:
        public_id = UUID(str(value))
    except AttributeError, TypeError, ValueError:
        pass
    else:
        condition |= Q(public_id=public_id)
    return User.objects.filter(condition).order_by("pk").first()


def _parse_period(value: str | None, name: str):
    """Parse an inclusive ``YYYY-MM-DD`` period bound, rejecting a bad value."""
    if not value:
        return None
    parsed = parse_date(value)
    if parsed is None:
        raise HttpError(400, f"{name} must be a date in YYYY-MM-DD format.")
    return parsed


def _attach_meeting_counts(meetings, member=None):
    """Attach each meeting's register tally (and the member's own row) in place."""
    for meeting in meetings:
        meeting.api_attendance_counts = meeting.attendance_counts()
        meeting.api_member_attendance = None
        if member is not None:
            meeting.api_member_attendance = next(
                (row for row in meeting.attendances.all() if row.user_id == member.pk),
                None,
            )
    return meetings


def _meetings_page(request, queryset, limit: int, offset: int, member=None) -> dict:
    """Paginate meetings, then compute each page's register tally."""
    page = _paginate(request, queryset, limit, offset)
    _attach_meeting_counts(page["results"], member=member)
    return page


def _bills_page(request, queryset, limit: int, offset: int) -> dict:
    """Paginate bills, then bulk-load each page's notes and referrals."""
    page = _paginate(request, queryset, limit, offset)
    _attach_bill_relations(page["results"])
    return page
