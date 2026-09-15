"""Build a report from a filter set.

A report is a read-only projection of the workflow registers. The same
permission chain the lists and detail pages use (:func:`visible_instances`)
decides which instances are counted, so a report can never show a figure the
reader could not open — an export or a shared link cannot widen access.

``ReportFilters`` parses a request (or a stored share's JSON) into a validated
filter set; :func:`build_report` applies it and returns a :class:`ReportData`
holding everything the page, the preview, the exports and a shared link need.

Filtering happens in Python over the accessible set rather than in SQL. The
permission chain is evaluated per object, so that set has to be materialised
anyway, and doing the rest in Python keeps one code path for every consumer
instead of one dialect per backend.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import timezone

from ..models import (
    AbstractLegislativeWorkflow,
    Bill,
    DelegationReport,
    InternationalAgreement,
    InternationalResolution,
    TransitionLog,
    WorkflowEvent,
    WorkflowReferral,
)
from ..services.permissions import visible_instances

#: Concrete workflow models a report spans, in display order.
REPORT_MODELS = (
    DelegationReport,
    InternationalResolution,
    InternationalAgreement,
    Bill,
)

#: Extra, type-specific text the free-text filter also matches. The common
#: fields (title, reference, description, owner, type, state) are searched for
#: every model; these add the fields only one instrument carries, so a search in
#: a report behaves like a search on that instrument.
SEARCH_FIELDS = {
    DelegationReport: ("engagement_name", "notes"),
    InternationalResolution: (
        "resolution_number",
        "resolution_text",
        "implementation_progress",
    ),
    InternationalAgreement: (
        "reference_number",
        "submitting_department",
        "responsible_minister_name",
        "notes",
    ),
    Bill: ("bill_number", "short_title", "sponsor_name", "notes"),
}

#: Workflows falling due within this many days count as "due soon".
DUE_SOON_DAYS = 7

#: Activity rows read from each of ``TransitionLog`` / ``WorkflowEvent``.
ACTIVITY_LIMIT = 300

#: Workflow rows the on-screen preview shows before it defers to an export.
PREVIEW_ROW_LIMIT = 100

REPORT_TYPE_CHOICES = (
    ("overview", "Overview"),
    ("workflows", "Workflow register"),
    ("activity", "Activity & audit trail"),
    ("referrals", "Referrals"),
)

PERIOD_CHOICES = (
    ("", "All time"),
    ("weekly", "Last 7 days"),
    ("monthly", "This month"),
    ("quarterly", "This quarter"),
    ("yearly", "This year"),
)

DATE_FIELD_CHOICES = (
    ("created_at", "Created"),
    ("updated_at", "Last updated"),
    ("deadline", "Deadline"),
)

SORT_CHOICES = (
    ("-created_at", "Newest first"),
    ("created_at", "Oldest first"),
    ("deadline", "Deadline (soonest first)"),
    ("priority", "Priority (highest first)"),
    ("title", "Title (A–Z)"),
)

#: Relative order of priorities, so "priority" sorting is not alphabetical.
PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

#: Event origins that are not a signed-in person.
_SYSTEM_ORIGINS = {"system", "integration"}


def priority_choices():
    """The priority choices declared on the workflow base, as ``(value, label)``."""
    return AbstractLegislativeWorkflow._meta.get_field("priority").choices


# -- filters ----------------------------------------------------------------
def _as_int(value):
    """``int(value)`` or ``None`` — never raises on a hand-edited query string."""
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _as_bool(value):
    return str(value).lower() in {"1", "true", "on", "yes"}


def _as_choice(value, choices, default=""):
    allowed = {choice for choice, _label in choices}
    return value if value in allowed else default


def _as_date(value):
    """Parse an ISO date, returning ``None`` when it is absent or malformed."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass
