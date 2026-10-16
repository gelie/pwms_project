"""Site-wide search.

One query, several registers. A *source* is a model plus the handful of
accessors that turn one row into a result: the identifier to match and show on
the right, the title, the free text worth matching, and the line of context
under the title. Adding a register to the global search is therefore adding one
entry to :data:`SOURCES` — no view or template change.

Two rules keep a result set honest and useful:

* **Permission first.** Workflow instances are narrowed through
  :func:`~pwms.services.permissions.visible_instances` — the same chain the
  detail pages use — *before* any text is matched, so a hit can never be a
  record whose own page would raise 403. Groups and people are readable by any
  signed-in user (the resolver's conservative default), so they need no such
  pass.
* **Relevance by field, not by model.** Ranking reflects *where* the query
  landed — an identifier typed in full beats a passing mention buried in a
  description — so hits of different kinds can be ordered on one scale and the
  likeliest match leads each category.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.db.models import QuerySet
from django.urls import reverse

from ..models import (
    Bill,
    DelegationReport,
    Group,
    InternationalAgreement,
    InternationalResolution,
    Meeting,
    User,
)
from .permissions import visible_instances

#: How many hits each category offers before the "see all results" link takes
#: over. Small on purpose: the panel is a jump-off point, not a results page.
DEFAULT_PER_CATEGORY = 5


@dataclass(frozen=True)
class SearchHit:
    """One result, ready to render as a link."""

    title: str
    url: str
    meta: str = ""
    badge: str = ""
    score: int = 0


@dataclass
class SearchCategory:
    """The hits from one register, with the label and icon for its heading."""

    key: str
    label: str
    icon: str
    hits: list[SearchHit] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.hits)


@dataclass(frozen=True)
class SearchSource:
    """A register the global search reads, and how to read one row from it.

    ``workflow`` selects the permission pass: workflow instances (and meetings,
    which are one) go through :func:`visible_instances`; anything else is read
    as-is.
    """

    key: str
    label: str
    icon: str
    queryset: Callable[[], QuerySet]
    identifier: Callable[[Any], str]
    title: Callable[[Any], str]
    body: Callable[[Any], str]
    meta: Callable[[Any], str]
    url: Callable[[Any], str]
    workflow: bool = True


def _title(instance: Any) -> str:
    """The record's title, falling back to ``str()`` (a meeting may carry none)."""
    return (getattr(instance, "title", "") or "").strip() or str(instance)


def _join(*parts) -> str:
    """Join the non-empty parts with a middot, for the context line."""
    return " · ".join(str(part).strip() for part in parts if part and str(part).strip())


def _workflow_queryset(model, *related) -> QuerySet:
    """A workflow model's rows with the relations a result row reads prefetched."""
    return model.objects.select_related("workflow_type", "current_state", *related)


def _delegation_meta(report: Any) -> str:
    location = report.location_city or report.location_country
    return _join(report.engagement_name, location)


def _agreement_meta(agreement: Any) -> str:
    return _join(
        agreement.submitting_department, agreement.get_agreement_type_display()
    )


def _resolution_meta(resolution: Any) -> str:
    return _join(resolution.responsible_group)


def _bill_meta(bill: Any) -> str:
    return _join(bill.short_title, bill.get_bill_type_display())


def _meeting_meta(meeting: Any) -> str:
    when = meeting.start_datetime
    when = when.strftime("%d %b %Y %H:%M") if when else ""
    return _join(meeting.get_meeting_type_display(), meeting.venue_display, when)


def _person_meta(person: Any) -> str:
    department = person.department.name if person.department_id else ""
    return _join(person.positiondesc, department)


