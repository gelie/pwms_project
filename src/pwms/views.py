import logging
from collections import Counter
from datetime import timedelta
from typing import ClassVar
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import AuthenticationForm, UsernameField
from django.contrib.auth.views import LoginView as BaseLoginView
from django.contrib.auth.views import LogoutView as BaseLogoutView
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Case, CharField, IntegerField, Q, Value, When
from django.http import (
    FileResponse,
    Http404,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from .forms import (
    BillForm,
    BillVersionForm,
    ChildResolutionForm,
    ChildResolutionFormSet,
    DelegateAdderForm,
    DelegationParticipantAdderForm,
    DelegationParticipantFormSet,
    DelegationReportForm,
    DelegationReportUpdateForm,
    InternationalAgreementForm,
    InternationalResolutionForm,
    ReferralForm,
    ReportShareForm,
    ResolutionAdderForm,
    WorkflowNoteForm,
)
from .models import (
    AbstractLegislativeWorkflow,
    Attachment,
    AttachmentVersion,
    Bill,
    BillVersion,
    City,
    Country,
    DelegationParticipant,
    DelegationReport,
    Group,
    InternationalAgreement,
    InternationalResolution,
    Notification,
    TransitionLog,
    User,
    WorkflowNote,
    WorkflowReferral,
    WorkflowType,
)
from .notifications import notify_workflow_created
from .reporting import (
    ATTACHMENT_CHOICES,
    DATE_FIELD_CHOICES,
    EXPORT_FORMATS,
    PERIOD_CHOICES,
    REPORT_TYPE_CHOICES,
    SORT_CHOICES,
    ReportFilters,
    absolute_share_url,
    build_report,
    create_share,
    report_for_share,
    resolve_share,
    response_for,
    response_for_instrument,
    send_share_email,
)
from .services import attachments as attachments_service
from .services.history import TIMELINE_PREVIEW_LIMIT, instance_timeline
from .services.permissions import (
    DELETE,
    EDIT,
    TRANSITION,
    VIEW,
    permissions_for,
    require,
    resolve,
    visible_instances,
)
from .services.progress import machine_for, workflow_progress
from .utils.diagrams import workflow_type_diagram_path

logger = logging.getLogger(__name__)


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
            Counter(i.current_state.get_public_name_display() for i in accessible),
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


#: Concrete workflow models shown on the unified register, in display order.
#: Each concrete model owns one ``WorkflowType``; the unified list spans them all.
_WORKFLOW_MODELS = (
    DelegationReport,
    InternationalResolution,
    InternationalAgreement,
    Bill,
)


def _workflow_identifier(instance):
    """The human-facing identifier this workflow carries ("" when it has none)."""
    return instance.identifier


def _workflow_search_text(instance):
    """Lower-cased haystack the free-text filter matches against."""
    return " ".join((instance.title or "", _workflow_identifier(instance))).lower()


def workflows(request):
    """Unified register of every workflow instance the signed-in user may view.

    One table spans all concrete workflow types, narrowed by free text, type,
    state and priority. Rows are admitted through :func:`visible_instances` —
    the same permission chain the detail pages use — so the page never lists a
    row whose own page would 403. The type and state options are derived from
    that accessible set rather than the whole register, so the filters cannot
    advertise types or states the user has no access to.
    """
    accessible = []
    for model in _WORKFLOW_MODELS:
        accessible.extend(
            visible_instances(
                request.user,
                model.objects.select_related("workflow_type", "current_state", "owner"),
            )
        )
    # Instances arrive grouped by type; re-sort so the merged list is newest first.
    accessible.sort(key=lambda instance: instance.created_at, reverse=True)

    query = request.GET.get("q", "").strip()
    type_filter = request.GET.get("type", "")
    state_filter = request.GET.get("status", "")
    priority_filter = request.GET.get("priority", "")

    needle = query.lower()
    rows = [
        {
            "object": instance,
            "url": _workflow_detail_url(instance),
            "identifier": _workflow_identifier(instance),
        }
        for instance in accessible
        if (not needle or needle in _workflow_search_text(instance))
        and (not type_filter or instance.workflow_type.name == type_filter)
        and (not state_filter or instance.current_state.name == state_filter)
        and (not priority_filter or instance.priority == priority_filter)
    ]

    context = {
        "rows": rows,
        "query": query,
        "type_choices": sorted(
            {instance.workflow_type.name for instance in accessible}
        ),
        "state_choices": sorted(
            {instance.current_state.name for instance in accessible}
        ),
        "priority_choices": AbstractLegislativeWorkflow._meta.get_field(
            "priority"
        ).choices,
        "has_filters": bool(query or type_filter or state_filter or priority_filter),
    }
    return render(request, "pwms/workflows.html", context)


def all_groups(request):
    """List every group in the organisational hierarchy (sign-in required).

    The list is narrowed by a free-text name filter (matched against the name
    and short name, as in the admin) and a group-type filter. The type options
    are derived from the groups on the page rather than the full choice list,
    so the filter cannot advertise a type no group uses.
    """
    groups = list(Group.objects.select_related("parent").order_by("name"))

    query = request.GET.get("q", "").strip()
    type_filter = request.GET.get("type", "")

    needle = query.lower()
    filtered = [
        group
        for group in groups
        if (
            not needle
            or needle in group.name.lower()
            or needle in group.short_name.lower()
        )
        and (not type_filter or group.group_type == type_filter)
    ]

    context = {
        "groups": filtered,
        "query": query,
        "type_choices": [
            (value, label)
            for value, label in Group.GROUP_TYPE_CHOICES
            if any(group.group_type == value for group in groups)
        ],
        "has_filters": bool(query or type_filter),
    }
    return render(request, "pwms/all-groups.html", context)


def my_groups(request):
    """List the groups the signed-in user is a member of (sign-in required)."""
    today = timezone.localdate()
    memberships = (
        request.user.get_groups_with_roles()
        .select_related("group__parent")
        .annotate(
            status=Case(
                When(is_active=False, then=Value("Inactive")),
                When(end_date__lt=today, then=Value("Expired")),
                default=Value("Active"),
                output_field=CharField(),
            )
        )
        .order_by("group__name", "role__name")
    )
    return render(request, "pwms/my-groups.html", {"memberships": memberships})


def group_detail(request, pk):
    """Show one group: hierarchy context, details and its member count."""
    group = get_object_or_404(Group.objects.select_related("parent"), pk=pk)
    memberships = (
        group.members.filter(is_active=True)
        .select_related("user", "role")
        .order_by("user__last_name", "user__first_name", "role__name")
    )
    context = {
        "group": group,
        "memberships": memberships,
        # Distinct users with an active membership (the model's own helper).
        "member_count": group.get_active_members().count(),
        "child_groups": group.children.order_by("name"),
    }
    return render(request, "pwms/group-detail.html", context)


# --- Workflow instance CRUD -------------------------------------------------
# DelegationReport and InternationalResolution share the same list / detail /
# create / update / delete shape. Access is gated by
# SiteLoginRequiredMiddleware, so the views need no login decorator.


def _workflow_detail_url(instance):
    """Site URL for a concrete workflow instance (for generic relations)."""
    if isinstance(instance, DelegationReport):
        return reverse(
            "pwms:delegation_report_detail", kwargs={"public_id": instance.public_id}
        )
    if isinstance(instance, InternationalResolution):
        return reverse(
            "pwms:international_resolution_detail",
            kwargs={"public_id": instance.public_id},
        )
    if isinstance(instance, InternationalAgreement):
        return reverse(
            "pwms:international_agreement_detail",
            kwargs={"public_id": instance.public_id},
        )
    if isinstance(instance, Bill):
        return reverse("pwms:bill_detail", kwargs={"public_id": instance.public_id})
    return None


def _target_identifiers(instance):
    """
    The ``content_type`` / ``object_id`` pair a fragment's form posts back.

    A fragment is rendered both inside the page and standalone as the HTMX
    response, so both context builders add these or the form would lose the
    record it is acting on.
    """
    meta = instance._meta
    return {
        "target_content_type": f"{meta.app_label}.{meta.model_name}",
        "target_object_id": str(instance.pk),
    }


def _can_change_note(user, note, record):
    """
    Whether ``user`` may edit or delete ``note``.

    A note is its author's to change, and only while they may still edit the
    record it is on. A note whose author's account has since been deleted (the
    FK is nulled with the account) is left to the record's editors, or nothing
    could ever correct or remove it.
    """
    if note.author_id is not None and note.author_id != user.pk:
        return False
    return resolve(user, record, EDIT)


def _notes_context(request, instance):
    """
    Template context for a record's note log and the form that adds to it.

    Notes are rows (:class:`WorkflowNote`), so every instrument can carry them
    and each one keeps its own author. ``note_rows`` pairs each note with whether
    this reader may change it — its author, while they may still edit the record
    (see :func:`_can_change_note`). ``record_notes`` carries the record's own
    ``notes`` column where a type has one — the BRS attribute (BR02.3.13) edited
    with the record — so the panel can show it above the log.
    """
    can_edit = resolve(request.user, instance, EDIT)
    notes = list(instance.note_log().select_related("author"))
    context = {
        **_target_identifiers(instance),
        "note_rows": [
            {
                "note": note,
                "can_change": _can_change_note(request.user, note, instance),
            }
            for note in notes
        ],
        "record_notes": getattr(instance, "notes", ""),
        "can_edit_notes": can_edit,
    }
    if can_edit:
        context["notes_form"] = WorkflowNoteForm()
    return context


def _participants_context(request, report):
    """
    Template context for a delegation report's participant list and its adder.

    ``participants`` is who is on the delegation now; ``removed_participants``
    is who was taken off it, kept (and shown, muted) as the record of the
    change rather than deleted.
    """
    can_edit = resolve(request.user, report, EDIT)
    context = {
        "report": report,
        "participants": report.participants.filter(
            removed_at__isnull=True
        ).select_related("user"),
        "removed_participants": report.participants.filter(removed_at__isnull=False)
        .select_related("user", "removed_by")
        .order_by("-removed_at"),
        "can_edit_participants": can_edit,
    }
    if can_edit:
        context["participant_form"] = DelegationParticipantAdderForm(report=report)
    return context


def _resolutions_context(request, report):
    """
    Template context for a delegation report's adopted resolutions and its adder.

    ``resolutions`` is the international resolutions the report captured, paired
    with their URLs so the table can link to each. The adder is offered only to a
    reader who may both edit the report and create resolutions: capturing one
    creates a new instrument, so the edit right alone is not enough (see
    ``_can_create_form_type``), matching the report form's own resolutions section.
    """
    can_add = resolve(request.user, report, EDIT) and _can_create_form_type(
        request.user, InternationalResolutionForm
    )
    context = {
        "report": report,
        "resolutions": _viewable_sub_workflows(report, request.user),
        "can_add_resolutions": can_add,
    }
    if can_add:
        context["resolution_form"] = ChildResolutionForm()
    return context


def _can_now_complete(report):
    """Whether recording the ATC publication has unblocked a closing move.

    The seeded *Close – House approved* transition is guarded by an
    ``atc-update-published`` event, so the message that follows the update form
    can say whether the report may now be closed. It runs the same guard check
    ``perform_transition`` repeats, rather than assuming the event was the only
    thing standing in the way.
    """
    for transition in report.get_available_transitions():
        to_state = transition.to_state
        if to_state is None or not to_state.is_terminal:
            continue
        if not report.unmet_transition_conditions(transition):
            return True
    return False


def _updates_context(request, report):
    """Template context for a report's BR03 update history and its adder.

    Recording an update carrying any ATC detail is what emits the
    ``atc-update-published`` event the close guard needs, so the card is both the
    record of the publication and the screen that unblocks closing the report.
    """
    can_edit = resolve(request.user, report, EDIT)
    context = {
        "report": report,
        "updates": report.updates.select_related("resulting_state", "recorded_by"),
        "can_edit_updates": can_edit,
    }
    if can_edit:
        context["update_form"] = DelegationReportUpdateForm(report=report)
    return context


def _updates_response(request, report, *, success="", error="", form=None):
    """Re-render the BR03 update card, announcing a change when there is one."""
    context = _updates_context(request, report)
    context.update(_record_counters(report))
    if form is not None:
        context["update_form"] = form
    if error:
        context["updates_status"] = {"level": "danger", "message": error}
    elif success:
        context["updates_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/report_updates.html", context)


def _update_recorded_message(report, update):
    """What to tell whoever recorded ``update``, given what it did to the report."""
    if not update.has_atc_details:
        return (
            "Update recorded. Add the ATC reference, date, page or document to "
            "record the publication."
        )
    if _can_now_complete(report):
        return (
            "ATC update published recorded. The report may now be closed from "
            "the Status menu."
        )
    return "ATC update published recorded."


@require_POST
def delegation_report_update_add(request, public_id):
    """Record a BR03 update on a report, the ATC publication included (HTMX).

    Creating a row that carries any ATC detail emits the ``atc-update-published``
    event the report's *Close – House approved* transition is guarded by, so this
    is the screen that unblocks closing a report without an administrator (see
    :class:`~pwms.models.DelegationReportUpdate`).
    """
    report = get_object_or_404(DelegationReport, public_id=public_id)
    require(request.user, report, EDIT)
    form = DelegationReportUpdateForm(request.POST, report=report)
    if not form.is_valid():
        return _updates_response(request, report, form=form)
    update = form.save(commit=False)
    update.delegation_report = report
    update.recorded_by = request.user
    update.save()
    return _updates_response(
        request, report, success=_update_recorded_message(report, update)
    )


def _can_answer_referral(user, referral, instance):
    """
    Whether ``user`` may record the answer to ``referral``.

    The committee a matter was referred to owns the answer, so an active member of
    that group may respond — checked first, so it holds however little else the
    member may do. Failing that, anyone who may edit the **record** may record the
    response on the committee's behalf instead.

    That second path means the record's own editors, not a group whose edit comes
    from a referral: a referral lends the referred group edit while it is open, and
    reading that as "an editor of the record" let one committee answer another
    committee's referral merely because its own was open. Hence
    ``ignore_referral_grants`` (see ``resolve``).
    """
    if not getattr(user, "is_authenticated", False) or not referral.is_open:
        return False
    if user.memberships.filter(
        is_active=True, group_id=referral.referred_to_id
    ).exists():
        return True
    return resolve(user, instance, EDIT, ignore_referral_grants=True)


def _can_recall_referral(user, referral, instance):
    """
    Whether ``user`` may withdraw ``referral``.

    Whoever raised it may withdraw it, and so may an editor of the record — but
    an editor whose right comes from **a referral's own grant** may not: the group
    a matter was referred to answers it, and does not get to withdraw the request
    it is answering. Anything the record's own policy gives that editor still
    counts, because only the referral's contribution is left out (see
    ``resolve``).
    """
    if not getattr(user, "is_authenticated", False) or not referral.is_open:
        return False
    if user.pk == referral.referred_by_id:
        return True
    return resolve(user, instance, EDIT, ignore_referral_grants=True)


def _referral_context(request, instance):
    """
    Template context for one instance's referral panel.

    ``referral_rows`` pairs each :class:`WorkflowReferral` with the two actions
    the reader may take on it, because a template cannot resolve a permission
    that takes arguments. ``can_refer`` folds the edit right together with the
    state's own ``allows_referrals`` gate — the same gate ``refer()`` enforces —
    so the button never offers a referral the model would refuse. That edit right
    ignores what a referral conferred, so a group the record was referred to
    cannot refer it on (see :func:`_can_recall_referral`).
    """
    referrals = list(
        instance.referrals().select_related(
            "referred_to", "referred_by", "responded_by", "recalled_by"
        )
    )
    state = instance.current_state
    can_refer = bool(
        resolve(request.user, instance, EDIT, ignore_referral_grants=True)
        and state is not None
        and state.allows_referrals
    )
    context = {
        **_target_identifiers(instance),
        "referral_rows": [
            {
                "referral": referral,
                "can_respond": _can_answer_referral(request.user, referral, instance),
                "can_recall": _can_recall_referral(request.user, referral, instance),
            }
            for referral in referrals
        ],
        "can_refer": can_refer,
    }
    if can_refer:
        # Only built for someone who may actually raise one, so a reader who
        # cannot refer pays nothing for the group register.
        context["referral_form"] = ReferralForm()
    return context


def _workflow_detail_context(request, instance):
    """
    Context every workflow detail page shares.

    ``timeline`` is the merged TransitionLog + auditlog history (see
    :mod:`pwms.services.history`); ``diagram_*`` drives the Diagram tab, which
    serves the image ``manage.py generate_diagrams`` wrote for the type.

    Neighbours in the hierarchy are separate instances with their own access, so
    only the ones the reader may actually open are advertised.
    """
    parent = instance.parent_workflow
    parent_viewable = parent is not None and resolve(request.user, parent, VIEW)
    children = [
        {"object": child, "url": _workflow_detail_url(child)}
        for child in instance.sub_workflows
        if resolve(request.user, child, VIEW)
    ]
    timeline = instance_timeline(instance)
    diagram_path = workflow_type_diagram_path(instance.workflow_type)
    context = {
        # A generic alias the shared detail template renders from.
        "object": instance,
        "perms": permissions_for(request.user, instance),
        # The Status menu offers the transitions available from the current state,
        # but only to a reader who may take one: `transition` is a capability of
        # its own, separate from `edit`, so it is not one of `perms`.
        "can_transition": resolve(request.user, instance, TRANSITION),
        # The Progress tab: where the record sits in its type's state machine.
        "progress": workflow_progress(instance),
        # The tab renders the newest rows and says how many there are in total.
        "timeline": timeline[:TIMELINE_PREVIEW_LIMIT],
        "timeline_total": len(timeline),
        "parent": parent if parent_viewable else None,
        "parent_url": _workflow_detail_url(parent) if parent_viewable else None,
        "children": children,
        "diagram_available": diagram_path is not None,
        "diagram_url": reverse(
            "pwms:workflow_diagram", kwargs={"public_id": instance.public_id}
        ),
    }
    context.update(_referral_context(request, instance))
    context.update(_notes_context(request, instance))
    return context


def _record_counters(instance):
    """The record-level tab counters a panel response sends out-of-band.

    The Progress percentage and the Timeline's row count are shown in the tab bar,
    outside every panel's swap target, so a panel response has to carry them for
    the bar to stay in step (see ``pwms/partials/tab_counters.html``). Nothing a
    panel does changes either today; the pair is sent so the bar cannot drift
    while a reader works, and so a future in-page action that does move the record
    has somewhere to refresh them from.
    """
    if not isinstance(instance, AbstractLegislativeWorkflow):
        # Attachments accept any target; only a workflow record has this tab bar.
        return {}
    return {
        "tab_counters": {
            "progress_percent": workflow_progress(instance).percent,
            "timeline_total": len(instance_timeline(instance)),
        }
    }


def _deny_uncreatable_workflow_type(request):
    """
    Reject (HTTP 403) a POST naming a workflow type the user may not create in.

    Creation is group-scoped RBAC: a type may only be created in by someone
    holding one of its ``create_roles`` within its group. The form's
    ``workflow_type`` queryset already hides unusable types, but a hand-crafted
    POST bypasses that, so the refusal is made explicit here.
    """
    raw_type = request.POST.get("workflow_type")
    if not raw_type:
        return
    try:
        workflow_type = WorkflowType.objects.filter(pk=int(raw_type)).first()
    except TypeError, ValueError:
        return
    if workflow_type is not None and not workflow_type.can_create(request.user):
        raise PermissionDenied(
            _("You do not have a role that may create this workflow.")
        )


def _creatable_form_type(user, form_class):
    """
    The workflow type ``form_class`` creates in, if ``user`` may create it.

    Each concrete form represents exactly one type (``initial_workflow_type``),
    so a create view gates on that type rather than on "some type the user may
    create in": a user whose create role covers only a *different* type would
    otherwise be shown a form that can never validate.
    """
    return (
        WorkflowType.creatable_by(user)
        .filter(name=form_class.initial_workflow_type)
        .first()
    )


def _can_create_form_type(user, form_class):
    """True when ``user`` may create the workflow type ``form_class`` stands for."""
    return _creatable_form_type(user, form_class) is not None


def _child_resolution_formset(request):
    """Child-resolution formset for the report form, bound on POST."""
    data = request.POST if request.method == "POST" else None
    return ChildResolutionFormSet(data, prefix="resolutions")


def _create_child_resolution(report, form, user, resolution_type):
    """Save one validated child-resolution form and nest it under ``report``.

    The workflow type, initial state and owner of a resolution captured from the
    report are the report page's to supply, not the form's, so they are filled in
    here before the new instance is linked to the report that produced it.
    """
    resolution = form.save(commit=False)
    resolution.workflow_type = resolution_type
    resolution.current_state = (
        resolution_type.get_initial_state()
        or resolution_type.states.order_by("order", "name").first()
    )
    resolution.owner = user
    resolution.save()
    report.add_sub_workflow(resolution)
    return resolution


def _save_child_resolutions(report, formset, user):
    """
    Create the resolutions entered on a report form and nest them under it.

    Ignored unless ``user`` may create resolutions in the first place: the section
    is hidden from everyone else, so a forged POST must not create instances.
    """
    resolution_type = _creatable_form_type(user, InternationalResolutionForm)
    if resolution_type is None:
        return
    for form in formset:
        if not form.cleaned_data:
            continue  # an untouched "add another" row
        _create_child_resolution(report, form, user, resolution_type)


def _viewable_sub_workflows(instance, user):
    """Children of ``instance`` the user may open, paired with their URLs."""
    return [
        {"object": child, "url": _workflow_detail_url(child)}
        for child in instance.sub_workflows
        if resolve(user, child, VIEW)
    ]


def _report_form_context(request, form, participants, resolutions, report=None):
    """Context shared by the delegation report create and edit pages.

    The two "add a …" rows (delegates, resolutions) are prefixed so their inputs
    cannot collide with the report's own fields; nothing in them is validated.
    """
    return {
        "form": form,
        "report": report,
        "participant_formset": participants,
        "resolution_formset": resolutions,
        "child_resolutions": (
            _viewable_sub_workflows(report, request.user) if report is not None else []
        ),
        "can_add_resolutions": _can_create_form_type(
            request.user, InternationalResolutionForm
        ),
        "delegate_adder": DelegateAdderForm(prefix="delegate_adder"),
        "resolution_adder": ResolutionAdderForm(prefix="resolution_adder"),
        "is_create": report is None,
    }


# -- Delegation reports ------------------------------------------------------


def delegation_reports(request):
    """List delegation reports the user may view, with a free-text filter (BR09)."""
    query = request.GET.get("q", "").strip()
    matching = DelegationReport.objects.select_related(
        "workflow_type", "current_state", "owner", "assigned_to"
    ).order_by("-created_at")
    if query:
        matching = matching.filter(
            Q(reference_number__icontains=query)
            | Q(title__icontains=query)
            | Q(engagement_name__icontains=query)
        )
    # View access gates the listing itself, not just the row actions.
    reports = visible_instances(request.user, matching)
    editable_pks = {
        report.pk for report in reports if resolve(request.user, report, EDIT)
    }
    deletable_pks = {
        report.pk for report in reports if resolve(request.user, report, DELETE)
    }
    return render(
        request,
        "pwms/delegation-report-list.html",
        {
            "reports": reports,
            "query": query,
            "editable_pks": editable_pks,
            "deletable_pks": deletable_pks,
        },
    )


def delegation_report_detail(request, public_id):
    """Show one delegation report and everything attached to it."""
    report = get_object_or_404(
        DelegationReport.objects.select_related(
            "workflow_type", "current_state", "owner", "assigned_to"
        ),
        public_id=public_id,
    )
    require(request.user, report, VIEW)
    context = {
        "report": report,
        "transitions": report.get_available_transitions(),
    }
    context.update(_participants_context(request, report))
    context.update(_resolutions_context(request, report))
    context.update(_updates_context(request, report))
    context.update(_workflow_detail_context(request, report))
    context.update(_attachment_context(request, report))
    return render(request, "pwms/delegation-report-detail.html", context)


def delegation_report_create(request):
    """Create a delegation report, with its delegates and adopted resolutions."""
    if request.method == "POST":
        _deny_uncreatable_workflow_type(request)
        form = DelegationReportForm(request.POST, user=request.user)
        participants = DelegationParticipantFormSet(request.POST, prefix="participants")
        resolutions = _child_resolution_formset(request)
        if form.is_valid() and participants.is_valid() and resolutions.is_valid():
            with transaction.atomic():
                report = form.save()
                participants.instance = report
                participants.save()
                _save_child_resolutions(report, resolutions, request.user)
            messages.success(
                request, f'Delegation report "{report.reference_number}" created.'
            )
            notify_workflow_created(report, actor=request.user)
            return redirect("pwms:delegation_report_detail", public_id=report.public_id)
    else:
        if not _can_create_form_type(request.user, DelegationReportForm):
            messages.error(
                request,
                "You do not have a role that may create delegation reports.",
            )
            return redirect("pwms:delegation_reports")
        form = DelegationReportForm(user=request.user, initial={"owner": request.user})
        participants = DelegationParticipantFormSet(prefix="participants")
        resolutions = _child_resolution_formset(request)
    return render(
        request,
        "pwms/delegation-report-form.html",
        _report_form_context(request, form, participants, resolutions),
    )


def delegation_report_update(request, public_id):
    """Edit a delegation report, its delegates and any adopted resolutions."""
    report = get_object_or_404(DelegationReport, public_id=public_id)
    require(request.user, report, EDIT)
    if request.method == "POST":
        form = DelegationReportForm(request.POST, instance=report, user=request.user)
        participants = DelegationParticipantFormSet(
            request.POST,
            instance=report,
            prefix="participants",
            # Only who is on the delegation now; a removed row is history.
            queryset=report.participants.filter(removed_at__isnull=True),
            removed_by=request.user,
        )
        resolutions = _child_resolution_formset(request)
        if form.is_valid() and participants.is_valid() and resolutions.is_valid():
            with transaction.atomic():
                report = form.save()
                participants.save()
                _save_child_resolutions(report, resolutions, request.user)
            messages.success(
                request, f'Delegation report "{report.reference_number}" updated.'
            )
            return redirect("pwms:delegation_report_detail", public_id=report.public_id)
    else:
        form = DelegationReportForm(instance=report, user=request.user)
        participants = DelegationParticipantFormSet(
            instance=report,
            prefix="participants",
            queryset=report.participants.filter(removed_at__isnull=True),
            removed_by=request.user,
        )
        resolutions = _child_resolution_formset(request)
    return render(
        request,
        "pwms/delegation-report-form.html",
        _report_form_context(request, form, participants, resolutions, report=report),
    )


def delegation_report_delete(request, public_id):
    """Confirm (GET) then delete (POST) a delegation report."""
    report = get_object_or_404(DelegationReport, public_id=public_id)
    require(request.user, report, DELETE)
    if request.method == "POST":
        label = report.reference_number or report.title
        report.delete()
        messages.success(request, f'Delegation report "{label}" deleted.')
        return redirect("pwms:delegation_reports")
    return render(
        request, "pwms/delegation-report-confirm-delete.html", {"report": report}
    )


# -- International resolutions ------------------------------------------------


def international_resolutions(request):
    """List resolutions the user may view, with a free-text filter (BR09)."""
    query = request.GET.get("q", "").strip()
    matching = InternationalResolution.objects.select_related(
        "workflow_type", "current_state", "owner", "assigned_to", "responsible_group"
    ).order_by("-created_at")
    if query:
        matching = matching.filter(
            Q(resolution_number__icontains=query)
            | Q(title__icontains=query)
            | Q(resolution_text__icontains=query)
        )
    # View access gates the listing itself, not just the row actions.
    resolutions = visible_instances(request.user, matching)
    editable_pks = {
        resolution.pk
        for resolution in resolutions
        if resolve(request.user, resolution, EDIT)
    }
    deletable_pks = {
        resolution.pk
        for resolution in resolutions
        if resolve(request.user, resolution, DELETE)
    }
    return render(
        request,
        "pwms/international-resolution-list.html",
        {
            "resolutions": resolutions,
            "query": query,
            "editable_pks": editable_pks,
            "deletable_pks": deletable_pks,
        },
    )


def international_resolution_detail(request, public_id):
    """Show one international resolution, its parent and its audit trail."""
    resolution = get_object_or_404(
        InternationalResolution.objects.select_related(
            "workflow_type",
            "current_state",
            "owner",
            "assigned_to",
            "responsible_group",
        ),
        public_id=public_id,
    )
    require(request.user, resolution, VIEW)
    context = {
        "resolution": resolution,
        "transitions": resolution.get_available_transitions(),
    }
    context.update(_workflow_detail_context(request, resolution))
    context.update(_attachment_context(request, resolution))
    return render(request, "pwms/international-resolution-detail.html", context)


def international_resolution_create(request):
    """Create a resolution; the initial state is derived from its type."""
    if request.method == "POST":
        _deny_uncreatable_workflow_type(request)
        form = InternationalResolutionForm(request.POST, user=request.user)
        if form.is_valid():
            resolution = form.save()
            messages.success(
                request,
                f'International resolution "{resolution.resolution_number}" created.',
            )
            notify_workflow_created(resolution, actor=request.user)
            return redirect(
                "pwms:international_resolution_detail",
                public_id=resolution.public_id,
            )
    else:
        if not _can_create_form_type(request.user, InternationalResolutionForm):
            messages.error(
                request,
                "You do not have a role that may create international resolutions.",
            )
            return redirect("pwms:international_resolutions")
        form = InternationalResolutionForm(
            user=request.user, initial={"owner": request.user}
        )
    return render(
        request,
        "pwms/international-resolution-form.html",
        {"form": form, "is_create": True},
    )


def international_resolution_update(request, public_id):
    """Edit an international resolution."""
    resolution = get_object_or_404(InternationalResolution, public_id=public_id)
    require(request.user, resolution, EDIT)
    if request.method == "POST":
        form = InternationalResolutionForm(
            request.POST, instance=resolution, user=request.user
        )
        if form.is_valid():
            resolution = form.save()
            messages.success(
                request,
                f'International resolution "{resolution.resolution_number}" updated.',
            )
            return redirect(
                "pwms:international_resolution_detail",
                public_id=resolution.public_id,
            )
    else:
        form = InternationalResolutionForm(instance=resolution)
    return render(
        request,
        "pwms/international-resolution-form.html",
        {"form": form, "resolution": resolution, "is_create": False},
    )


def international_resolution_delete(request, public_id):
    """Confirm (GET) then delete (POST) an international resolution."""
    resolution = get_object_or_404(InternationalResolution, public_id=public_id)
    require(request.user, resolution, DELETE)
    if request.method == "POST":
        label = resolution.resolution_number or resolution.title
        resolution.delete()
        messages.success(request, f'International resolution "{label}" deleted.')
        return redirect("pwms:international_resolutions")
    return render(
        request,
        "pwms/international-resolution-confirm-delete.html",
        {"resolution": resolution},
    )


# -- International agreements ------------------------------------------------


def international_agreements(request):
    """List agreements the user may view, with a free-text filter (BR09)."""
    query = request.GET.get("q", "").strip()
    matching = InternationalAgreement.objects.select_related(
        "workflow_type", "current_state", "owner", "assigned_to"
    ).order_by("-created_at")
    if query:
        matching = matching.filter(
            Q(reference_number__icontains=query)
            | Q(title__icontains=query)
            | Q(submitting_department__icontains=query)
            | Q(responsible_minister_name__icontains=query)
            | Q(responsible_minister__first_name__icontains=query)
            | Q(responsible_minister__last_name__icontains=query)
        )
    # View access gates the listing itself, not just the row actions.
    agreements = visible_instances(request.user, matching)
    editable_pks = {
        agreement.pk
        for agreement in agreements
        if resolve(request.user, agreement, EDIT)
    }
    deletable_pks = {
        agreement.pk
        for agreement in agreements
        if resolve(request.user, agreement, DELETE)
    }
    return render(
        request,
        "pwms/international-agreement-list.html",
        {
            "agreements": agreements,
            "query": query,
            "editable_pks": editable_pks,
            "deletable_pks": deletable_pks,
        },
    )


def international_agreement_detail(request, public_id):
    """Show one international agreement, its parent and its audit trail."""
    agreement = get_object_or_404(
        InternationalAgreement.objects.select_related(
            "workflow_type",
            "current_state",
            "owner",
            "assigned_to",
        ).prefetch_related("referral_committees"),
        public_id=public_id,
    )
    require(request.user, agreement, VIEW)
    context = {
        "agreement": agreement,
        "transitions": agreement.get_available_transitions(),
    }
    context.update(_workflow_detail_context(request, agreement))
    context.update(_attachment_context(request, agreement))
    return render(request, "pwms/international-agreement-detail.html", context)


def international_agreement_create(request):
    """Create an agreement; the initial state is derived from its type."""
    if request.method == "POST":
        _deny_uncreatable_workflow_type(request)
        form = InternationalAgreementForm(request.POST, user=request.user)
        if form.is_valid():
            agreement = form.save()
            messages.success(
                request,
                f'International agreement "{agreement.reference_number}" created.',
            )
            notify_workflow_created(agreement, actor=request.user)
            return redirect(
                "pwms:international_agreement_detail",
                public_id=agreement.public_id,
            )
    else:
        if not _can_create_form_type(request.user, InternationalAgreementForm):
            messages.error(
                request,
                "You do not have a role that may create international agreements.",
            )
            return redirect("pwms:international_agreements")
        form = InternationalAgreementForm(
            user=request.user, initial={"owner": request.user}
        )
    return render(
        request,
        "pwms/international-agreement-form.html",
        {"form": form, "is_create": True},
    )


def international_agreement_update(request, public_id):
    """Edit an international agreement."""
    agreement = get_object_or_404(InternationalAgreement, public_id=public_id)
    require(request.user, agreement, EDIT)
    if request.method == "POST":
        form = InternationalAgreementForm(request.POST, instance=agreement)
        if form.is_valid():
            agreement = form.save()
            messages.success(
                request,
                f'International agreement "{agreement.reference_number}" updated.',
            )
            return redirect(
                "pwms:international_agreement_detail",
                public_id=agreement.public_id,
            )
    else:
        form = InternationalAgreementForm(instance=agreement)
    return render(
        request,
        "pwms/international-agreement-form.html",
        {"form": form, "agreement": agreement, "is_create": False},
    )


def international_agreement_delete(request, public_id):
    """Confirm (GET) then delete (POST) an international agreement."""
    agreement = get_object_or_404(InternationalAgreement, public_id=public_id)
    require(request.user, agreement, DELETE)
    if request.method == "POST":
        label = agreement.reference_number or agreement.title
        agreement.delete()
        messages.success(request, f'International agreement "{label}" deleted.')
        return redirect("pwms:international_agreements")
    return render(
        request,
        "pwms/international-agreement-confirm-delete.html",
        {"agreement": agreement},
    )


# -- Bills -------------------------------------------------------------------


def bills(request):
    """List bills the user may view, with a free-text filter (BRS §7.4)."""
    query = request.GET.get("q", "").strip()
    matching = Bill.objects.select_related(
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
        "responsible_committee",
    ).order_by("-created_at")
    if query:
        matching = matching.filter(
            Q(bill_number__icontains=query)
            | Q(title__icontains=query)
            | Q(short_title__icontains=query)
            | Q(sponsor_name__icontains=query)
            | Q(sponsor__first_name__icontains=query)
            | Q(sponsor__last_name__icontains=query)
        )
    # View access gates the listing itself, not just the row actions.
    viewable = visible_instances(request.user, matching)
    editable_pks = {bill.pk for bill in viewable if resolve(request.user, bill, EDIT)}
    deletable_pks = {
        bill.pk for bill in viewable if resolve(request.user, bill, DELETE)
    }
    return render(
        request,
        "pwms/bill-list.html",
        {
            "bills": viewable,
            "query": query,
            "editable_pks": editable_pks,
            "deletable_pks": deletable_pks,
        },
    )


def bill_detail(request, public_id):
    """Show one bill: profile, public status, transitions and audit trail."""
    bill = get_object_or_404(
        Bill.objects.select_related(
            "workflow_type",
            "current_state",
            "owner",
            "assigned_to",
            "responsible_committee",
        ),
        public_id=public_id,
    )
    require(request.user, bill, VIEW)
    context = {
        "bill": bill,
        "versions": bill.versions.select_related("recorded_by"),
        "transitions": bill.get_available_transitions(),
    }
    context.update(_workflow_detail_context(request, bill))
    context.update(_attachment_context(request, bill))
    return render(request, "pwms/bill-detail.html", context)


def bill_create(request):
    """Create a bill; the initial state is derived from its type."""
    if request.method == "POST":
        _deny_uncreatable_workflow_type(request)
        form = BillForm(request.POST, user=request.user)
        if form.is_valid():
            bill = form.save()
            messages.success(request, f'Bill "{bill.bill_number}" created.')
            notify_workflow_created(bill, actor=request.user)
            return redirect("pwms:bill_detail", public_id=bill.public_id)
    else:
        if not _can_create_form_type(request.user, BillForm):
            messages.error(request, "You do not have a role that may create bills.")
            return redirect("pwms:bills")
        form = BillForm(user=request.user, initial={"owner": request.user})
    return render(
        request,
        "pwms/bill-form.html",
        {"form": form, "is_create": True},
    )


def bill_update(request, public_id):
    """Edit a bill."""
    bill = get_object_or_404(Bill, public_id=public_id)
    require(request.user, bill, EDIT)
    if request.method == "POST":
        form = BillForm(request.POST, instance=bill)
        if form.is_valid():
            bill = form.save()
            messages.success(request, f'Bill "{bill.bill_number}" updated.')
            return redirect("pwms:bill_detail", public_id=bill.public_id)
    else:
        form = BillForm(instance=bill)
    return render(
        request,
        "pwms/bill-form.html",
        {"form": form, "bill": bill, "is_create": False},
    )


def bill_delete(request, public_id):
    """Confirm (GET) then delete (POST) a bill."""
    bill = get_object_or_404(Bill, public_id=public_id)
    require(request.user, bill, DELETE)
    if request.method == "POST":
        label = bill.bill_number or bill.title
        bill.delete()
        messages.success(request, f'Bill "{label}" deleted.')
        return redirect("pwms:bills")
    return render(
        request,
        "pwms/bill-confirm-delete.html",
        {"bill": bill},
    )


def bill_version_create(request, public_id):
    """Record a preserved version on a bill from the web UI (BRS §15A).

    Gated on ``edit`` rights on the bill rather than a separate capability:
    recording a version is an edit of the bill's record, and who recorded it is
    captured automatically for contributor accountability.
    """
    bill = get_object_or_404(Bill, public_id=public_id)
    require(request.user, bill, EDIT)
    if request.method == "POST":
        form = BillVersionForm(request.POST)
        if form.is_valid():
            version = form.save(commit=False)
            version.bill = bill
            version.recorded_by = request.user
            version.save()
            messages.success(request, f'Version "{version.version_label}" recorded.')
            return redirect("pwms:bill_detail", public_id=bill.public_id)
    else:
        # A newly recorded version is normally the one before Parliament now.
        form = BillVersionForm(initial={"is_current": True})
    return render(
        request,
        "pwms/bill-version-form.html",
        {"form": form, "bill": bill, "is_create": True},
    )


def bill_version_update(request, public_id, version_public_id):
    """Edit a recorded bill version (BRS §15A).

    Corrections are made in place rather than by deleting and re-recording: the
    row keeps its identity and ``recorded_by`` (who recorded it originally),
    while ``auditlog`` captures the edit as an UPDATE. ``is_current`` still
    normalises on save, so flagging this version demotes any other current one.
    """
    bill = get_object_or_404(Bill, public_id=public_id)
    version = get_object_or_404(BillVersion, public_id=version_public_id, bill=bill)
    require(request.user, bill, EDIT)
    if request.method == "POST":
        form = BillVersionForm(request.POST, instance=version)
        if form.is_valid():
            version = form.save()
            messages.success(request, f'Version "{version.version_label}" updated.')
            return redirect("pwms:bill_detail", public_id=bill.public_id)
    else:
        form = BillVersionForm(instance=version)
    return render(
        request,
        "pwms/bill-version-form.html",
        {"form": form, "bill": bill, "version": version, "is_create": False},
    )


# --- reporting -------------------------------------------------------------
#
# A report is a permission-scoped projection of the workflow registers, built by
# ``pwms.reporting``. The page renders a filter form plus an initial preview; the
# preview endpoint re-renders just that region for HTMX; the export endpoint
# streams the same figures as xlsx / pdf / html / csv; and the share endpoints
# pin a filter set behind a token so it can be handed on by link or email.


#: Filter values that are falsy for a URL — dropped when rebuilding a query.
_EMPTY_FILTER_VALUES = (None, "", False)


def _filter_query(filters):
    """The filter set as a query string (for export and share links)."""
    params = {
        key: value
        for key, value in filters.to_dict().items()
        if value not in _EMPTY_FILTER_VALUES
    }
    return urlencode(params)


def _report_context(request, report, *, share=None, is_shared=False):
    """Everything the reports page, its preview partial and the share modal read."""
    filters = report.filters
    query = _filter_query(filters)
    if share is not None:
        query = f"{query}&share={share.token}" if query else f"share={share.token}"
    return {
        "report": report,
        "filters": filters,
        "report_types": REPORT_TYPE_CHOICES,
        "period_choices": PERIOD_CHOICES,
        "date_field_choices": DATE_FIELD_CHOICES,
        "sort_choices": SORT_CHOICES,
        "export_formats": EXPORT_FORMATS,
        "attachment_choices": ATTACHMENT_CHOICES,
        "filter_params": filters.to_dict(),
        "filter_query": query,
        "share_form": ReportShareForm(initial={"title": report.title}),
        "share": share,
        "is_shared": is_shared,
    }


def reports(request):
    """The report builder: filters, a live preview, exports and sharing."""
    filters = ReportFilters.from_request(request)
    report = build_report(request.user, filters)
    return render(request, "pwms/reports.html", _report_context(request, report))


def reports_preview(request):
    """Just the report preview, so the filter form can swap it over HTMX."""
    filters = ReportFilters.from_request(request)
    report = build_report(request.user, filters)
    return render(
        request, "pwms/partials/report_preview.html", _report_context(request, report)
    )


def reports_export(request):
    """Download the current report as xlsx / pdf / html / csv.

    ``?share=<token>`` exports a shared report as its creator saw it, so a file
    downloaded from a share link matches the page it was downloaded from.
    """
    token = request.GET.get("share", "")
    if token:
        share = resolve_share(token)
        if share is None:
            raise Http404("No such report link.")
        report = report_for_share(share) if share.is_active else None
        if report is None:
            return HttpResponseBadRequest("This report link is no longer active.")
    else:
        report = build_report(request.user, ReportFilters.from_request(request))

    try:
        return response_for(report, request.GET.get("format", "xlsx"))
    except ValueError:
        return HttpResponseBadRequest("Unknown export format.")


@require_POST
def reports_share(request):
    """Mint a share for the current filters, optionally emailing and repeating it."""
    filters = ReportFilters.from_mapping(request.POST)
    report = build_report(request.user, filters)

    form = ReportShareForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"success": False, "errors": form.errors.get_json_data()}, status=400
        )

    data = form.cleaned_data
    scheduled = data["schedule"] not in ("", "none")
    # A repeating share is emailed now and thereafter, so either choice implies
    # the address list — the form rejects one without the other.
    email_to = data["recipients"] if (data["send_email"] or scheduled) else ""
    share = create_share(
        user=request.user,
        filters=filters,
        title=data["title"] or report.title,
        recipients=email_to,
        message=data["message"],
        expires_days=data["expires_days"] or None,
        schedule=data["schedule"] or "none",
        schedule_format=data["attach_format"],
    )
    link = absolute_share_url(share, request)

    sent = 0
    if email_to:
        try:
            sent = send_share_email(
                share,
                request=request,
                report=report,
                attachment_format=data["attach_format"],
            )
        except Exception as error:
            logger.exception("Report share %s could not be emailed", share.pk)
            return JsonResponse(
                {
                    "success": False,
                    "error": f"The link was created, but the email failed: {error}",
                    "link": link,
                    "sent": 0,
                    "schedule": share.schedule,
                },
                status=502,
            )

    return JsonResponse(
        {
            "success": True,
            "link": link,
            "sent": sent,
            "title": share.title,
            "schedule": share.schedule,
            "next_send_at": (
                share.next_send_at.isoformat() if share.next_send_at else None
            ),
            "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        }
    )


def report_shared(request, token):
    """Open a shared report read-only, exactly as its creator saw it."""
    share = resolve_share(token)
    if share is None:
        raise Http404("No such report link.")
    if not share.is_active:
        return render(
            request,
            "pwms/report-shared.html",
            {"share": share, "inactive": True},
            status=410,
        )

    report = report_for_share(share)
    if report is None:
        return render(
            request,
            "pwms/report-shared.html",
            {"share": share, "unreadable": True},
            status=410,
        )

    share.record_access()
    return render(
        request,
        "pwms/report-shared.html",
        _report_context(request, report, share=share, is_shared=True),
    )


# --- instrument routes (document, diagram, transition) ---------------------


def _workflow_by_public_id(public_id):
    """
    Resolve a workflow instance by public id, across the concrete models.

    ``public_id`` is a UUIDv7 that is unique *per table* rather than globally, so
    the lookup spans the whole register. The tables are disjoint, so the first
    hit is the instance; a miss is a 404.
    """
    for model in _WORKFLOW_MODELS:
        instance = (
            model.objects.select_related(
                "workflow_type",
                "workflow_type__group",
                "current_state",
                "owner",
                "assigned_to",
            )
            .filter(public_id=public_id)
            .first()
        )
        if instance is not None:
            return instance
    raise Http404("No such workflow instance.")


def workflow_document(request, public_id):
    """
    One instrument as a formal, filed-style document.

    ``?format=pdf`` (the default) or ``?format=html``. VIEW on the instance is
    required, exactly as for its detail page, so the document can never disclose
    a record the reader could not open.
    """
    instance = _workflow_by_public_id(public_id)
    require(request.user, instance, VIEW)
    try:
        return response_for_instrument(
            instance, request.user, request.GET.get("format", "pdf")
        )
    except ValueError:
        return HttpResponseBadRequest("Unknown document format.")


def workflow_diagram(request, public_id):
    """
    The state-machine diagram for an instance's workflow *type*.

    ``manage.py generate_diagrams`` renders one image per enabled type; this
    serves the SVG a detail page's Diagram tab embeds. Keyed by an instance's
    public id so it matches the other instrument routes, and VIEW on the
    instance is required. A type whose diagram has never been generated is a
    404 (the tab renders an empty state rather than a broken image).
    """
    instance = _workflow_by_public_id(public_id)
    require(request.user, instance, VIEW)
    path = workflow_type_diagram_path(instance.workflow_type)
    if path is None:
        raise Http404("No diagram has been generated for this workflow type.")
    # No filename= argument: the browser must render it inline, and an <img>-
    # loaded SVG cannot run any script it may carry.
    return FileResponse(path.open("rb"), content_type="image/svg+xml")


# <<<<<<< Updated upstream
def _transition_or_404(instance, raw_id):
    """
    The instance's transition named by ``raw_id``, if it is available right now.

    Looked up through ``get_available_transitions()`` rather than the type's whole
    transition table, so a hand-crafted request cannot name an edge the record has
    already passed. ``perform_transition()`` re-checks the same thing.
    """
    try:
        pk = int(raw_id or "")
    except TypeError, ValueError:
        raise Http404("Unknown transition.") from None
    transition = instance.get_available_transitions().filter(pk=pk).first()
    if transition is None:
        raise Http404("That transition is not available from the current state.")
    return transition


def _transition_page(
    request, instance, transition, *, unmet=(), errors=(), comment="", status=200
):
    """Render the status-change confirmation page for one transition."""
    return render(
        request,
        "pwms/workflow-transition.html",
        {
            "object": instance,
            "transition": transition,
            "unmet_conditions": list(unmet),
            "errors": list(errors),
            # A refused POST hands the comment back so the reader does not retype it.
            "comment": comment,
            "cancel_url": _workflow_detail_url(instance),
        },
        status=status,
    )


def workflow_transition(request, public_id):
    """
    Confirm (GET) then apply (POST) one state transition on an instrument.

    Keyed by public id like the document and diagram routes, so one view serves
    every instrument. The ``transition`` capability is required rather than
    ``edit``: moving a record on is authorised separately from editing it (see
    [System Design §4], the RBAC layers).

    The confirmation step exists because a transition is an audited state change
    that may need a comment (``Transition.requires_comment``) and may be blocked
    by guards. The page explains either before the POST, and
    ``perform_transition()`` validates them again when it is applied.
    """
    instance = _workflow_by_public_id(public_id)
    require(request.user, instance, TRANSITION)
    transition = _transition_or_404(
        instance, request.POST.get("transition") or request.GET.get("transition")
    )
    unmet = instance.unmet_transition_conditions(transition)

    if request.method == "POST":
        comment = request.POST.get("comment", "").strip()
        try:
            instance.perform_transition(
                transition,
                actor=request.user,
                comment=comment,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except ValidationError as exc:
            return _transition_page(
                request,
                instance,
                transition,
                unmet=unmet,
                errors=exc.messages,
                comment=comment,
                status=400,
            )
        messages.success(
            request,
            f"{instance.identifier} moved to “{transition.to_state.name}”.",
        )
        # Land on the Timeline, where the state change is recorded.
        return HttpResponseRedirect(f"{_workflow_detail_url(instance)}#pane-timeline")

    return _transition_page(request, instance, transition, unmet=unmet)


# =======
@login_not_required
# >>>>>>> Stashed changes
def about(request):
    """About page (sign-in required)."""
    return render(request, "pwms/about.html")


def contact(request):
    """Contact page (sign-in required)."""
    return render(request, "pwms/contact.html")


# -- alerts -----------------------------------------------------------------
#: Alerts listed on the alerts page (the bell menu shows the newest few).
ALERT_PAGE_SIZE = 25


def notifications(request):
    """Every in-app alert for the signed-in user, newest first."""
    inbox = Notification.objects.filter(
        recipient=request.user, channel="in_app"
    ).select_related("actor")
    return render(
        request,
        "pwms/notifications.html",
        {
            "alerts": inbox[:ALERT_PAGE_SIZE],
            "alert_total": inbox.count(),
            "unread_count": inbox.filter(read_at__isnull=True).count(),
        },
    )


def notification_open(request, public_id):
    """Open an alert's subject: mark it read, then follow its link."""
    alert = get_object_or_404(Notification, public_id=public_id, recipient=request.user)
    alert.mark_read()
    # Alerts without a target (e.g. an account-level notice) just mark themselves
    # read and leave the reader on the list.
    return redirect(alert.url or "pwms:notifications")


@require_POST
def notifications_read_all(request):
    """Mark every unread alert read. POST-only: reading is a state change."""
    updated = request.user.notifications.filter(
        channel="in_app", read_at__isnull=True
    ).update(read_at=timezone.now())
    if updated:
        messages.success(request, f"Marked {updated} alert(s) as read.")
    return redirect("pwms:notifications")


class LoginForm(AuthenticationForm):
    """Authentication form wired to the project's Bootstrap form styling."""

    username = UsernameField(
        # AD resolves the account name, the user principal name and the mail
        # address (see AUTH_LDAP_USER_SEARCH), so people can sign in with either
        # their account name or their email address.
        label=_("Username or email"),
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": _("Username or email"),
                "autocomplete": "username",
                "autofocus": True,
            }
        ),
    )
    password = forms.CharField(
        label=_("Password"),
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": _("Password"),
                "autocomplete": "current-password",
            }
        ),
    )