class ReportFilters:
    """A validated, round-trippable report filter set.

    Values are stored in the shapes a query string and ``JSONField`` both
    handle, so a share can be stored as a plain dict and reopened unchanged.
    """

    report_type: str = "overview"
    q: str = ""
    type_id: int | None = None
    state_id: int | None = None
    priority: str = ""
    owner_id: int | None = None
    assigned_to_id: int | None = None
    group_id: int | None = None
    overdue_only: bool = False
    due_soon: bool = False
    unassigned_only: bool = False
    period: str = ""
    date_field: str = "created_at"
    date_from: str = ""
    date_to: str = ""
    sort: str = "-created_at"

    @classmethod
    def from_mapping(cls, data):
        """Build filters from a request ``GET`` or a stored share's dict."""
        get = data.get if hasattr(data, "get") else (lambda key, default="": default)
        return cls(
            report_type=_as_choice(
                get("report_type", ""), REPORT_TYPE_CHOICES, "overview"
            ),
            q=str(get("q", "") or "").strip(),
            type_id=_as_int(get("type")),
            state_id=_as_int(get("state")),
            priority=_as_choice(str(get("priority", "") or ""), priority_choices(), ""),
            owner_id=_as_int(get("owner")),
            assigned_to_id=_as_int(get("assigned_to")),
            group_id=_as_int(get("group")),
            overdue_only=_as_bool(get("overdue_only", "")),
            due_soon=_as_bool(get("due_soon", "")),
            unassigned_only=_as_bool(get("unassigned_only", "")),
            period=_as_choice(get("period", ""), PERIOD_CHOICES, ""),
            date_field=_as_choice(
                get("date_field", ""), DATE_FIELD_CHOICES, "created_at"
            ),
            date_from=str(get("date_from", "") or "").strip(),
            date_to=str(get("date_to", "") or "").strip(),
            sort=_as_choice(get("sort", ""), SORT_CHOICES, "-created_at"),
        )

    @classmethod
    def from_request(cls, request):
        return cls.from_mapping(request.GET)

    @classmethod
    def from_dict(cls, data):
        return cls.from_mapping(data or {})

    def to_dict(self):
        return {
            "report_type": self.report_type,
            "q": self.q,
            "type": self.type_id,
            "state": self.state_id,
            "priority": self.priority,
            "owner": self.owner_id,
            "assigned_to": self.assigned_to_id,
            "group": self.group_id,
            "overdue_only": self.overdue_only,
            "due_soon": self.due_soon,
            "unassigned_only": self.unassigned_only,
            "period": self.period,
            "date_field": self.date_field,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "sort": self.sort,
        }

    @property
    def is_filtered(self):
        """True when anything beyond the report-type/sort defaults is applied."""
        return any(
            [
                self.q,
                self.type_id,
                self.state_id,
                self.priority,
                self.owner_id,
                self.assigned_to_id,
                self.group_id,
                self.overdue_only,
                self.due_soon,
                self.unassigned_only,
                self.period,
                self.date_from,
                self.date_to,
            ]
        )

    def window(self, now):
        """
        ``(start, end)`` aware datetimes bounding the report.

        An explicit ``date_from`` beats the period preset; ``date_to`` adds a
        closed upper bound. Either end may be ``None``.
        """
        start = None
        if self.period:
            start = _period_start(self.period, now)

        explicit_from = _as_date(self.date_from)
        if explicit_from is not None:
            start = _aware_datetime(explicit_from, time.min)

        end = None
        explicit_to = _as_date(self.date_to)
        if explicit_to is not None:
            end = _aware_datetime(explicit_to, time.max)

        return start, end


