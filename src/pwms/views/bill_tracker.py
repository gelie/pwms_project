import logging

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import (
    Bill,
    BillUpdateLog,
    State,
    TransitionLog,
)
from ..services.bill_updates import (
    queue_bill_update_rejection,
)

logger = logging.getLogger(__name__)


#: How often the Bill tracker page re-reads the list, in seconds.
BILL_TRACKER_POLL_SECONDS = 3

#: How many inbound webhook rows the tracker's activity panel shows.
BILL_TRACKER_ACTIVITY_ROWS = 8

#: Bill fields compared in the review popup. These are the fields the webhook
#: snapshot carries; adding one here without adding it to ``bill_snapshot()``
#: would simply never show a change.
BILL_REVIEW_FIELDS = (
    ("bill_number", "B-number"),
    ("title", "Title"),
    ("short_title", "Short title"),
    ("description", "Description"),
    ("bill_type", "Bill type"),
    ("house_of_origin", "House of origin"),
    ("sponsor_name", "Sponsor"),
    ("introduced_date", "Introduced date"),
    ("responsible_committee", "Responsible committee"),
    ("current_state", "Internal stage"),
    ("public_status", "Public status"),
    ("priority", "Priority"),
    ("deadline", "Deadline"),
    ("is_overdue", "Overdue"),
    ("owner", "Owner"),
    ("assigned_to", "Assignee"),
)

BILL_TYPE_LABELS = dict(Bill.BILL_TYPE_CHOICES)
BILL_HOUSE_LABELS = dict(Bill.HOUSE_CHOICES)


def _display_value(value):
    """Normalise a snapshot value for the review table."""
    if isinstance(value, dict):
        value = (
            value.get("name")
            or value.get("display_name")
            or value.get("username")
            or ""
        )
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None or value == "":
        return "—"
    return value


def _review_diff(baseline, incoming):
    """The fields an update changes, with the accepted value beside the new one."""
    rows = []
    for key, label in BILL_REVIEW_FIELDS:
        old = _display_value(baseline.get(key))
        new = _display_value(incoming.get(key))
        if str(old) != str(new):
            rows.append({"label": label, "old": old, "new": new})
    return rows


def _review_summary(incoming):
    """Every field of an update that has no accepted version to compare with."""
    return [
        {"label": label, "value": _display_value(incoming.get(key))}
        for key, label in BILL_REVIEW_FIELDS
    ]


