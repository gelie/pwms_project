"""Search helpers shared by the list and cross-resource search endpoints.

Each resource maps a *friendly field name* to one or more ORM lookups. A search
term is OR-ed across every selected lookup, so ``?search=alice`` can match a
username, an email, a first name, a group name or a bill title depending on the
resource. ``search_fields`` narrows that to an allow-listed subset; unknown
names are rejected rather than silently ignored, so a typo does not quietly
return the wrong rows.
"""

from __future__ import annotations

from uuid import UUID

from django.db.models import Q
from ninja.errors import HttpError

#: Users: what ``?search=`` matches by default, and what ``?search_fields=`` may
#: select.
USER_SEARCH_FIELDS: dict[str, tuple[str, ...]] = {
    "username": ("username",),
    "first_name": ("first_name",),
    "middle_name": ("middle_name",),
    "last_name": ("last_name",),
    "email": ("email",),
    "positiondesc": ("positiondesc",),
    "department": ("department__name",),
    "constituency": ("constituency",),
    "party_affiliation": ("party_affiliation",),
    "phone": ("phone",),
}

#: Memberships: match the person, the group and the role independently.
MEMBERSHIP_SEARCH_FIELDS: dict[str, tuple[str, ...]] = {
    "user_username": ("user__username",),
    "user_first_name": ("user__first_name",),
    "user_middle_name": ("user__middle_name",),
    "user_last_name": ("user__last_name",),
    "user_email": ("user__email",),
    "group_name": ("group__name",),
    "group_short_name": ("group__short_name",),
    "group_slug": ("group__slug",),
    "role_name": ("role__name",),
    "role_slug": ("role__slug",),
}

#: Bills: bibliographic fields, sponsor, committee and the public status.
BILL_SEARCH_FIELDS: dict[str, tuple[str, ...]] = {
    "bill_number": ("bill_number",),
    "title": ("title",),
    "short_title": ("short_title",),
    "description": ("description",),
    "sponsor_name": (
        "sponsor_name",
        "sponsor__username",
        "sponsor__first_name",
        "sponsor__last_name",
    ),
    "responsible_committee": ("responsible_committee__name",),
    "atc_reference": ("atc_reference",),
    "order_paper_reference": ("order_paper_reference",),
    "public_status": (
        "current_state__public_status__name",
        "current_state__public_status__slug",
    ),
}


def _allowed_fields_text(field_map: dict[str, tuple[str, ...]]) -> str:
    return ", ".join(sorted(field_map))


def resolve_search_fields(
    field_map: dict[str, tuple[str, ...]], raw_fields: str | None
) -> list[str]:
    """Return the ORM lookups selected by ``search_fields``.

    With no selector, every field in the map is used. Unknown field names raise
    ``400`` and list the allowed names.
    """
    if not raw_fields:
        return [lookup for lookups in field_map.values() for lookup in lookups]

    names = [name.strip() for name in raw_fields.split(",") if name.strip()]
    if not names:
        return [lookup for lookups in field_map.values() for lookup in lookups]

    unknown = sorted({name for name in names if name not in field_map})
    if unknown:
        raise HttpError(
            400,
            f"Unknown search field(s): {', '.join(unknown)}. "
            f"Allowed: {_allowed_fields_text(field_map)}.",
        )

    # Deduplicate while preserving the caller's order.
    return [lookup for name in dict.fromkeys(names) for lookup in field_map[name]]


def apply_search(
    queryset,
    term: str,
    field_map: dict[str, tuple[str, ...]],
    raw_fields: str | None = None,
):
    """Filter ``queryset`` by ``term`` across one or more allow-listed fields.

    Returns the queryset unchanged for an empty/whitespace term, but still
    validates ``search_fields`` so a bad selector is reported on every call.
    """
    lookups = resolve_search_fields(field_map, raw_fields)
    term = (term or "").strip()
    if not term:
        return queryset

    condition = Q()
    for lookup in lookups:
        condition |= Q(**{f"{lookup}__icontains": term})
    return queryset.filter(condition)


def related_identifier_q(prefix: str, value: str, *, text_fields: tuple[str, ...]) -> Q:
    """Match a related object by ``public_id`` **or** a human-readable field.

    Used by filters such as ``?department=``, ``?user=``, ``?group=`` and
    ``?responsible_committee=`` so an integration can pass the id it stored or
    the name a user typed, without needing two parameters.
    """
    condition = Q()
    for field in text_fields:
        condition |= Q(**{f"{prefix}__{field}__iexact": value})

    try:
        public_id = UUID(str(value))
    except AttributeError, TypeError, ValueError:
        pass
    else:
        condition |= Q(**{f"{prefix}__public_id": public_id})

    return condition


def group_filter_q(prefix: str, value: str) -> Q:
    """Group filter accepting ``public_id``, name, short name or slug."""
    return related_identifier_q(
        prefix, value, text_fields=("name", "short_name", "slug")
    )


def user_filter_q(prefix: str, value: str) -> Q:
    """User filter accepting ``public_id``, username or email."""
    return related_identifier_q(prefix, value, text_fields=("username", "email"))
