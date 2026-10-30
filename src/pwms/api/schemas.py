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
from pydantic import ConfigDict


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
# Bills
# ---------------------------------------------------------------------------


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
    atc_reference: str
    order_paper_reference: str
    bill_document_url: str
    public_status: str
    current_version: str
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