#: The registers the search reads, in the order their categories appear. Each
#: entry names the fields to match and the line to show under each hit.
SOURCES: tuple[SearchSource, ...] = (
    SearchSource(
        key="delegation-reports",
        label="Delegation reports",
        icon="plane",
        queryset=lambda: _workflow_queryset(
            DelegationReport, "location_country", "location_city"
        ),
        identifier=lambda report: report.reference_number or "",
        title=_title,
        body=lambda report: "\n".join(
            (report.engagement_name or "", report.description or "", report.notes or "")
        ),
        meta=_delegation_meta,
        url=lambda report: report.get_absolute_url(),
    ),
    SearchSource(
        key="international-resolutions",
        label="International resolutions",
        icon="scroll-text",
        queryset=lambda: _workflow_queryset(
            InternationalResolution, "responsible_group"
        ),
        identifier=lambda resolution: resolution.resolution_number or "",
        title=_title,
        body=lambda resolution: "\n".join(
            (resolution.resolution_text or "", resolution.implementation_progress or "")
        ),
        meta=_resolution_meta,
        url=lambda resolution: resolution.get_absolute_url(),
    ),
    SearchSource(
        key="international-agreements",
        label="International agreements",
        icon="handshake",
        queryset=lambda: _workflow_queryset(InternationalAgreement),
        identifier=lambda agreement: agreement.reference_number or "",
        title=_title,
        body=lambda agreement: "\n".join(
            (
                agreement.submitting_department or "",
                agreement.notes or "",
                agreement.atc_reference or "",
            )
        ),
        meta=_agreement_meta,
        url=lambda agreement: agreement.get_absolute_url(),
    ),
    SearchSource(
        key="bills",
        label="Bills",
        icon="gavel",
        queryset=lambda: _workflow_queryset(Bill, "responsible_committee"),
        identifier=lambda bill: bill.bill_number or "",
        title=_title,
        body=lambda bill: "\n".join(
            (
                bill.short_title or "",
                bill.notes or "",
                bill.atc_reference or "",
                bill.order_paper_reference or "",
            )
        ),
        meta=_bill_meta,
        url=lambda bill: bill.get_absolute_url(),
    ),
    SearchSource(
        key="meetings",
        label="Meetings",
        icon="calendar-days",
        queryset=lambda: _workflow_queryset(
            Meeting, "host_group", "venue__building", "chaired_by"
        ),
        identifier=lambda meeting: meeting.reference_number or "",
        title=_title,
        body=lambda meeting: "\n".join(
            (
                meeting.off_site_venue or "",
                meeting.off_site_address or "",
                meeting.agenda_summary or "",
                meeting.notes or "",
            )
        ),
        meta=_meeting_meta,
        url=lambda meeting: meeting.get_absolute_url(),
    ),
    SearchSource(
        key="groups",
        label="Groups",
        icon="users",
        queryset=lambda: Group.objects.select_related("parent"),
        identifier=lambda group: group.short_name or "",
        title=lambda group: group.name,
        body=lambda group: "\n".join((group.description or "", group.location or "")),
        meta=lambda group: group.get_full_path(),
        url=lambda group: reverse("pwms:group_detail", kwargs={"pk": group.pk}),
        workflow=False,
    ),
    SearchSource(
        key="people",
        label="People",
        icon="circle-user",
        # Active accounts only: a former office holder's profile still opens from
        # a record that names them, but the typeahead should not offer departed
        # staff. Consistent with the assignment pickers (``user_search``).
        queryset=lambda: User.objects.filter(is_active=True).select_related(
            "department"
        ),
        identifier=lambda person: person.username,
        title=lambda person: person.display_name,
        body=lambda person: "\n".join(
            (
                person.positiondesc or "",
                person.department.name if person.department_id else "",
                person.party_affiliation or "",
                person.constituency or "",
                person.bio or "",
            )
        ),
        meta=_person_meta,
        url=lambda person: person.get_absolute_url(),
        workflow=False,
    ),
)


def _score(query: str, *, identifier: str = "", title: str = "", body: str = "") -> int:
    """How well one record matches ``query``; ``0`` means no match at all.

    The tiers, best first: an identifier that *equals* the query (someone typing
    a bill number wants that bill), then identifier / title prefixes, then the
    same as substrings, then a free-text body hit. When no single field holds the
    whole query, an all-tokens match is accepted at the lowest tier, so
    "delegation report" still finds a delegation report.
    """
    q = query.casefold().strip()
    if not q:
        return 0
    identifier = (identifier or "").casefold()
    title = (title or "").casefold()
    body = (body or "").casefold()

    if identifier and identifier == q:
        return 100

    score = 0
    if identifier.startswith(q):
        score = max(score, 90)
    elif identifier and q in identifier:
        score = max(score, 75)
    if title.startswith(q):
        score = max(score, 85)
    elif title and q in title:
        score = max(score, 65)
    if body and q in body:
        score = max(score, 40)
    if score:
        return score

    tokens = q.split()
    if len(tokens) > 1:
        haystack = f"{identifier} {title} {body}"
        if all(token in haystack for token in tokens):
            return 25
    return 0


def _hits_for(user, source: SearchSource, query: str, limit: int) -> list[SearchHit]:
    """The best ``limit`` hits for ``query`` from one register."""
    queryset = source.queryset()
    candidates = visible_instances(user, queryset) if source.workflow else queryset

    hits = []
    for instance in candidates:
        identifier = source.identifier(instance)
        score = _score(
            query,
            identifier=identifier,
            title=source.title(instance),
            body=source.body(instance),
        )
        if not score:
            continue
        hits.append(
            SearchHit(
                title=source.title(instance),
                url=source.url(instance),
                meta=source.meta(instance),
                badge=identifier,
                score=score,
            )
        )

    # Best match first; ties broken by title so the order is stable, not
    # database-dependent.
    hits.sort(key=lambda hit: (-hit.score, hit.title.casefold()))
    return hits[:limit]


def search_site(
    user, query: str, *, per_category: int = DEFAULT_PER_CATEGORY
) -> list[SearchCategory]:
    """Categorized hits for ``query`` across every register ``user`` may read.

    Returns one :class:`SearchCategory` per register that matched, in
    :data:`SOURCES` order, each ranked best-first. An empty query — or an
    anonymous user — yields no categories at all.
    """
    query = (query or "").strip()
    if not query or not getattr(user, "is_authenticated", False):
        return []

    categories = []
    for source in SOURCES:
        hits = _hits_for(user, source, query, per_category)
        if hits:
            categories.append(
                SearchCategory(source.key, source.label, source.icon, hits)
            )
    return categories


def count_hits(categories: list[SearchCategory]) -> int:
    """The total number of hits across ``categories``."""
    return sum(category.count for category in categories)