def _period_start(period, now):
    """Start of the named period, relative to ``now`` (aware)."""
    if period == "weekly":
        return now - timedelta(days=7)
    if period == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "quarterly":
        first_month = ((now.month - 1) // 3) * 3 + 1
        return now.replace(
            month=first_month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    if period == "yearly":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def _aware_datetime(day, at):
    """Combine a date and a time into an aware datetime in the current zone."""
    return timezone.make_aware(
        datetime.combine(day, at), timezone.get_current_timezone()
    )


# -- the report itself ------------------------------------------------------
@dataclass
class ReportData:
    """Everything a report's page, preview, exports and share link render."""

    filters: ReportFilters
    user: object
    generated_on: datetime
    title: str = ""
    rows: list = field(default_factory=list)
    total: int = 0
    summary: dict = field(default_factory=dict)
    by_type: list = field(default_factory=list)
    by_state: list = field(default_factory=list)
    by_priority: list = field(default_factory=list)
    by_public_status: list = field(default_factory=list)
    by_owner: list = field(default_factory=list)
    by_group: list = field(default_factory=list)
    transitions: list = field(default_factory=list)
    events: list = field(default_factory=list)
    activity: list = field(default_factory=list)
    referrals: list = field(default_factory=list)
    filter_summary: list = field(default_factory=list)
    options: dict = field(default_factory=dict)

    @property
    def preview_rows(self):
        """The rows the on-screen preview shows (the rest live in an export)."""
        return self.rows[:PREVIEW_ROW_LIMIT]

    @property
    def has_more_than_preview(self):
        return self.total > PREVIEW_ROW_LIMIT

    @property
    def generated_by_label(self):
        if self.user is None:
            return "—"
        return getattr(self.user, "display_name", None) or str(self.user)

    @property
    def is_filtered(self):
        return self.filters.is_filtered


def accessible_instances(user):
    """
    Every workflow instance ``user`` may view, across all concrete models.

    One permission pass, exactly as the unified register and dashboard do.
    """
    instances = []
    for model in REPORT_MODELS:
        instances.extend(
            visible_instances(
                user,
                model.objects.select_related(
                    "workflow_type",
                    "workflow_type__group",
                    "current_state",
                    "owner",
                    "assigned_to",
                ),
            )
        )
    return instances


def build_report(user, filters, *, now=None):
    """Apply ``filters`` to the instances ``user`` may view."""
    now = now or timezone.now()
    accessible = accessible_instances(user)
    options = _build_options(accessible)

    selected = _apply_filters(accessible, filters, now)
    selected = _sort_instances(selected, filters.sort, now)

    rows = [_describe(instance) for instance in selected]
    by_key = _key_map(selected)

    transitions = _transition_rows(by_key)
    events = _event_rows(by_key)
    activity = sorted(
        transitions + events, key=lambda row: row["timestamp"], reverse=True
    )[:ACTIVITY_LIMIT]
    referrals = _referral_rows(by_key)

    summary = _summary(rows, referrals, now)

    return ReportData(
        filters=filters,
        user=user,
        generated_on=now,
        title=_report_title(filters, options),
        rows=rows,
        total=len(rows),
        summary=summary,
        by_type=_counter_breakdown(Counter(row["type"] for row in rows), len(rows)),
        by_state=_counter_breakdown(Counter(row["state"] for row in rows), len(rows)),
        by_priority=_priority_breakdown(rows),
        by_public_status=_counter_breakdown(
            Counter(row["public_status"] for row in rows), len(rows)
        ),
        by_owner=_counter_breakdown(Counter(row["owner"] for row in rows), len(rows)),
        by_group=_counter_breakdown(Counter(row["group"] for row in rows), len(rows)),
        transitions=transitions,
        events=events,
        activity=activity,
        referrals=referrals,
        filter_summary=_filter_summary(filters, options),
        options=options,
    )


def _build_options(instances):
    """
    Filter option lists, scoped to the instances the reader may see.

    Options come from the *whole* accessible set, not the filtered one, so
    choosing a type never blanks out the other filters' choices.
    """
    types = {}
    states = {}
    groups = {}
    owners = {}
    for instance in instances:
        types[instance.workflow_type_id] = instance.workflow_type.name
        states[instance.current_state_id] = instance.current_state.name
        owner = instance.owner
        if owner is not None:
            owners[owner.pk] = owner.display_name
        group = instance.workflow_type.group
        if group is not None:
            groups[group.pk] = group.name

    def as_options(mapping):
        return [
            {"value": key, "label": label}
            for key, label in sorted(mapping.items(), key=lambda item: item[1])
        ]

    return {
        "types": as_options(types),
        "states": as_options(states),
        "groups": as_options(groups),
        "owners": as_options(owners),
        "priorities": [
            {"value": value, "label": label} for value, label in priority_choices()
        ],
    }


def _apply_filters(instances, filters, now):
    start, end = filters.window(now)
    needle = filters.q.lower()
    rows = instances

    if filters.type_id is not None:
        rows = [i for i in rows if i.workflow_type_id == filters.type_id]
    if filters.state_id is not None:
        rows = [i for i in rows if i.current_state_id == filters.state_id]
    if filters.priority:
        rows = [i for i in rows if i.priority == filters.priority]
    if filters.owner_id is not None:
        rows = [i for i in rows if i.owner_id == filters.owner_id]
    if filters.assigned_to_id is not None:
        rows = [i for i in rows if i.assigned_to_id == filters.assigned_to_id]
    if filters.group_id is not None:
        rows = [i for i in rows if i.workflow_type.group_id == filters.group_id]
    if filters.overdue_only:
        rows = [i for i in rows if i.is_overdue]
    if filters.due_soon:
        cutoff = now + timedelta(days=DUE_SOON_DAYS)
        rows = [
            i
            for i in rows
            if not i.current_state.is_terminal
            and i.deadline is not None
            and now <= i.deadline <= cutoff
        ]
    if filters.unassigned_only:
        rows = [i for i in rows if i.assigned_to_id is None]
    if start is not None or end is not None:
        rows = [
            i for i in rows if _in_window(getattr(i, filters.date_field), start, end)
        ]
    if needle:
        rows = [i for i in rows if needle in _haystack(i)]

    return rows


def _in_window(value, start, end):
    if value is None:
        return False
    if start is not None and value < start:
        return False
    return not (end is not None and value > end)


def _haystack(instance):
    """Lower-cased text the free-text filter matches against."""
    owner = instance.owner
    parts = [
        instance.title or "",
        instance.identifier or "",
        instance.description or "",
        instance.workflow_type.name or "",
        instance.current_state.name or "",
        owner.display_name if owner is not None else "",
    ]
    for field_name in SEARCH_FIELDS.get(type(instance), ()):
        value = getattr(instance, field_name, "")
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _sort_instances(instances, sort, now):
    field_name = (sort or "-created_at").lstrip("-") or "created_at"
    reverse = sort.startswith("-")

    if field_name == "deadline":
        return sorted(
            instances,
            key=lambda i: (i.deadline is None, i.deadline or now),
            reverse=reverse,
        )
    if field_name == "priority":
        return sorted(
            instances, key=lambda i: PRIORITY_RANK.get(i.priority, 99), reverse=reverse
        )
    if field_name == "title":
        return sorted(instances, key=lambda i: (i.title or "").lower(), reverse=reverse)
    if field_name == "updated_at":
        return sorted(instances, key=lambda i: i.updated_at, reverse=reverse)
    return sorted(instances, key=lambda i: i.created_at, reverse=reverse)


# -- presentation rows ------------------------------------------------------
#: Columns every workflow export shares, as ``(header, row key)``.
WORKFLOW_COLUMNS = (
    ("Reference", "reference"),
    ("Title", "title"),
    ("Type", "type"),
    ("State", "state"),
    ("Public status", "public_status"),
    ("Priority", "priority"),
    ("Owner", "owner"),
    ("Assigned to", "assigned_to"),
    ("Group", "group"),
    ("Created", "created"),
    ("Last updated", "updated"),
    ("Deadline", "deadline"),
    ("Overdue", "overdue_label"),
    ("Closed", "closed_label"),
)


def _describe(instance):
    """Display values for one instance, shared by the preview and the exports."""
    assigned = instance.assigned_to
    group = instance.workflow_type.group
    return {
        "object": instance,
        "url": instance.get_absolute_url(),
        "reference": instance.identifier or "",
        "title": instance.title,
        "type": instance.workflow_type.name,
        "state": instance.current_state.name,
        "public_status": instance.current_state.get_public_name_display(),
        "priority": instance.get_priority_display(),
        "priority_value": instance.priority,
        "owner": instance.owner.display_name if instance.owner else "",
        "assigned_to": assigned.display_name if assigned is not None else "",
        "group": group.name if group is not None else "",
        "created": instance.created_at,
        "updated": instance.updated_at,
        "deadline": instance.deadline,
        "is_overdue": instance.is_overdue,
        "is_terminal": instance.current_state.is_terminal,
        "overdue_label": "Yes" if instance.is_overdue else "No",
        "closed_label": "Yes" if instance.current_state.is_terminal else "No",
    }


def _key_map(instances):
    """``(content type id, object id) -> instance`` for the filtered set."""
    ct_ids = {
        model: ContentType.objects.get_for_model(model).pk for model in REPORT_MODELS
    }
    return {(ct_ids[type(instance)], instance.pk): instance for instance in instances}


def _target_q(by_key):
    """
    A ``Q`` matching exactly the rows in ``by_key``.

    Built per content type because object ids are only unique within one: a
    blanket ``object_id__in`` would pull in rows of other models that happen to
    share an id.
    """
    grouped = defaultdict(set)
    for content_type_id, object_id in by_key:
        grouped[content_type_id].add(object_id)

    query = Q()
    for content_type_id, object_ids in grouped.items():
        query |= Q(content_type_id=content_type_id, object_id__in=list(object_ids))
    return query


def _transition_rows(by_key):
    if not by_key:
        return []
    logs = (
        TransitionLog.objects.filter(_target_q(by_key))
        .select_related("from_state", "to_state", "actor")
        .order_by("-timestamp")[:ACTIVITY_LIMIT]
    )
    rows = []
    for log in logs:
        workflow = by_key.get((log.content_type_id, log.object_id))
        if workflow is None:
            continue
        rows.append(
            {
                "kind": "transition",
                "label": "State change",
                "timestamp": log.timestamp,
                "workflow": workflow,
                "url": workflow.get_absolute_url(),
                "actor": log.actor.display_name if log.actor else "System",
                "from_state": log.from_state.name if log.from_state else "",
                "to_state": log.to_state.name if log.to_state else "",
                "detail": log.action,
                "notes": log.notes,
            }
        )
    return rows


def _event_rows(by_key):
    if not by_key:
        return []
    events = (
        WorkflowEvent.objects.filter(_target_q(by_key))
        .select_related("event_type", "actor")
        .order_by("-occurred_at")[:ACTIVITY_LIMIT]
    )
    rows = []
    for event in events:
        workflow = by_key.get((event.content_type_id, event.object_id))
        if workflow is None:
            continue
        actor = event.actor
        rows.append(
            {
                "kind": "event",
                "label": event.event_type.name,
                "timestamp": event.occurred_at,
                "workflow": workflow,
                "url": workflow.get_absolute_url(),
                "actor": actor.display_name if actor else "System",
                "from_state": "",
                "to_state": "",
                "detail": event.get_origin_display()
                if event.origin in _SYSTEM_ORIGINS
                else "",
                "notes": event.notes,
            }
        )
    return rows


def _referral_rows(by_key):
    if not by_key:
        return []
    referrals = (
        WorkflowReferral.objects.filter(_target_q(by_key))
        .select_related("referred_to", "referred_by", "responded_by")
        .order_by("-referred_at")
    )
    rows = []
    for referral in referrals:
        workflow = by_key.get((referral.content_type_id, referral.object_id))
        if workflow is None:
            continue
        rows.append(
            {
                "referral": referral,
                "workflow": workflow,
                "url": workflow.get_absolute_url(),
                "referred_to": referral.referred_to.name,
                "referred_by": referral.referred_by.display_name
                if referral.referred_by
                else "",
                "status": referral.get_status_display(),
                "status_value": referral.status,
                "referred_at": referral.referred_at,
                "due_date": referral.due_date,
                "is_overdue": referral.is_overdue,
                "responded_at": referral.responded_at,
            }
        )
    return rows


def _summary(rows, referrals, now):
    due_soon_cutoff = now + timedelta(days=DUE_SOON_DAYS)
    active = [row for row in rows if not row["is_terminal"]]
    return {
        "total": len(rows),
        "active": len(active),
        "completed": len(rows) - len(active),
        "overdue": sum(1 for row in rows if row["is_overdue"]),
        "due_soon": sum(
            1
            for row in rows
            if not row["is_terminal"]
            and row["deadline"] is not None
            and now <= row["deadline"] <= due_soon_cutoff
        ),
        "unassigned": sum(1 for row in active if not row["assigned_to"]),
        "high_priority": sum(
            1 for row in rows if row["priority_value"] in ("urgent", "high")
        ),
        "type_count": len({row["type"] for row in rows}),
        "referrals_total": len(referrals),
        "referrals_open": sum(1 for row in referrals if row["status_value"] == "open"),
        "referrals_overdue": sum(
            1
            for row in referrals
            if row["status_value"] == "open" and row["is_overdue"]
        ),
        "due_soon_days": DUE_SOON_DAYS,
    }


def _counter_breakdown(counter, total):
    """``Counter`` -> rows carrying their share of ``total``, biggest first."""
    if not total:
        return []
    return [
        {"label": label, "count": count, "percent": round(count * 100 / total)}
        for label, count in counter.most_common()
    ]


def _priority_breakdown(rows):
    """Priorities in severity order (urgent first), dropping the empty ones."""
    if not rows:
        return []
    counts = Counter(row["priority_value"] for row in rows)
    labels = dict(priority_choices())
    ordered = sorted(
        priority_choices(), key=lambda choice: PRIORITY_RANK.get(choice[0], 99)
    )
    return [
        {
            "label": labels.get(value, value),
            "count": counts[value],
            "percent": round(counts[value] * 100 / len(rows)),
        }
        for value, _label in ordered
        if counts[value]
    ]


def _filter_summary(filters, options):
    """Human sentences describing the active filters, for display and export."""
    labels = []
    if filters.q:
        labels.append(f'Search: "{filters.q}"')

    def name_of(key, value):
        for option in options.get(key, []):
            if option["value"] == value:
                return option["label"]
        return str(value)

    if filters.type_id is not None:
        labels.append(f"Type: {name_of('types', filters.type_id)}")
    if filters.state_id is not None:
        labels.append(f"State: {name_of('states', filters.state_id)}")
    if filters.priority:
        labels.append(f"Priority: {name_of('priorities', filters.priority)}")
    if filters.owner_id is not None:
        labels.append(f"Owner: {name_of('owners', filters.owner_id)}")
    if filters.assigned_to_id is not None:
        labels.append(f"Assigned to: #{filters.assigned_to_id}")
    if filters.group_id is not None:
        labels.append(f"Group: {name_of('groups', filters.group_id)}")
    if filters.period:
        labels.append(f"Period: {dict(PERIOD_CHOICES)[filters.period]}")
    if filters.date_from or filters.date_to:
        field_label = dict(DATE_FIELD_CHOICES)[filters.date_field]
        span = f"{filters.date_from or '…'} → {filters.date_to or '…'}"
        labels.append(f"{field_label}: {span}")
    if filters.overdue_only:
        labels.append("Overdue only")
    if filters.due_soon:
        labels.append(f"Due within {DUE_SOON_DAYS} days")
    if filters.unassigned_only:
        labels.append("Unassigned only")
    return labels


def _report_title(filters, options):
    base = dict(REPORT_TYPE_CHOICES).get(filters.report_type, "Report")
    if filters.type_id is not None:
        for option in options.get("types", []):
            if option["value"] == filters.type_id:
                return f"{base}: {option['label']}"
    return base