def _bill_tracker_context():
    """
    Everything the Bill tracker page renders, in one read.

    The receiving side is deliberately *not* the source of truth: a row shows
    the last **approved** snapshot, and each inbound post waits for review.
    Approving a post adopts its bill snapshot, so a status change advances the
    stepper and an edit updates the displayed fields only once accepted.

    The stepper follows the **public** status vocabulary (BRS §12): the bill's
    internal states collapse onto the citizen-facing statuses. Before any post
    has been approved (a legacy bill, or a NEW still awaiting review), the live
    record supplies the baseline and ``TransitionLog`` supplies the visited
    steps.
    """
    bills = list(
        Bill.objects.select_related(
            "current_state",
            "workflow_type",
            "sponsor",
        ).order_by("bill_number")
    )

    states_by_type: dict[int, list[State]] = {}
    for state in (
        State.objects.filter(
            workflow_type_id__in={bill.workflow_type_id for bill in bills}
        )
        .select_related("public_status")
        .order_by("order", "name")
    ):
        states_by_type.setdefault(state.workflow_type_id, []).append(state)

    # One stepper step per public status, in the order the type's states reach
    # them. Repeated internal states (the four committee stages) share a step;
    # the first state to reach a status supplies its colour.
    public_steps: dict[int, list[dict]] = {}
    for type_id, states in states_by_type.items():
        seen_public: set[int] = set()
        steps = []
        for state in states:
            public = state.public_status
            if public is None or public.pk in seen_public:
                continue
            seen_public.add(public.pk)
            steps.append({"status": public, "color": state.color})
        public_steps[type_id] = steps

    visited: dict[int, set[int]] = {}
    if bills:
        content_type = ContentType.objects.get_for_model(Bill)
        for object_id, from_id, to_id in TransitionLog.objects.filter(
            content_type=content_type, object_id__in=[bill.pk for bill in bills]
        ).values_list("object_id", "from_state_id", "to_state_id"):
            seen = visited.setdefault(object_id, set())
            if from_id is not None:
                seen.add(from_id)
            if to_id is not None:
                seen.add(to_id)

    # Inbound posts per bill, oldest first: the review queue is applied in the
    # order the posts arrived, so each approval has the right predecessor.
    inbound_by_bill: dict[int, list[BillUpdateLog]] = {}
    if bills:
        for log in (
            BillUpdateLog.objects.filter(
                direction=BillUpdateLog.DIRECTION_INBOUND,
                event__in=BillUpdateLog.UPDATE_EVENTS,
                bill_id__in=[bill.pk for bill in bills],
            )
            .select_related("reviewed_by")
            .order_by("created_at")
        ):
            inbound_by_bill.setdefault(log.bill_id, []).append(log)

    inbound = list(
        BillUpdateLog.objects.filter(direction=BillUpdateLog.DIRECTION_INBOUND)
        .select_related("bill")
        .order_by("-created_at")[:BILL_TRACKER_ACTIVITY_ROWS]
    )

    rows = []
    for bill in bills:
        state_by_id = {
            state.pk: state for state in states_by_type.get(bill.workflow_type_id, [])
        }
        state_by_name = {
            state.name: state for state in states_by_type.get(bill.workflow_type_id, [])
        }
        logs = inbound_by_bill.get(bill.pk, [])
        approved = [log for log in logs if log.status == BillUpdateLog.STATUS_APPROVED]
        pending = [log for log in logs if log.status == BillUpdateLog.STATUS_RECEIVED]
        accepted = approved[-1] if approved else None
        accepted_snapshot = (accepted.payload.get("bill") or {}) if accepted else None
        decisions = [
            log
            for log in logs
            if log.status
            in (BillUpdateLog.STATUS_APPROVED, BillUpdateLog.STATUS_REJECTED)
        ]
        last_decision = decisions[-1] if decisions else None

        # Displayed fields come from the accepted snapshot once one exists; a
        # bill with nothing accepted yet falls back to the live record.
        if accepted_snapshot:
            public_status_name = accepted_snapshot.get("public_status", "")
            internal_stage = accepted_snapshot.get("current_state", "")
            title = accepted_snapshot.get("title") or bill.title
            bill_type_label = BILL_TYPE_LABELS.get(
                accepted_snapshot.get("bill_type"), "—"
            )
            house_label = BILL_HOUSE_LABELS.get(
                accepted_snapshot.get("house_of_origin"), "—"
            )
            sponsor = accepted_snapshot.get("sponsor_name") or _display_value(
                accepted_snapshot.get("sponsor")
            )
        else:
            current_state = state_by_id.get(bill.current_state_id)
            public_status_name = (
                current_state.public_status.name if current_state else ""
            )
            internal_stage = current_state.name if current_state else ""
            title = bill.title
            bill_type_label = bill.get_bill_type_display() or "—"
            house_label = bill.get_house_of_origin_display() or "—"
            sponsor = (
                bill.sponsor.display_name if bill.sponsor_id else bill.sponsor_name
            )

        if approved:
            visited_public: set[str] = set()
            for log in approved:
                snapshot = log.payload.get("bill") or {}
                if snapshot.get("public_status"):
                    visited_public.add(snapshot["public_status"])
                # A status change names the state it moved out of; its public
                # status was visited even though the new snapshot no longer
                # shows it.
                from_state = state_by_name.get(
                    (log.payload.get("status_change") or {}).get("from_state")
                )
                if from_state is not None and from_state.public_status:
                    visited_public.add(from_state.public_status.name)
        else:
            # Nothing accepted yet: the live transition history is the only
            # truthful record of what the bill has been through.
            visited_public = {
                state_by_id[state_id].public_status.name
                for state_id in visited.get(bill.pk, set())
                if state_id in state_by_id and state_by_id[state_id].public_status
            }
        if public_status_name:
            visited_public.add(public_status_name)

        current_color = ""
        steps = []
        for entry in public_steps.get(bill.workflow_type_id, []):
            public = entry["status"]
            if public.name == public_status_name:
                status = "current"
                current_color = entry["color"]
            elif public.name in visited_public:
                status = "done"
            else:
                status = "todo"
            steps.append(
                {"name": public.name, "status": status, "color": entry["color"]}
            )

        rows.append(
            {
                "bill": bill,
                "steps": steps,
                "title": title,
                "bill_type_label": bill_type_label,
                "house_label": house_label,
                "sponsor": sponsor or "—",
                "public_status": public_status_name,
                "current_color": current_color,
                "internal_stage": internal_stage,
                "accepted_event": accepted,
                "accepted_at": (
                    (accepted.reviewed_at or accepted.created_at) if accepted else None
                ),
                "last_decision": last_decision,
                "pending": pending[0] if pending else None,
                "pending_count": len(pending),
                "pending_event_display": (
                    pending[0].get_event_display() if pending else ""
                ),
            }
        )

    return {
        "rows": rows,
        "recent": [
            {
                "log": log,
                # Inbound rows have no local actor; the posted body names one.
                "actor_name": (
                    (log.payload.get("actor") or {}).get("username")
                    or (log.payload.get("rejected_by") or {}).get("username", "")
                ),
            }
            for log in inbound
        ],
        "last_received": inbound[0].created_at if inbound else None,
        "poll_seconds": BILL_TRACKER_POLL_SECONDS,
    }


