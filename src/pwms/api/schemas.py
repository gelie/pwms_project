"""Pydantic schemas for the external API's request and response bodies.

These are deliberately narrower than the internal DRF serializers they replace:

* no identity numbers, dates of birth, gender, bio/avatar or supervisor chain;
* no free-text internal notes on memberships;
* bills expose their public status and the fields the website publishes.

Output schemas use ``from_attributes`` so Ninja validates ORM objects directly.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from ninja import Schema
from pydantic import ConfigDict, Field


class ErrorOut(Schema):
    detail: str


# ---------------------------------------------------------------------------
# Embedded references
# ---------------------------------------------------------------------------


class GroupRef(Schema):
    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    name: str
    short_name: str
    group_type: str


class RoleRef(Schema):
    model_config = ConfigDict(from_attributes=True)

    name: str
    slug: str


class UserRef(Schema):
    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    username: str
    display_name: str
    email: str


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class UserOut(Schema):
    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    username: str
    title: str
    first_name: str
    middle_name: str
    last_name: str
    display_name: str
    email: str
    employee_type: str
    positiondesc: str
    department: GroupRef | None = None
    supervisor: UserRef | None = None
    is_active: bool
    is_mp: bool
    is_staff_member: bool
    constituency: str
    party_affiliation: str
    phone: str


class UserPage(Schema):
    count: int
    limit: int
    offset: int
    next: str | None = None
    previous: str | None = None
    results: list[UserOut]


# ---------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------


class MembershipOut(Schema):
    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    user: UserRef
    group: GroupRef
    role: RoleRef
    start_date: date
    end_date: date | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MembershipPage(Schema):
    count: int
    limit: int
    offset: int
    next: str | None = None
    previous: str | None = None
    results: list[MembershipOut]


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


class GroupMemberOut(Schema):
    """One membership nested inside a group."""

    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    user: UserRef
    role: RoleRef
    start_date: date
    end_date: date | None = None
    is_active: bool


class GroupOut(Schema):
    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    name: str
    slug: str
    short_name: str
    group_type: str
    parent: GroupRef | None = None
    full_path: str
    description: str
    start_date: date | None = None
    end_date: date | None = None
    is_active: bool
    contact_email: str
    contact_phone: str
    location: str
    memberships: list[GroupMemberOut] = Field(
        default_factory=list, validation_alias="api_memberships"
    )


class GroupPage(Schema):
    count: int
    limit: int
    offset: int
    next: str | None = None
    previous: str | None = None
    results: list[GroupOut]


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------


class BillVersionOut(Schema):
    """One preserved version in a bill's version history (BRS §15A)."""

    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    version_label: str
    version_type: str
    version_date: date
    document_url: str
    notes: str
    recorded_by: UserRef | None = None
    is_current: bool
    created_at: datetime
    updated_at: datetime


class AttachmentVersionOut(Schema):
    """One mirrored SharePoint version of an attachment."""

    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    version_id: str
    size: int | None = None
    modified_at: datetime | None = None
    modified_by: str
    is_current: bool
    created_at: datetime


class AttachmentOut(Schema):
    """A SharePoint document attached to a bill, with its version mirror."""

    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    name: str
    type: str
    mimetype: str | None = None
    size: int | None = None
    sharepoint_web_url: str | None = None
    download_url: str | None = None
    sharepoint_folder_path: str
    uploaded_by: UserRef | None = None
    created_at: datetime
    updated_at: datetime
    versions: list[AttachmentVersionOut] = Field(default_factory=list)


class BillNoteOut(Schema):
    """One dated note recorded against a bill."""

    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    author: UserRef | None = None
    body: str
    created_at: datetime
    updated_at: datetime


class BillReferralOut(Schema):
    """A bill referred to a committee or group, with its answer."""

    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    referred_to: GroupRef
    referred_by: UserRef | None = None
    referred_at: datetime
    due_date: datetime | None = None
    status: str
    is_open: bool
    is_overdue: bool
    responded_at: datetime | None = None
    responded_by: UserRef | None = None
    response_document_url: str
    response_notes: str
    recalled_at: datetime | None = None
    recalled_by: UserRef | None = None
    recall_reason: str
    notes: str
    created_at: datetime
    updated_at: datetime


class BillOut(Schema):
    model_config = ConfigDict(from_attributes=True)

    public_id: UUID
    bill_number: str
    title: str
    short_title: str
    description: str
    bill_type: str
    house_of_origin: str
    sponsor: UserRef | None = None
    sponsor_name: str
    introduced_date: date | None = None
    responsible_committee: GroupRef | None = None
    # atc_reference: str
    # order_paper_reference: str
    # The Bill model's citizen-facing property, published to partners as bill_stage.
    bill_stage: str = Field(validation_alias="public_status")
    bill_stage_date: date
    # Annotated on the API bill queryset (TruncDate of the latest transition,
    # falling back to created_at): the date the bill entered its current stage.
    # Version history fields derived on the model (BRS §15A); the API queryset
    # prefetches the versions so they cost nothing extra per page.
    introduced_version: str
    introduced_version_date: date | None = None
    current_version: str
    current_version_date: date | None = None
    # The full preserved version history and the SharePoint documents filed
    # against the bill, both prefetched by the API bill queryset, plus the note
    # log and referrals bulk-loaded for each page.
    versions: list[BillVersionOut] = Field(default_factory=list)
    attachments: list[AttachmentOut] = Field(default_factory=list)
    notes: list[BillNoteOut] = Field(default_factory=list, validation_alias="api_notes")
    referrals: list[BillReferralOut] = Field(
        default_factory=list, validation_alias="api_referrals"
    )
    bill_document_url: str
    identifier: str
    is_overdue: bool
    deadline: datetime | None = None
    priority: str
    created_at: datetime
    updated_at: datetime


class BillPage(Schema):
    count: int
    limit: int
    offset: int
    next: str | None = None
    previous: str | None = None
    results: list[BillOut]


# ---------------------------------------------------------------------------
# Cross-resource search
# ---------------------------------------------------------------------------


class SearchOut(Schema):
    query: str
    resources: list[str]
    skipped: list[str]
    users: UserPage
    groups: GroupPage
    memberships: MembershipPage
    bills: BillPage


# ---------------------------------------------------------------------------
# Authentication and discovery
# ---------------------------------------------------------------------------


class TokenRequest(Schema):
    client_id: str
    client_secret: str
    grant_type: str = "client_credentials"


class RefreshRequest(Schema):
    refresh_token: str


class RevokeRequest(Schema):
    refresh_token: str


class TokenPairOut(Schema):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_expires_in: int
    scope: str


class ClientInfoOut(Schema):
    name: str
    client_id: str
    description: str
    contact_name: str
    contact_email: str
    scopes: list[str]
    rate_limit_per_minute: int
    last_used_at: datetime | None = None


class ResourceLink(Schema):
    name: str
    url: str


class RootOut(Schema):
    api: str
    version: str
    client: str
    scopes: list[str]
    documentation: str
    resources: list[ResourceLink]
