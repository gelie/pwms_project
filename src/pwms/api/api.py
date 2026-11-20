"""Django-Ninja API for external (partner application) integrations.

Mounted at ``/api/v1/`` from :mod:`pwms.root_urls`. Four read-only resource
families are exposed — users, memberships, bills and meetings — and access to
each is gated by the scope registered on the calling :class:`ApiClient`.
"""

from __future__ import annotations

from uuid import UUID

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
from django.db.models import DateField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce, TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from ninja import NinjaAPI, Router
from ninja.errors import HttpError

from ..models import (
    Attachment,
    Bill,
    BillUpdateLog,
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
from .auth import api_auth, require_scope
from .models import ApiClient
from .schemas import (
    BillOut,
    BillPage,
    BillUpdateIn,
    BillUpdateReceiptOut,
    BillUpdateRejectionIn,
    BillUpdateRejectionReceiptOut,
    ClientInfoOut,
    ErrorOut,
    GroupOut,
    GroupPage,
    MeetingDetailOut,
    MeetingPage,
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
    GROUP_SEARCH_FIELDS,
    MEETING_SEARCH_FIELDS,
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
from .webhook_auth import webhook_auth

# Used to spend the same PBKDF2 work when an unknown client_id is presented, so
# response timing does not reveal whether a client id exists.
_DUMMY_SECRET_HASH = make_password("not-a-real-secret")

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


SEARCH_RESOURCES = ("users", "groups", "memberships", "bills")


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
    if principal.has_scope(Scope.GROUPS_READ):
        resources.append(ResourceLink(name="groups", url="/api/v1/groups/"))
    if principal.has_scope(Scope.MEMBERSHIPS_READ):
        resources.append(ResourceLink(name="memberships", url="/api/v1/memberships/"))
    if principal.has_scope(Scope.BILLS_READ):
        resources.append(ResourceLink(name="bills", url="/api/v1/bills/"))
    if principal.has_scope(Scope.MEETINGS_READ):
        resources.append(ResourceLink(name="meetings", url="/api/v1/meetings/"))
    if resources:
        resources.append(ResourceLink(name="search", url="/api/v1/search/"))

    return {
        "api": "PWMS API",
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


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

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


def _bills_page(request, queryset, limit: int, offset: int) -> dict:
    """Paginate bills, then bulk-load each page's notes and referrals."""
    page = _paginate(request, queryset, limit, offset)
    _attach_bill_relations(page["results"])
    return page


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

    return _bills_page(
        request, queryset.order_by(_bill_ordering(ordering)), limit, offset
    )


@bills_router.get(
    "/{public_id}/",
    response={200: BillOut, 403: ErrorOut, 404: ErrorOut},
    summary="Retrieve a bill",
)
def get_bill(request, public_id: UUID):
    require_scope(request, Scope.BILLS_READ)
    bill = get_object_or_404(_bill_queryset(), public_id=public_id)
    _attach_bill_relations([bill])
    return bill


# ---------------------------------------------------------------------------
# Bill update webhook (testing receiver)
# ---------------------------------------------------------------------------


def _receipt(log, *, duplicate=False):
    return {
        "update_id": log.update_id,
        "event": log.event,
        "received_at": log.created_at,
        "duplicate": duplicate,
        "public_status": (log.payload.get("bill") or {}).get("public_status", ""),
    }


def _rejection_receipt(log, *, duplicate=False):
    return {
        "rejection_id": log.update_id,
        "update_id": log.payload.get("update_id"),
        "received_at": log.created_at,
        "duplicate": duplicate,
    }


@bill_updates_router.post(
    "/",
    response={200: BillUpdateReceiptOut, 404: ErrorOut},
    summary="Receive a Bill update (testing stand-in for the external API)",
)
def receive_bill_update(request, payload: BillUpdateIn):
    """
    Accept one ``NEW`` / ``EDITS`` / ``STATUS_CHANGE`` update and keep it.

    This is the testing stand-in for the partner system's endpoint. It never
    mutates the Bill: it checks that the referenced bill still exists, stores
    the exact body as an inbound ``BillUpdateLog`` row and returns a receipt.
    Idempotent on ``update_id`` — a retried outbound post returns the original
    receipt instead of writing a second row.
    """
    existing = BillUpdateLog.objects.filter(
        update_id=payload.update_id, direction=BillUpdateLog.DIRECTION_INBOUND
    ).first()
    if existing is not None:
        return 200, _receipt(existing, duplicate=True)

    body = payload.model_dump(mode="json")
    bill = None
    if payload.bill.public_id is not None:
        bill = Bill.objects.filter(public_id=payload.bill.public_id).first()

    if bill is None:
        # Rejections are evidence too: keep why the post was refused.
        try:
            BillUpdateLog.objects.create(
                update_id=payload.update_id,
                bill_public_id=payload.bill.public_id,
                bill_number=payload.bill.bill_number,
                event=payload.event,
                direction=BillUpdateLog.DIRECTION_INBOUND,
                status=BillUpdateLog.STATUS_REJECTED,
                payload=body,
                target_url=request.path[:2048],
                error="Unknown bill public_id.",
            )
        except IntegrityError:
            existing = BillUpdateLog.objects.filter(
                update_id=payload.update_id,
                direction=BillUpdateLog.DIRECTION_INBOUND,
            ).first()
            return 200, _receipt(existing, duplicate=True)
        raise HttpError(404, "Unknown bill for this update.")

    try:
        log = BillUpdateLog.objects.create(
            update_id=payload.update_id,
            bill=bill,
            bill_public_id=bill.public_id,
            bill_number=bill.bill_number,
            event=payload.event,
            direction=BillUpdateLog.DIRECTION_INBOUND,
            status=BillUpdateLog.STATUS_RECEIVED,
            payload=body,
            target_url=request.path[:2048],
        )
    except IntegrityError:
        # A concurrent retry won the race; answer with its receipt.
        existing = BillUpdateLog.objects.filter(
            update_id=payload.update_id,
            direction=BillUpdateLog.DIRECTION_INBOUND,
        ).first()
        return 200, _receipt(existing, duplicate=True)

    return 200, _receipt(log)


@bill_updates_router.post(
    "/rejections/",
    response={200: BillUpdateRejectionReceiptOut},
    summary="Receive a rejection decision from the consuming API",
)
def receive_bill_update_rejection(request, payload: BillUpdateRejectionIn):
    """
    Log one update's rejection, posted back by the consuming API.

    This is the other half of the bi-directional exchange: the receiver decides
    an update is wrong and tells the sender, which records the outcome on the
    message it originally sent. The same shared token authenticates the call,
    and ``rejection_id`` makes a retried callback idempotent. The rejection is
    logged even when the original message is no longer present.
    """
    existing = BillUpdateLog.objects.filter(
        update_id=payload.rejection_id,
        direction=BillUpdateLog.DIRECTION_INBOUND,
        event=BillUpdateLog.EVENT_REJECTION,
    ).first()
    if existing is not None:
        return 200, _rejection_receipt(existing, duplicate=True)

    body = payload.model_dump(mode="json")
    bill = None
    if payload.bill is not None and payload.bill.public_id is not None:
        bill = Bill.objects.filter(public_id=payload.bill.public_id).first()

    rejected_by_name = ""
    if payload.rejected_by is not None:
        rejected_by_name = (
            payload.rejected_by.display_name or payload.rejected_by.username
        )
    rejected_at = payload.rejected_at or timezone.now()

    try:
        log = BillUpdateLog.objects.create(
            update_id=payload.rejection_id,
            bill=bill,
            bill_public_id=payload.bill.public_id if payload.bill else None,
            bill_number=payload.bill.bill_number if payload.bill else "",
            event=BillUpdateLog.EVENT_REJECTION,
            direction=BillUpdateLog.DIRECTION_INBOUND,
            status=BillUpdateLog.STATUS_RECEIVED,
            payload=body,
            target_url=request.path[:2048],
            rejection_reason=payload.reason,
            rejected_at=rejected_at,
            rejected_by_name=rejected_by_name,
        )
    except IntegrityError:
        # A concurrent retry won the race; answer with its receipt.
        existing = BillUpdateLog.objects.filter(
            update_id=payload.rejection_id,
            direction=BillUpdateLog.DIRECTION_INBOUND,
        ).first()
        return 200, _rejection_receipt(existing, duplicate=True)

    # Stamp the sender's original message, so its log answers "was this
    # accepted?" without joining the two sides.
    original = BillUpdateLog.objects.filter(
        update_id=payload.update_id,
        direction=BillUpdateLog.DIRECTION_OUTBOUND,
    ).first()
    if original is not None:
        original.rejection_reason = payload.reason
        original.rejected_at = rejected_at
        original.rejected_by_name = rejected_by_name
        original.save(
            update_fields=[
                "rejection_reason",
                "rejected_at",
                "rejected_by_name",
                "updated_at",
            ]
        )

    return 200, _rejection_receipt(log)


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# URL wiring
# ---------------------------------------------------------------------------

api.add_router("/auth", auth_router)
api.add_router("/users", users_router)
api.add_router("/groups", groups_router)
api.add_router("/memberships", memberships_router)
api.add_router("/bills", bills_router)
api.add_router("/bill-updates", bill_updates_router)
api.add_router("/meetings", meetings_router)