def _inbound_update_or_404(update_id):
    return get_object_or_404(
        BillUpdateLog.objects.select_related("bill"),
        update_id=update_id,
        direction=BillUpdateLog.DIRECTION_INBOUND,
        # A rejection callback is inbound too, but it is not reviewable.
        event__in=BillUpdateLog.UPDATE_EVENTS,
    )


def _has_earlier_pending_update(log):
    """True when this bill has an older undecided update waiting."""
    return BillUpdateLog.objects.filter(
        direction=BillUpdateLog.DIRECTION_INBOUND,
        bill_id=log.bill_id,
        status=BillUpdateLog.STATUS_RECEIVED,
        event__in=BillUpdateLog.UPDATE_EVENTS,
        created_at__lt=log.created_at,
    ).exists()


def _review_baseline(log):
    """The accepted bill snapshot an incoming update is compared against."""
    if log.bill_id is None:
        return {}
    approved = (
        BillUpdateLog.objects.filter(
            direction=BillUpdateLog.DIRECTION_INBOUND,
            bill_id=log.bill_id,
            status=BillUpdateLog.STATUS_APPROVED,
        )
        .order_by("-created_at")
        .first()
    )
    if approved is None:
        return {}
    return approved.payload.get("bill") or {}


def bill_update_review(request, update_id):
    """HTMX: the review popup for one inbound update."""
    log = _inbound_update_or_404(update_id)
    incoming = log.payload.get("bill") or {}
    baseline = _review_baseline(log)
    actor = log.payload.get("actor") or {}
    return render(
        request,
        "pwms/partials/bill_update_review.html",
        {
            "log": log,
            "incoming": incoming,
            "status_change": log.payload.get("status_change") or {},
            "diff_rows": _review_diff(baseline, incoming) if baseline else [],
            "summary_rows": [] if baseline else _review_summary(incoming),
            "actor_name": actor.get("display_name") or actor.get("username", ""),
            "can_decide": log.status == BillUpdateLog.STATUS_RECEIVED,
        },
    )


@require_POST
def bill_update_approve(request, update_id):
    """Apply one inbound update to the tracker and record who approved it."""
    log = _inbound_update_or_404(update_id)
    if log.status == BillUpdateLog.STATUS_RECEIVED:
        # The review queue is chronological: accepting a later post would make
        # the accepted history disagree with the order events happened in.
        if _has_earlier_pending_update(log):
            messages.error(
                request,
                "Review the earlier update for this bill first.",
            )
            return redirect("pwms:bill_tracker")

        log.status = BillUpdateLog.STATUS_APPROVED
        log.reviewed_at = timezone.now()
        log.reviewed_by = request.user
        log.save(update_fields=["status", "reviewed_at", "reviewed_by", "updated_at"])
        messages.success(
            request,
            f"Approved {log.get_event_display()} for {log.bill_number or 'the bill'}.",
        )
    return redirect("pwms:bill_tracker")


@require_POST
def bill_update_reject(request, update_id):
    """
    Reject one inbound update and post the decision back to the sender's API.

    The local row records the decision immediately (status, reason, reviewer and
    time); the callback is queued so the sender's log is updated without
    holding up the request, exactly as outbound updates are delivered.
    """
    log = _inbound_update_or_404(update_id)
    if log.status == BillUpdateLog.STATUS_RECEIVED:
        if _has_earlier_pending_update(log):
            messages.error(
                request,
                "Review the earlier update for this bill first.",
            )
            return redirect("pwms:bill_tracker")

        reason = request.POST.get("reason", "").strip()[:500]
        decided_at = timezone.now()
        log.status = BillUpdateLog.STATUS_REJECTED
        log.rejection_reason = reason
        log.rejected_at = decided_at
        log.reviewed_at = decided_at
        log.reviewed_by = request.user
        log.save(
            update_fields=[
                "status",
                "rejection_reason",
                "rejected_at",
                "reviewed_at",
                "reviewed_by",
                "updated_at",
            ]
        )
        queue_bill_update_rejection(log, reason, actor=request.user)
        messages.success(
            request,
            f"Rejected {log.get_event_display()} for {log.bill_number or 'the bill'}.",
        )
    return redirect("pwms:bill_tracker")


def bill_tracker(request):
    """
    Live demo of the Bill update webhook: every bill and its lifecycle stepper.

    The page polls this view, so a bill created in PWMS appears once its ``NEW``
    update has been received and a status change advances the stepper once the
    ``STATUS_CHANGE`` post is received. Nothing here is separate state: the list
    is the same ``Bill`` table the API publishes, and the activity panel is the
    inbound half of ``BillUpdateLog``. ``EDITS`` only refreshes the details.
    """
    context = _bill_tracker_context()
    if request.headers.get("HX-Request"):
        return render(request, "pwms/partials/bill_tracker_body.html", context)
    return render(request, "pwms/bill-tracker.html", context)