class LoginView(BaseLoginView):
    """Sign-in view handling the username/password authentication form."""

    template_name = "pwms/login.html"
    authentication_form = LoginForm
    # Send already-authenticated visitors straight to LOGIN_REDIRECT_URL.
    redirect_authenticated_user = True


class LogoutView(BaseLogoutView):
    """
    Sign-out view.

    Django only ends the session on POST, so GET renders a confirmation page
    whose form posts back here; a successful POST clears the session and
    redirects to ``settings.LOGOUT_REDIRECT_URL``.
    """

    template_name = "pwms/logout.html"
    http_method_names: ClassVar[list[str]] = ["get", "post", "options"]

    def get(self, request, *args, **kwargs):
        """Render the confirmation page (the logout itself happens on POST)."""
        return self.render_to_response(self.get_context_data(**kwargs))


# @htmx_
def user_search(request):
    """Search active users and return HTML results for HTMX."""
    search_query = request.GET.get("search", "")

    users = User.objects.filter(is_active=True)

    if search_query:
        users = users.filter(
            Q(username__icontains=search_query)
            | Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
        )

    users = users.order_by("first_name", "last_name", "username")[:20]

    return render(
        request=request,
        template_name="pwms/partials/user_search_results.html",
        context={"users": users},
    )


def group_search(request):
    """Search groups and return HTML results for HTMX."""
    search_query = request.GET.get("search", "")

    groups = Group.objects.all()

    if search_query:
        groups = groups.filter(Q(name__icontains=search_query))

    groups = groups.order_by("name")[:20]  # Limit to 20 results
    return render(
        request=request,
        template_name="pwms/partials/group_search_results.html",
        context={"groups": groups},
    )


