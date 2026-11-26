import logging
from collections import Counter
from datetime import timedelta

from django.contrib.auth.decorators import login_not_required
from django.shortcuts import render
from django.utils import timezone

from ..models import (
    TransitionLog,
    WorkflowReferral,
)
from ..services.permissions import (
    visible_instances,
)
from ..services.progress import machine_for

logger = logging.getLogger(__name__)

from .common import _WORKFLOW_MODELS, _workflow_detail_url, _workflow_identifier


@login_not_required
def index(request):
    """Home page - the only route reachable without signing in."""
    return render(request, "pwms/index.html")


def dashboard(request):
    """Personal summary of the work the signed-in user may act on.

    Every number on the page comes from one permission pass over the four
    workflow registers, through :func:`visible_instances` — the same chain the
    detail pages use. A metric therefore can never count a row the user could
    not open, and referrals / transition logs are filtered through that same
    set rather than queried in their own right.
    """
    user = request.user

    accessible = []
    for model in _WORKFLOW_MODELS:
        accessible.extend(
            visible_instances(
                user,
                model.objects.select_related(
                    "workflow_type", "current_state", "owner", "assigned_to"
                ),
            )
        )

    # (content type, object id) -> instance. Resolves referrals and transition
    # logs back to their workflow, and doubles as the permission filter for
    # them: anything the user may not view is simply absent from the map.
    by_key = {
        (instance._instance_ct().pk, instance.pk): instance for instance in accessible
    }

    now = timezone.now()
    due_soon_cutoff = now + timedelta(days=DUE_SOON_DAYS)

    # The machine (a type's states + transition graph) is read once per workflow
    # type rather than once per row: a row's percentage needs nothing but the
    # type's machine and the row's own current state, never its logs.
    machines = {}

    open_instances = [i for i in accessible if not i.current_state.is_terminal]
    assigned = _dashboard_by_deadline(
        [i for i in accessible if i.assigned_to_id == user.pk], now
    )
    overdue = _dashboard_by_deadline([i for i in accessible if i.is_overdue], now)
    due_soon = _dashboard_by_deadline(
        [
            i
            for i in open_instances
            if i.deadline is not None and now <= i.deadline <= due_soon_cutoff
        ],
        now,
    )

    referral_rows = _dashboard_referrals(user, by_key, now, machines)
    total = len(accessible)
    context = {
        "total_count": total,
        "open_count": len(open_instances),
        "assigned_count": len(assigned),
        "overdue_count": len(overdue),
        "due_soon_count": len(due_soon),
        "unassigned_count": sum(1 for i in open_instances if i.assigned_to_id is None),
        "assigned_rows": _dashboard_rows(assigned, machines),
        "overdue_rows": _dashboard_rows(overdue, machines),
        "due_soon_rows": _dashboard_rows(due_soon, machines),
        "type_breakdown": _dashboard_breakdown(
            Counter(i.workflow_type.name for i in accessible), total
        ),
        "status_breakdown": _dashboard_breakdown(
            Counter(i.current_state.public_status.name for i in accessible),
            total,
        ),
        "referral_rows": referral_rows[:DASHBOARD_ROWS],
        "referral_count": len(referral_rows),
        "activity_rows": _dashboard_activity(by_key),
        "due_soon_days": DUE_SOON_DAYS,
        "has_work": bool(accessible),
    }
    return render(request, "pwms/dashboard.html", context)


#: Workflows due within this many days are counted as "due soon".
DUE_SOON_DAYS = 7

#: Each dashboard list shows this many rows before its "view all" link takes over.
DASHBOARD_ROWS = 5

#: Transition-log rows scanned when building the activity feed (see below).
DASHBOARD_ACTIVITY_SCAN = 200

#: State changes kept in the activity feed.
DASHBOARD_ACTIVITY_ROWS = 8


def _dashboard_by_deadline(instances, now):
    """Soonest deadline first; undated rows last, keeping their input order."""
    return sorted(instances, key=lambda i: (i.deadline is None, i.deadline or now))


def _dashboard_rows(instances, machines):
    """Presentation rows for one dashboard list, capped at ``DASHBOARD_ROWS``."""
    rows = []
    for instance in instances[:DASHBOARD_ROWS]:
        machine = _machine_for(machines, instance)
        rows.append(
            {
                "object": instance,
                "url": _workflow_detail_url(instance),
                "identifier": _workflow_identifier(instance),
                "percent": (
                    machine.percent(instance.current_state_id) if machine else None
                ),
            }
        )
    return rows


def _machine_for(cache, instance):
    """
    The instance's :class:`WorkflowMachine`, built at most once per type.

    ``None`` when the type has no machine configured, which is what the template
    treats as “no progress to show”.
    """
    key = instance.workflow_type_id
    if key not in cache:
        machine = machine_for(instance.workflow_type)
        cache[key] = machine if machine.has_machine else None
    return cache[key]


def _dashboard_breakdown(counter, total):
    """``Counter`` -> rows carrying their rounded share of ``total``, biggest first."""
    if not total:
        return []
    return [
        {"label": label, "count": count, "percent": round(count * 100 / total)}
        for label, count in counter.most_common()
    ]


def _dashboard_referrals(user, by_key, now, machines):
    """Open referrals to the user's committees, on workflows they may view."""
    group_ids = user.memberships.filter(is_active=True).values_list(
        "group_id", flat=True
    )
    rows = []
    for referral in WorkflowReferral.objects.filter(
        referred_to_id__in=group_ids, status="open"
    ).select_related("referred_to", "referred_by"):
        workflow = by_key.get((referral.content_type_id, referral.object_id))
        if workflow is None:
            continue
        machine = _machine_for(machines, workflow)
        rows.append(
            {
                "referral": referral,
                "workflow": workflow,
                "url": _workflow_detail_url(workflow),
                "percent": (
                    machine.percent(workflow.current_state_id) if machine else None
                ),
            }
        )
    rows.sort(
        key=lambda row: (
            row["referral"].due_date is None,
            row["referral"].due_date or now,
        )
    )
    return rows


def _dashboard_activity(by_key):
    """Newest state changes on the user's workflows, most recent first.

    One global scan, then a filter through ``by_key``, rather than a per-type
    query: it keeps the page to a single read while still refusing to surface
    activity on work the user may not see.
    """
    rows = []
    recent = TransitionLog.objects.select_related(
        "from_state", "to_state", "actor"
    ).order_by("-timestamp")[:DASHBOARD_ACTIVITY_SCAN]
    for log in recent:
        workflow = by_key.get((log.content_type_id, log.object_id))
        if workflow is None:
            continue
        rows.append(
            {
                "log": log,
                "workflow": workflow,
                "url": _workflow_detail_url(workflow),
            }
        )
        if len(rows) >= DASHBOARD_ACTIVITY_ROWS:
            break
    return rows