def report_search(request):
    """
    Search the delegation reports the user may edit, for HTMX.

    Feeds the resolution form's parent picker: linking a resolution under a report
    edits that report's hierarchy too, so only reports the user may edit are
    offered.
    """
    search_query = request.GET.get("search", "").strip()

    reports = [
        report
        for report in DelegationReport.objects.order_by("-created_at")
        if resolve(request.user, report, EDIT)
    ]
    if search_query:
        needle = search_query.lower()
        reports = [
            report
            for report in reports
            if needle in f"{report.reference_number or ''} {report.title}".lower()
        ]

    return render(
        request=request,
        template_name="pwms/partials/report_search_results.html",
        context={"reports": reports[:20]},
    )


def country_search(request):
    """Search countries and return HTML results for HTMX."""
    search_query = request.GET.get("search", "").strip()

    countries = Country.objects.all()
    order = ["name"]
    if search_query:
        countries = countries.filter(
            Q(name__icontains=search_query) | Q(code__iexact=search_query)
        ).annotate(
            # A name that starts with the query is the likelier match: "south"
            # should offer South Africa before French Southern Territories.
            rank=Case(
                When(name__istartswith=search_query, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        order = ["rank", "name"]

    return render(
        request=request,
        template_name="pwms/partials/country_search_results.html",
        context={"countries": countries.order_by(*order)[:20]},
    )


def city_search(request):
    """
    Search cities and return HTML results for HTMX.

    The picker sends the country it is paired with as ``country``, so a city can
    only be chosen for the country that is already selected. An empty search
    lists that country's largest cities, so the box doubles as a browse control.
    """
    search_query = request.GET.get("search", "").strip()
    country_id = request.GET.get("country", "").strip()

    cities = City.objects.select_related("country")
    if country_id.isdigit():
        cities = cities.filter(country_id=int(country_id))
    if search_query:
        cities = cities.filter(
            Q(name__istartswith=search_query) | Q(ascii_name__istartswith=search_query)
        )

    # Largest places first, which also keeps village namesakes out of the way.
    return render(
        request=request,
        template_name="pwms/partials/city_search_results.html",
        context={"cities": cities.order_by("-population", "name")[:20]},
    )


# --- Attachments (SharePoint documents) -------------------------------------
# The picker is a lazy HTMX flow: a detail page renders the attachment list plus
# an "Add attachment" button, which swaps in the SharePoint browser. Every
# endpoint names its target record with a ``content_type`` ("app.model") plus an
# ``object_id``, so one set of views serves every concrete workflow subclass.


def _attachment_target(request, action):
    """Resolve a request's ``content_type``/``object_id`` to an allowed record."""
    raw_type = request.GET.get("content_type") or request.POST.get("content_type")
    object_id = request.GET.get("object_id") or request.POST.get("object_id")
    app_label, separator, model_name = (raw_type or "").partition(".")
    content_type = (
        ContentType.objects.filter(app_label=app_label, model=model_name).first()
        if separator
        else None
    )
    model = content_type.model_class() if content_type is not None else None
    if model is None or not object_id:
        raise Http404("Unknown attachment target.")
    target = get_object_or_404(model, pk=object_id)
    require(request.user, target, action)
    return target


def _attachment_context(request, target):
    """Template context for the attachment section/partials of one record."""
    meta = target._meta
    attachments = attachments_service.attachments_for(target)
    return {
        "attachment_target": target,
        "attachment_content_type": f"{meta.app_label}.{meta.model_name}",
        "attachment_object_id": str(target.pk),
        "attachments": attachments,
        "attachment_events": attachments_service.attachment_activity(target),
        # Which documents an activity row names are still attached: those open
        # through the record's own endpoint, while a detached document keeps only
        # the snapshot URL its event recorded.
        "live_attachment_ids": {
            str(attachment.public_id) for attachment in attachments
        },
        "can_attach": resolve(request.user, target, EDIT),
    }


def attachment_browser(request):
    """Render the SharePoint document picker for a record (HTMX fragment)."""
    target = _attachment_target(request, EDIT)
    context = _attachment_context(request, target)
    context["attachment_sites"] = attachments_service.member_sites(request.user)
    return render(request, "pwms/partials/attachment_browser.html", context)


def attachment_tree_children(request):
    """One tree level: a site's drives, or a folder's subfolders."""
    target = _attachment_target(request, EDIT)
    context = _attachment_context(request, target)
    folder_id = request.GET.get("folder_id") or attachments_service.ROOT
    drive_id = request.GET.get("drive_id")
    try:
        if drive_id:
            drive = attachments_service.require_drive_access(request.user, drive_id)
            subfolders, _ = attachments_service.folder_children(
                drive.drive_id, folder_id
            )
            context["attachment_subfolders"] = subfolders
            context["attachment_drive_id"] = drive.drive_id
            context["attachment_parent_folder_id"] = folder_id
        else:
            site = attachments_service.require_site_access(
                request.user, request.GET.get("site_id")
            )
            context["attachment_drives"] = attachments_service.site_drives(site)
    except attachments_service.AttachmentError as exc:
        context["attachment_error"] = str(exc)
    return render(request, "pwms/partials/attachment_tree_children.html", context)


def attachment_folder(request):
    """The files in a selected folder, with the upload form aimed at it."""
    target = _attachment_target(request, EDIT)
    context = _attachment_context(request, target)
    folder_id = request.GET.get("folder_id") or attachments_service.ROOT
    context.update(
        {
            "attachment_folder_id": folder_id,
            "attachment_folder_name": request.GET.get("folder_name") or "Root",
            "attachment_parent_folder_id": request.GET.get("parent_folder_id") or "",
            "attachment_types": Attachment.ATTACHMENT_TYPE_CHOICES,
        }
    )
    try:
        drive = attachments_service.require_drive_access(
            request.user, request.GET.get("drive_id")
        )
        _, files = attachments_service.folder_children(drive.drive_id, folder_id)
    except attachments_service.AttachmentError as exc:
        context["attachment_error"] = str(exc)
        return render(request, "pwms/partials/attachment_folder_contents.html", context)
    context["attachment_drive"] = drive
    context["attachment_files"] = files
    return render(request, "pwms/partials/attachment_folder_contents.html", context)


@require_POST
def attachment_link(request):
    """Attach an existing SharePoint document to the current record."""
    target = _attachment_target(request, EDIT)
    try:
        drive = attachments_service.require_drive_access(
            request.user, request.POST.get("drive_id")
        )
        folder = attachments_service.resolve_folder(
            drive,
            request.POST.get("folder_id"),
            request.POST.get("folder_name", ""),
            request.POST.get("parent_folder_id", ""),
        )
        attachment = attachments_service.link_document(
            obj=target,
            user=request.user,
            drive=drive,
            item_id=request.POST.get("item_id", ""),
            folder=folder,
            attachment_type=request.POST.get("type", "document"),
        )
    except attachments_service.AttachmentError as exc:
        return _attachment_response(request, target, error=str(exc))
    return _attachment_response(
        request, target, success=f'"{attachment.name}" attached.'
    )


@require_POST
def attachment_upload(request):
    """Upload a file into the selected folder and attach it in one step."""
    target = _attachment_target(request, EDIT)
    upload = request.FILES.get("file")
    if upload is None:
        return _attachment_response(request, target, error="No file was selected.")
    try:
        drive = attachments_service.require_drive_access(
            request.user, request.POST.get("drive_id")
        )
        folder = attachments_service.resolve_folder(
            drive,
            request.POST.get("folder_id"),
            request.POST.get("folder_name", ""),
            request.POST.get("parent_folder_id", ""),
        )
        attachment, created = attachments_service.upload_document(
            obj=target,
            user=request.user,
            drive=drive,
            filename=upload.name,
            content=upload.read(),
            folder=folder,
            attachment_type=request.POST.get("type", "document"),
        )
    except attachments_service.AttachmentError as exc:
        return _attachment_response(request, target, error=str(exc))
    if created:
        message = f'"{attachment.name}" uploaded and attached.'
    else:
        # SharePoint kept the existing item and added a version to it.
        message = f'"{attachment.name}" uploaded as a new version.'
    return _attachment_response(request, target, success=message)


@require_POST
def attachment_delete(request, public_id):
    """Detach a document from the record, leaving the file in SharePoint."""
    attachment = get_object_or_404(Attachment, public_id=public_id)
    target = attachment.content_object
    if target is None:
        raise Http404("Attachment is not linked to a record.")
    require(request.user, target, EDIT)
    name = attachment.name
    # Detach through the service so the removal lands on the audit trail.
    attachments_service.detach_document(attachment, actor=request.user)
    return _attachment_response(request, target, success=f'"{name}" removed.')


def attachment_versions(request, public_id):
    """Version history for one attachment, refreshed from SharePoint."""
    attachment = get_object_or_404(Attachment, public_id=public_id)
    target = attachment.content_object
    if target is None:
        raise Http404("Attachment is not linked to a record.")
    require(request.user, target, VIEW)
    context = _attachment_context(request, target)
    context["attachment"] = attachment
    try:
        context["versions"] = attachments_service.sync_versions(attachment)
    except attachments_service.AttachmentError as exc:
        # Show whatever was mirrored last rather than an empty panel.
        context["attachment_error"] = str(exc)
        context["versions"] = attachment.versions.all()
    return render(request, "pwms/partials/attachment_versions.html", context)


def attachment_version_download(request, public_id):
    """Send the browser to SharePoint's pre-authenticated URL for one version."""
    version = get_object_or_404(AttachmentVersion, public_id=public_id)
    attachment = version.attachment
    target = attachment.content_object
    if target is None:
        raise Http404("Attachment is not linked to a record.")
    require(request.user, target, VIEW)
    try:
        url = attachments_service.version_download_url(attachment, version)
    except attachments_service.AttachmentError as exc:
        messages.error(request, str(exc))
        return redirect(target.get_absolute_url() or reverse("pwms:dashboard"))
    return HttpResponseRedirect(url)


def attachment_open(request, public_id):
    """Send the browser to a fresh pre-authenticated URL for the document.

    This is what the attachment's name links to. The document opens under the
    application's own authorisation — see
    :func:`pwms.services.attachments.open_url` — so the reader is never asked to
    sign in to SharePoint. VIEW on the owning record is required, exactly as for
    its detail page, so the endpoint can only hand out documents the reader could
    already open.
    """
    attachment = get_object_or_404(Attachment, public_id=public_id)
    target = attachment.content_object
    if target is None:
        raise Http404("Attachment is not linked to a record.")
    require(request.user, target, VIEW)
    try:
        url = attachments_service.open_url(attachment)
    except attachments_service.AttachmentError as exc:
        messages.error(request, str(exc))
        return redirect(target.get_absolute_url() or reverse("pwms:dashboard"))
    return HttpResponseRedirect(url)


def _attachment_response(request, target, *, success="", error=""):
    """Re-render the attachment list, announcing a change when there is one."""
    context = _attachment_context(request, target)
    context.update(_record_counters(target))
    if error:
        context["attachment_status"] = {"level": "danger", "message": error}
    elif success:
        context["attachment_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/attachment_list.html", context)


# --- Referrals (who a matter was sent to, and their answer) -------------------
# A referral is a typed row (WorkflowReferral) rather than a state change, so it
# is raised, answered and withdrawn from the record's Referrals tab. Every
# endpoint names its target with a ``content_type`` ("app.model") plus an
# ``object_id``, so one set of views serves every concrete workflow subclass.


def _workflow_target(request, action, *, ignore_referral_grants=False):
    """Resolve a request's ``content_type``/``object_id`` to an allowed record.

    Shared by the fragments that name their target rather than sitting on a
    type's own route, so one endpoint serves every concrete subclass.
    """
    raw_type = request.GET.get("content_type") or request.POST.get("content_type")
    object_id = request.GET.get("object_id") or request.POST.get("object_id")
    app_label, separator, model_name = (raw_type or "").partition(".")
    content_type = (
        ContentType.objects.filter(app_label=app_label, model=model_name).first()
        if separator
        else None
    )
    model = content_type.model_class() if content_type is not None else None
    if (
        model is None
        or not object_id
        or not issubclass(model, AbstractLegislativeWorkflow)
    ):
        raise Http404("Unknown referral target.")
    target = get_object_or_404(model, pk=object_id)
    require(request.user, target, action, ignore_referral_grants=ignore_referral_grants)
    return target


def _referral_response(request, target, *, success="", error="", form=None):
    """Re-render the referral panel, announcing a change when there is one."""
    context = _referral_context(request, target)
    context.update(_record_counters(target))
    if form is not None:
        # Hand the rejected form back bound, so its errors and the values already
        # typed are shown again rather than silently dropped.
        context["referral_form"] = form
    if error:
        context["referral_status"] = {"level": "danger", "message": error}
    elif success:
        context["referral_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/referral_section.html", context)


@require_POST
def referral_create(request):
    """Refer a workflow instance to a group (HTMX fragment)."""
    # Raising a referral is the record's business, not something a group the
    # record was referred to does: what a referral conferred is left out of the
    # check, so a committee cannot refer a record onward on the strength of being
    # asked to advise on it.
    target = _workflow_target(request, EDIT, ignore_referral_grants=True)
    state = target.current_state
    if state is not None and not state.allows_referrals:
        return _referral_response(
            request, target, error=f"Referrals are not allowed in state {state.name}."
        )
    form = ReferralForm(request.POST)
    if not form.is_valid():
        return _referral_response(request, target, form=form)
    referral = target.refer(
        form.cleaned_data["referred_to"],
        referred_by=request.user,
        due_date=form.cleaned_data["due_date"],
        notes=form.cleaned_data["notes"],
    )
    return _referral_response(
        request, target, success=f"Referred to {referral.referred_to.name}."
    )


def _referral_for_action(request, public_id, allowed):
    """Load a referral and its record, refusing unless ``allowed`` permits."""
    referral = get_object_or_404(
        WorkflowReferral.objects.select_related("referred_to"), public_id=public_id
    )
    target = referral.content_object
    if target is None:
        raise Http404("Referral is not linked to a record.")
    if not allowed(request.user, referral, target):
        raise PermissionDenied(_("You may not change this referral."))
    return referral, target


@require_POST
def referral_respond(request, public_id):
    """Record the referred group's answer (HTMX fragment)."""
    referral, target = _referral_for_action(request, public_id, _can_answer_referral)
    try:
        referral.respond(
            responded_by=request.user,
            document_url=request.POST.get("document_url", "").strip(),
            notes=request.POST.get("notes", "").strip(),
        )
    except ValidationError as exc:
        return _referral_response(request, target, error=exc.messages[0])
    return _referral_response(
        request, target, success="The referral has been answered."
    )


@require_POST
def referral_recall(request, public_id):
    """Withdraw a referral (HTMX fragment)."""
    referral, target = _referral_for_action(request, public_id, _can_recall_referral)
    try:
        referral.recall(
            recalled_by=request.user,
            reason=request.POST.get("reason", "").strip(),
        )
    except ValidationError as exc:
        return _referral_response(request, target, error=exc.messages[0])
    return _referral_response(
        request, target, success="The referral has been withdrawn."
    )


# --- Notes (what people recorded against a record) ---------------------------
# A note is a typed row (WorkflowNote) rather than a state change or a column on
# the record, so each one keeps its own author and timestamp and any instrument
# can carry them. There is no lifecycle: a note is written, read, and deleted if
# it was recorded in error.


def _notes_response(request, target, *, success="", error="", form=None):
    """Re-render the notes panel, announcing a change when there is one."""
    context = _notes_context(request, target)
    context.update(_record_counters(target))
    if form is not None:
        context["notes_form"] = form
    if error:
        context["notes_status"] = {"level": "danger", "message": error}
    elif success:
        context["notes_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/notes_section.html", context)


@require_POST
@require_POST
def workflow_notes_add(request):
    """Record a note against a record (HTMX fragment)."""
    target = _workflow_target(request, EDIT)
    form = WorkflowNoteForm(request.POST)
    if not form.is_valid():
        return _notes_response(request, target, form=form)
    note = form.save(commit=False)
    note.content_object = target
    note.author = request.user
    note.save()
    return _notes_response(request, target, success="Note added.")


def _note_for_change(request, public_id):
    """Load a note and its record, refusing unless the reader may change it."""
    note = get_object_or_404(
        WorkflowNote.objects.select_related("author"), public_id=public_id
    )
    target = note.content_object
    if target is None:
        raise Http404("Note is not linked to a record.")
    if not _can_change_note(request.user, note, target):
        raise PermissionDenied(_("You may not change this note."))
    return note, target


@require_POST
def workflow_note_edit(request, public_id):
    """Rewrite one note (HTMX fragment).

    An empty body is the only way this can fail, and the note itself is left
    untouched, so the refusal is reported as a status message rather than by
    re-rendering the row's own form.
    """
    note, target = _note_for_change(request, public_id)
    form = WorkflowNoteForm(request.POST, instance=note)
    if not form.is_valid():
        return _notes_response(request, target, error="A note cannot be empty.")
    form.save()
    return _notes_response(request, target, success="Note updated.")


@require_POST
def workflow_note_delete(request, public_id):
    """Delete one note (HTMX fragment)."""
    note, target = _note_for_change(request, public_id)
    note.delete()
    return _notes_response(request, target, success="Note deleted.")


# --- Delegation participants (BR02.3.7/8) ------------------------------------


def _participants_response(request, report, *, success="", error="", form=None):
    """Re-render the participant table, announcing a change when there is one."""
    context = _participants_context(request, report)
    context.update(_record_counters(report))
    if form is not None:
        context["participant_form"] = form
    if error:
        context["participants_status"] = {"level": "danger", "message": error}
    elif success:
        context["participants_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/report_participants.html", context)


@require_POST
def delegation_report_participant_add(request, public_id):
    """Add one participant to a delegation report (HTMX fragment)."""
    report = get_object_or_404(DelegationReport, public_id=public_id)
    require(request.user, report, EDIT)
    form = DelegationParticipantAdderForm(
        request.POST,
        instance=DelegationParticipant(delegation_report=report),
        report=report,
    )
    if not form.is_valid():
        return _participants_response(request, report, form=form)
    participant = form.save()
    return _participants_response(
        request, report, success=f"{participant.full_name} added to the delegation."
    )


@require_POST
def delegation_report_participant_delete(request, public_id):
    """Take one participant off a delegation report (HTMX fragment).

    The row is kept, marked removed with who did it and when, so the report
    still shows who was on the delegation.
    """
    participant = get_object_or_404(DelegationParticipant, public_id=public_id)
    report = participant.delegation_report
    require(request.user, report, EDIT)
    name = participant.full_name
    participant.remove(by=request.user)
    return _participants_response(request, report, success=f"{name} removed.")


# --- Adopted resolutions (BR02.3.9) ------------------------------------------


def _resolutions_response(request, report, *, success="", error="", form=None):
    """Re-render the resolution card, announcing a change when there is one."""
    context = _resolutions_context(request, report)
    context.update(_record_counters(report))
    if form is not None:
        context["resolution_form"] = form
    if error:
        context["resolutions_status"] = {"level": "danger", "message": error}
    elif success:
        context["resolutions_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/report_resolutions.html", context)


@require_POST
def delegation_report_resolution_add(request, public_id):
    """Capture one resolution adopted at the engagement (HTMX fragment).

    The resolution is created as an international resolution and nested under the
    report, exactly as the report form's resolutions section does. Creating one is
    a *create* action, so it takes the role that may create resolutions as well as
    the edit right on the report it is linked to — the same pair the button in
    ``_resolutions_context`` is hidden behind.
    """
    report = get_object_or_404(DelegationReport, public_id=public_id)
    require(request.user, report, EDIT)
    resolution_type = _creatable_form_type(request.user, InternationalResolutionForm)
    if resolution_type is None:
        raise PermissionDenied(
            "You do not have a role that may create international resolutions."
        )
    form = ChildResolutionForm(request.POST)
    if not form.is_valid():
        return _resolutions_response(request, report, form=form)
    with transaction.atomic():
        resolution = _create_child_resolution(
            report, form, request.user, resolution_type
        )
    return _resolutions_response(
        request,
        report,
        success=f"Resolution {resolution.resolution_number} added to the report.",
    )
