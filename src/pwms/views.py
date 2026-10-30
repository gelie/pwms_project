import calendar
import logging
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import ClassVar
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import AuthenticationForm, UsernameField
from django.contrib.auth.views import LoginView as BaseLoginView
from django.contrib.auth.views import LogoutView as BaseLogoutView
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
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
    MeetingAgendaForm,
    MeetingAttendanceAdderForm,
    MeetingAttendanceRecordForm,
    MeetingForm,
    MeetingImportForm,
    ReferralForm,
    ReportShareForm,
    ResolutionAdderForm,
    WorkflowEventForm,
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
    Meeting,
    MeetingAttendance,
    Notification,
    TransitionLog,
    User,
    WorkflowNote,
    WorkflowReferral,
    WorkflowType,
)
from .notifications import (
    notify_meeting_attendance,
    notify_meeting_created,
    notify_workflow_created,
)
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
from .services.meetings_io import (
    MEETING_COLUMNS,
    ImportFormatError,
    export_response,
    import_meetings,
    template_response,
)
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
from .services.search import count_hits, search_site
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


#: Concrete workflow models shown on the unified register, in display order.
#: Each concrete model owns one ``WorkflowType``; the unified list spans them all.
_WORKFLOW_MODELS = (
    DelegationReport,
    InternationalResolution,
    InternationalAgreement,
    Bill,
    Meeting,
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


def _text_search(query, *fields):
    """A ``Q`` matching every whitespace-separated token of ``query`` in any field.

    Token-AND rather than one substring: "john smith" finds a person whose first
    name is John and surname Smith, and each token may land in a different field.
    That is the free-text behaviour the listings used to implement in Python,
    expressed as a database predicate so the page can be sliced with
    ``LIMIT``/``OFFSET``. A blank query yields an empty ``Q``, matching anything.
    """
    match = Q()
    for token in query.split():
        token_match = Q()
        for field in fields:
            token_match |= Q(**{f"{field}__icontains": token})
        match &= token_match
    return match


def _directory_page_sizes():
    """The page sizes the directory listings offer, smallest first.

    The configured default is folded in, so a deployment that sets a size the
    choices do not list still offers it (and it stays the selected one).
    """
    return sorted({*settings.DIRECTORY_PAGE_SIZES, settings.DIRECTORY_PAGE_SIZE})


def _directory_page_size(request):
    """The page size for this request: an offered ``per_page``, else the default."""
    raw = request.GET.get("per_page", "")
    if raw.isdigit() and int(raw) in _directory_page_sizes():
        return int(raw)
    return settings.DIRECTORY_PAGE_SIZE


def _directory_page(request, queryset):
    """Paginate a queryset in the database, and hand its links the filters.

    Returns the context a directory template needs: ``page_obj`` (the requested
    page, sliced by the database rather than after reading every row),
    ``page_range`` (the page numbers to offer, elided when there are many),
    ``base_query`` (the filters as a query string with ``page`` stripped),
    ``per_page`` and ``page_sizes`` (for the "Per page" control). Page links
    append ``page=N`` to ``base_query``, so moving page keeps the filters and the
    filters themselves (submitted without ``page``) reset to the first page.
    ``get_page`` tolerates a missing, non-numeric or out-of-range ``page`` rather
    than raising, so the listing cannot 404 on a hand-edited URL.
    """
    per_page = _directory_page_size(request)
    paginator = Paginator(queryset, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return {
        "page_obj": page_obj,
        # Called here because a template cannot pass arguments to a method.
        "page_range": paginator.get_elided_page_range(page_obj.number),
        "base_query": params.urlencode(),
        "per_page": per_page,
        "page_sizes": _directory_page_sizes(),
    }


def all_groups(request):
    """List every group in the organisational hierarchy (sign-in required).

    The list is narrowed by a free-text name filter (matched against the name and
    short name, as in the admin) and a group-type filter, both applied in the
    database so only the requested page's rows are read. The type options are
    derived from the types in use rather than the full choice list, so the filter
    cannot advertise a type no group uses.
    """
    groups = Group.objects.select_related("parent").order_by("name")

    query = request.GET.get("q", "").strip()
    type_filter = request.GET.get("type", "")

    if query:
        groups = groups.filter(_text_search(query, "name", "short_name"))
    if type_filter:
        groups = groups.filter(group_type=type_filter)

    in_use = set(Group.objects.values_list("group_type", flat=True))
    context = {
        **_directory_page(request, groups),
        "query": query,
        "type_choices": [
            (value, label)
            for value, label in Group.GROUP_TYPE_CHOICES
            if value in in_use
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


def user_detail(request, pk):
    """Show one person's profile: who they are and the groups they belong to.

    A directory card, not an HR record. The User model also holds identity and
    personnel fields (ID number, date of birth, gender, employee id, supervisor,
    termination date, identity source) that are deliberately left off the page:
    it is readable by any signed-in user — the same audience that may view a
    group's member list — so only the professional details are shown.
    """
    person = get_object_or_404(User.objects.select_related("department"), pk=pk)
    memberships = (
        person.get_groups_with_roles()
        .select_related("group__parent", "role")
        .annotate(
            status=Case(
                When(is_active=False, then=Value("Ended")),
                When(end_date__lt=timezone.localdate(), then=Value("Ended")),
                default=Value("Active"),
                output_field=CharField(),
            )
        )
        .order_by("-is_active", "-start_date", "group__name", "role__name")
    )
    context = {
        "person": person,
        "memberships": memberships,
        # Current groups the person sits in; ended rows still show below, but the
        # headline and the status badges count only the live ones.
        "active_membership_count": sum(1 for m in memberships if m.status == "Active"),
        "current_portfolio": person.current_portfolio,
    }
    return render(request, "pwms/user-detail.html", context)


#: Kind filter for the people directory: value -> a ``Q`` narrowing to that kind.
PEOPLE_KIND_FILTERS = {
    "mp": Q(is_mp=True),
    "staff": Q(is_staff_member=True),
}

#: Status filter: value -> a ``Q`` narrowing to that status.
PEOPLE_STATUS_FILTERS = {
    "active": Q(is_active=True),
    "former": Q(is_active=False),
}


def people(request):
    """Browsable directory of the people in PWMS (sign-in required).

    Mirrors the group list: a free-text filter (name, username, position or
    department) plus kind and status selects, all applied in the database so only
    the requested page's rows are read. Current accounts show by default, so
    former staff do not clutter the browse — they stay reachable through the
    status filter and from any record that names them.
    """
    everyone = User.objects.select_related("department").order_by(
        "last_name", "first_name", "username"
    )

    query = request.GET.get("q", "").strip()
    kind = request.GET.get("kind", "")
    status = request.GET.get("status", "active")

    kind_q = PEOPLE_KIND_FILTERS.get(kind)
    if kind_q is not None:
        everyone = everyone.filter(kind_q)
    status_q = PEOPLE_STATUS_FILTERS.get(status)
    if status_q is not None:
        everyone = everyone.filter(status_q)
    if query:
        everyone = everyone.filter(
            _text_search(
                query,
                "first_name",
                "last_name",
                "username",
                "positiondesc",
                "department__name",
            )
        )

    context = {
        **_directory_page(request, everyone),
        "query": query,
        "kind": kind,
        "status": status,
        "kind_choices": (
            ("", "All people"),
            ("mp", "Members of Parliament"),
            ("staff", "Staff"),
        ),
        "status_choices": (
            ("active", "Current only"),
            ("all", "Current and former"),
            ("former", "Former only"),
        ),
        "has_filters": bool(query or kind or status != "active"),
    }
    return render(request, "pwms/all-people.html", context)


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
    if isinstance(instance, Meeting):
        return reverse("pwms:meeting_detail", kwargs={"public_id": instance.public_id})
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

    ``timeline`` is the merged TransitionLog + WorkflowEvent + auditlog history
    (see :mod:`pwms.services.history`); ``diagram_*`` drives the Diagram tab,
    which serves the image ``manage.py generate_diagrams`` wrote for the type.

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
    context.update(_events_context(request, instance))
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


def global_search(request):
    """Site-wide search across every register the reader may see.

    Returns the categorized results fragment for an HTMX request, which the
    header swaps into its search panel, and a full results page otherwise — so
    the box still works, and its results are still linkable, without JavaScript.
    """
    query = request.GET.get("q", "").strip()
    categories = search_site(request.user, query)
    context = {
        "query": query,
        "categories": categories,
        "total": count_hits(categories),
    }
    template_name = (
        "pwms/partials/global_search_results.html"
        if request.headers.get("HX-Request") == "true"
        else "pwms/search-results.html"
    )
    return render(request, template_name, context)


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
    # Preselect an attachment type (a meeting preselects agenda or minutes), so
    # the picker files the document under the right classification by default.
    context["attachment_default_type"] = request.GET.get("default_type", "")
    return render(request, "pwms/partials/attachment_browser.html", context)


def attachment_tree_children(request):
    """One tree level: a site's drives, or a folder's subfolders."""
    target = _attachment_target(request, EDIT)
    context = _attachment_context(request, target)
    context["attachment_default_type"] = request.GET.get("default_type", "")
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
            "attachment_default_type": request.GET.get("default_type", ""),
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


# --- Domain events (facts recorded against a record) -------------------------
# A domain event is an append-only row (``WorkflowEvent``) typed by the
# ``EventType`` registry, so it is evidence rather than state: it can guard a
# transition and it feeds the merged Timeline. Unlike a note it is never
# corrected in place — a mistake is answered with a compensating event — so this
# panel only ever adds.


def _events_context(request, instance):
    """
    Template context for a record's domain-event panel and its recorder.

    ``event_rows`` is the instance's event log, newest first, shown to any reader
    who may view the record. ``can_record_event`` folds the edit right into
    whether the recorder is offered, because an event is evidence *on* the record
    — writing one is an edit of the record, not a comment on it.
    """
    can_record = resolve(request.user, instance, EDIT)
    context = {
        **_target_identifiers(instance),
        "event_rows": list(instance.events()),
        "can_record_event": can_record,
    }
    if can_record:
        context["event_form"] = WorkflowEventForm()
    return context


def _events_response(request, target, *, success="", error="", form=None):
    """Re-render the events panel, announcing a change when there is one."""
    context = _events_context(request, target)
    context.update(_record_counters(target))
    if form is not None:
        # Hand the rejected form back bound, so its errors and the values already
        # typed are shown again rather than silently dropped.
        context["event_form"] = form
    if error:
        context["events_status"] = {"level": "danger", "message": error}
    elif success:
        context["events_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/events_section.html", context)


@require_POST
def workflow_event_add(request):
    """Record a domain event against a record (HTMX fragment)."""
    target = _workflow_target(request, EDIT)
    form = WorkflowEventForm(request.POST)
    if not form.is_valid():
        return _events_response(request, target, form=form)
    event_type = form.cleaned_data["event_type"]
    # ``record_event`` is the model's own append path: it stamps the actor and
    # origin and never updates an existing row.
    target.record_event(
        event_type,
        actor=request.user,
        notes=form.cleaned_data["notes"],
        document_url=form.cleaned_data["document_url"],
    )
    return _events_response(request, target, success=f"Recorded “{event_type.name}”.")


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


# --- Meetings (the Events workflow) -----------------------------------------
# The main page is a colour-coded calendar with three layouts — month, week and
# day (``?view=``) — over the meetings the signed-in user may view, with HTMX
# navigation, type/mine/text filters, a day panel and a "next up" strip. A JSON
# feed over the same permission-scoped query is exposed for interactive consumers
# (``meeting_calendar_json``).

#: Weekday labels, in Python's weekday order (Monday == 0). The grid's columns
#: are rotated to the configured week start; the same order is what
#: ``calendar.Calendar(firstweekday=…)`` expects.
CALENDAR_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CALENDAR_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

#: Weekday names / abbreviations → index (Monday == 0), for parsing the calendar
#: week-start setting and the ``week_start`` query parameter.
_WEEKDAY_INDEX = {
    name.lower(): index for index, name in enumerate(CALENDAR_WEEKDAY_NAMES)
}
_WEEKDAY_INDEX.update(
    {abbr.lower(): index for index, abbr in enumerate(CALENDAR_WEEKDAYS)}
)

#: How many meetings the "next up" strip on the calendar page shows.
MEETING_AGENDA_ROWS = 8

#: The calendar's three layouts. ``month`` is a dated grid; ``week`` and ``day``
#: lay the meetings on a shared time axis.
CALENDAR_VIEWS = ("month", "week", "day")

#: The time axis of the week/day views: a comfortable working-day window that is
#: widened to fit earlier or later meetings, and the height of one hour on it.
CALENDAR_DEFAULT_START_HOUR = 7
CALENDAR_DEFAULT_END_HOUR = 19
CALENDAR_HOUR_PX = 44


def _meeting_create_url(day=None):
    """The "new meeting" form, optionally prefilled for ``day``."""
    base = reverse("pwms:meeting_create")
    if day is None:
        return base
    return f"{base}?{urlencode({'date': day.isoformat()})}"


def _parse_month(value, today):
    """Parse a ``YYYY-MM`` query parameter into the first of that month."""
    if value:
        parts = value.split("-")
        if len(parts) == 2:
            try:
                return date(int(parts[0]), int(parts[1]), 1)
            except TypeError, ValueError:
                pass
    return date(today.year, today.month, 1)


def _shift_month(first, delta):
    """The first of the month ``delta`` months away from ``first``."""
    index = first.year * 12 + (first.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def _parse_day(value, fallback):
    """Parse a ``YYYY-MM-DD`` query parameter into a :class:`~datetime.date`."""
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return fallback


def _calendar_view(request):
    """The requested calendar layout, defaulting to the month grid."""
    view = request.GET.get("view", "month")
    return view if view in CALENDAR_VIEWS else "month"


def _week_start_index(value):
    """Resolve a week-start day to a weekday index (Monday == 0).

    Accepts a weekday name or abbreviation (``"Sunday"``/``"sun"``) or an int
    ``0``–``6``; anything unrecognised falls back to Monday so a bad setting can
    never break the grid.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 0 <= value <= 6 else 0
    key = str(value or "").strip().lower()
    if key.isdigit():
        index = int(key)
        return index if 0 <= index <= 6 else 0
    return _WEEKDAY_INDEX.get(key, 0)


def _calendar_week_start(request):
    """The day the calendar's weeks begin on.

    A ``week_start`` query parameter (the toolbar control) wins, so a reader can
    flip it for themselves; otherwise the site-wide
    ``settings.MEETING_CALENDAR_WEEK_START`` applies, defaulting to Monday.
    """
    override = request.GET.get("week_start")
    if override:
        return _week_start_index(override)
    return _week_start_index(getattr(settings, "MEETING_CALENDAR_WEEK_START", "monday"))


def _minutes_since_midnight(moment):
    """Minutes past local midnight for a timezone-aware datetime."""
    local = timezone.localtime(moment)
    return local.hour * 60 + local.minute


def _time_window(meetings):
    """The ``(start_hour, end_hour)`` the time axis must show to fit ``meetings``.

    Starts from a comfortable working-day window and grows to cover the earliest
    start and latest end, clamped to the clock. A meeting without an end time is
    still given a sensible slot. Days with no timed meetings keep the default.
    """
    start_hour = CALENDAR_DEFAULT_START_HOUR
    end_hour = CALENDAR_DEFAULT_END_HOUR
    for meeting in meetings:
        if meeting.all_day or meeting.start_datetime is None:
            continue
        start_min = _minutes_since_midnight(meeting.start_datetime)
        end_min = _minutes_since_midnight(meeting.end or meeting.start_datetime)
        if end_min <= start_min:
            end_min = start_min + 60
        start_hour = min(start_hour, start_min // 60)
        end_hour = max(end_hour, -(-end_min // 60))
    start_hour = max(0, min(start_hour, 23))
    end_hour = max(start_hour + 1, min(end_hour, 24))
    return start_hour, end_hour


def _assign_lanes(events):
    """Stack overlapping events into side-by-side lanes so none is hidden.

    ``events`` are dicts carrying ``start_min``/``end_min``; each is given a
    ``lane`` and the run's ``lanes`` count. Non-overlapping events reuse a lane
    and therefore stay full width.
    """
    lane_ends = []
    for event in events:
        for lane, lane_end in enumerate(lane_ends):
            if event["start_min"] >= lane_end:
                event["lane"] = lane
                lane_ends[lane] = event["end_min"]
                break
        else:
            event["lane"] = len(lane_ends)
            lane_ends.append(event["end_min"])
    total = max(1, len(lane_ends))
    for event in events:
        event["lanes"] = total
    return events


def _positioned_events(meetings, start_hour, end_hour):
    """Lay timed meetings on the axis as ``top``/``height`` percentages."""
    total = (end_hour - start_hour) * 60
    events = []
    for meeting in meetings:
        if meeting.all_day or meeting.start_datetime is None:
            continue
        start_min = _minutes_since_midnight(meeting.start_datetime)
        end_min = _minutes_since_midnight(meeting.end or meeting.start_datetime)
        if end_min <= start_min:
            end_min = start_min + 45
        top = max(0, start_min - start_hour * 60)
        height = min(total - top, max(30, end_min - start_min))
        events.append(
            {
                "meeting": meeting,
                "start_min": start_min,
                "end_min": end_min,
                "top": round(top / total * 100, 3),
                "height": round(height / total * 100, 3),
                "lane": 0,
                "lanes": 1,
            }
        )
    return _assign_lanes(events)


def _range_label(start, end):
    """A compact date-range label for the week view, e.g. ``6–12 October 2026``."""
    if start.year != end.year:
        return f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"
    if start.month != end.month:
        return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
    return f"{start.strftime('%d')}–{end.strftime('%d %B %Y')}"


def _week_aligned_range(start, end, week_start):
    """Widen ``[start, end]`` to whole weeks beginning on ``week_start``.

    The month grid draws the weeks that *contain* the month, so the feed mirrors
    that coverage: from the week-start day on or before ``start`` to the day
    before the next week start on or after ``end``.
    """
    first = start - timedelta(days=(start.weekday() - week_start) % 7)
    last = end + timedelta(days=(week_start - 1 - end.weekday()) % 7)
    return first, last


def _calendar_ordered(meetings, week_start):
    """Order meetings the way the calendar reads them.

    Days run from the configured week start (so a week reads Mon→Sun or
    Sun→Sat), and within a day meetings run by time. The week-start component
    makes the ordering explicit in one place, shared by the grid's day cells,
    the day panel and the JSON feed; across a date range it is the natural
    chronological order, just anchored to the week start.
    """

    def sort_key(meeting):
        local = timezone.localtime(meeting.start_datetime)
        week_anchor = local.date() - timedelta(days=(local.weekday() - week_start) % 7)
        return (week_anchor, local, meeting.title)

    return sorted(meetings, key=sort_key)


def _meeting_create_initial(params):
    """Prefill a new meeting's schedule from a calendar ``date``/``time`` link."""
    day = _parse_day(params.get("date"), None)
    if day is None:
        return {}
    start_time = time(9, 0)
    raw_time = params.get("time", "")
    if raw_time:
        try:
            start_time = time.fromisoformat(raw_time)
        except ValueError:
            start_time = time(9, 0)
    start = timezone.make_aware(datetime.combine(day, start_time))
    return {"start_datetime": start, "end_datetime": start + timedelta(hours=2)}


def _meeting_type_choices():
    return Meeting.MEETING_TYPE_CHOICES


def _meeting_query(request, start=None, end=None):
    """The permission-scoped meetings the filters select.

    ``start``/``end`` (inclusive dates) restrict the window when both are given;
    the calendar passes its grid range and the export passes none (the whole set
    the reader may see).
    """
    queryset = Meeting.objects.select_related(
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
        "host_group",
        "chaired_by",
        "venue",
        "venue__building",
    )
    if start is not None and end is not None:
        queryset = queryset.in_period(start, end)

    meeting_type = request.GET.get("type", "").strip()
    if meeting_type:
        queryset = queryset.filter(meeting_type=meeting_type)
    if request.GET.get("mine") in ("1", "true", "on", "yes"):
        queryset = queryset.for_member(request.user)
    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(
            Q(title__icontains=query)
            | Q(reference_number__icontains=query)
            | Q(venue__name__icontains=query)
            | Q(venue__building__name__icontains=query)
            | Q(off_site_venue__icontains=query)
            | Q(off_site_address__icontains=query)
        )
    return visible_instances(request.user, queryset)


def _meeting_export_urls(request):
    """The CSV/xlsx export links, carrying the calendar's current filters."""
    params = {
        key: request.GET.get(key)
        for key in ("type", "mine", "q")
        if request.GET.get(key)
    }
    base = reverse("pwms:meeting_export")
    return {
        export_format: f"{base}?{urlencode({**params, 'format': export_format})}"
        for export_format in ("xlsx", "csv")
    }


def _meeting_calendar_context(request):
    """Everything the calendar page's layouts, filters and day panel need.

    One context serves all three layouts (``?view=month|week|day``): the month
    grid reads ``week_rows``, the week/day layouts read ``days``/``hours``. The
    period shown is anchored on ``day`` (a ``YYYY-MM-DD``) or, in the month view,
    on ``month`` (a ``YYYY-MM``), so every navigation link keeps both the window
    and the reader's filters.
    """
    today = timezone.localdate()
    view = _calendar_view(request)
    week_start = _calendar_week_start(request)

    requested_day = _parse_day(request.GET.get("day"), None)
    if requested_day is not None:
        anchor = requested_day
    else:
        anchor = _parse_month(request.GET.get("month"), today)

    # The filters (type / mine / q) and the week-start preference survive every
    # navigation, so a link only ever changes the view window rather than dropping
    # the reader's selection.
    persisted = {
        key: request.GET.get(key)
        for key in ("type", "mine", "q", "week_start")
        if request.GET.get(key)
    }

    def nav_url(**overrides):
        """A full-page calendar URL carrying the filters plus the given window.

        The page boosts its own links/filters over HTMX, selecting the calendar
        region out of the response, so the same URL is both the ``href`` (the no-
        JavaScript fallback) and the ``hx-get``.
        """
        params = dict(persisted)
        params.update({key: value for key, value in overrides.items() if value})
        return f"{reverse('pwms:meetings')}?{urlencode(params)}"

    # The days the layout shows, and the date window it must fetch. Columns and
    # weeks are rotated to the configured week start.
    weekdays = tuple(
        CALENDAR_WEEKDAYS[(week_start + offset) % 7] for offset in range(7)
    )
    weeks = []
    if view == "month":
        weeks = calendar.Calendar(firstweekday=week_start).monthdatescalendar(
            anchor.year, anchor.month
        )
        days = [day for week in weeks for day in week]
        window_start, window_end = days[0], days[-1]
    elif view == "week":
        window_start = anchor - timedelta(days=(anchor.weekday() - week_start) % 7)
        window_end = window_start + timedelta(days=6)
        days = [window_start + timedelta(days=offset) for offset in range(7)]
    else:
        window_start = window_end = anchor
        days = [anchor]

    meetings = list(_meeting_query(request, window_start, window_end))

    by_day = {}
    for meeting in meetings:
        key = timezone.localtime(meeting.start_datetime).date()
        by_day.setdefault(key, []).append(meeting)

    def day_meetings(day):
        return _calendar_ordered(by_day.get(day, []), week_start)

    def in_period(meeting):
        local = timezone.localtime(meeting.start_datetime)
        if view == "month":
            return (local.year, local.month) == (anchor.year, anchor.month)
        return window_start <= local.date() <= window_end

    period_meetings = [meeting for meeting in meetings if in_period(meeting)]

    # The time axis of the week/day layouts (fitted to the meetings shown).
    start_hour, end_hour = _time_window(meetings)

    # The month day panel: a named day, else the next day this month with a
    # meeting. Only days inside the month are offered.
    selected = None
    if view == "month":
        if requested_day is not None and (
            requested_day.year,
            requested_day.month,
        ) == (anchor.year, anchor.month):
            selected = requested_day
        elif requested_day is None:
            selected = next(
                (
                    timezone.localtime(meeting.start_datetime).date()
                    for meeting in period_meetings
                    if timezone.localtime(meeting.start_datetime).date() >= today
                ),
                timezone.localtime(period_meetings[0].start_datetime).date()
                if period_meetings
                else anchor,
            )

    week_rows = []
    if view == "month":
        for week in weeks:
            week_rows.append(
                [
                    {
                        "date": day,
                        "in_month": (day.year, day.month)
                        == (anchor.year, anchor.month),
                        "is_today": day == today,
                        "is_selected": day == selected,
                        "url": nav_url(
                            view="month",
                            month=anchor.strftime("%Y-%m"),
                            day=day.isoformat(),
                        ),
                        "create_url": _meeting_create_url(day),
                        "meetings": day_meetings(day),
                    }
                    for day in week
                ]
            )

    day_blocks = []
    if view != "month":
        for day in days:
            day_meetings_for_day = day_meetings(day)
            day_blocks.append(
                {
                    "date": day,
                    "is_today": day == today,
                    "is_weekend": day.weekday() >= 5,
                    "is_selected": day == anchor,
                    "day_url": nav_url(view="day", day=day.isoformat()),
                    "create_url": _meeting_create_url(day),
                    "meetings": day_meetings_for_day,
                    "all_day": [m for m in day_meetings_for_day if m.all_day],
                    "events": _positioned_events(
                        [m for m in day_meetings_for_day if not m.all_day],
                        start_hour,
                        end_hour,
                    ),
                }
            )

    # Period navigation and the period label, per layout.
    if view == "month":
        prev_url = nav_url(
            view="month", month=_shift_month(anchor, -1).strftime("%Y-%m")
        )
        next_url = nav_url(
            view="month", month=_shift_month(anchor, 1).strftime("%Y-%m")
        )
        today_url = nav_url(view="month", month=today.strftime("%Y-%m"))
        period_label = anchor.strftime("%B %Y")
        switch_day = selected or anchor
    elif view == "week":
        prev_url = nav_url(view="week", day=(anchor - timedelta(days=7)).isoformat())
        next_url = nav_url(view="week", day=(anchor + timedelta(days=7)).isoformat())
        today_url = nav_url(view="week", day=today.isoformat())
        period_label = _range_label(window_start, window_end)
        switch_day = anchor
    else:
        prev_url = nav_url(view="day", day=(anchor - timedelta(days=1)).isoformat())
        next_url = nav_url(view="day", day=(anchor + timedelta(days=1)).isoformat())
        today_url = nav_url(view="day", day=today.isoformat())
        period_label = anchor.strftime("%A, %d %B %Y")
        switch_day = anchor

    view_urls = {
        name: nav_url(view=name, day=switch_day.isoformat()) for name in CALENDAR_VIEWS
    }

    # A legend of the meeting types actually on show, in a stable colour per type.
    legend = []
    seen_types = set()
    for meeting in sorted(period_meetings, key=lambda item: item.start_datetime):
        if meeting.meeting_type in seen_types:
            continue
        seen_types.add(meeting.meeting_type)
        legend.append(
            {
                "label": meeting.get_meeting_type_display(),
                "color": meeting.color,
            }
        )

    return {
        "view": view,
        "anchor": anchor,
        "period_label": period_label,
        "prev_url": prev_url,
        "next_url": next_url,
        "today_url": today_url,
        "view_urls": view_urls,
        # Month layout.
        "week_rows": week_rows,
        "weekdays": weekdays,
        "selected_day": selected,
        "selected_meetings": day_meetings(selected) if selected else [],
        # Week/day layout.
        "days": day_blocks,
        "hours": list(range(start_hour, end_hour)),
        "hour_px": CALENDAR_HOUR_PX,
        "has_all_day": any(block["all_day"] for block in day_blocks),
        # The day the week starts on, and the choices for the toolbar control.
        "week_start": week_start,
        "week_start_options": [
            {"value": index, "label": name}
            for index, name in enumerate(CALENDAR_WEEKDAY_NAMES)
        ],
        # Shared.
        "legend": legend,
        "meeting_count": len(period_meetings),
        "meeting_type_choices": _meeting_type_choices(),
        "selected_type": request.GET.get("type", ""),
        "mine_only": request.GET.get("mine") in ("1", "true", "on", "yes"),
        "query": request.GET.get("q", "").strip(),
        "has_filters": bool(
            request.GET.get("type") or request.GET.get("mine") or request.GET.get("q")
        ),
        "new_meeting_url": _meeting_create_url(anchor if view != "month" else None),
        "calendar_json_url": reverse("pwms:meeting_calendar_json"),
    }


def meetings(request):
    """Calendar dashboard: the Events workflow's main page.

    A colour-coded calendar of every meeting the reader may view, in a month,
    week or day layout (``?view=``), with the filters (meeting type, "my
    meetings", free text) applied, a legend, a "next up" strip and — in the
    month view — a panel for the selected day. The calendar swaps in place over
    HTMX (``pwms:meeting_calendar``); the whole page works without JavaScript
    too, because navigation is ordinary links and every layout is server-rendered.
    """
    context = _meeting_calendar_context(request)
    upcoming = list(
        visible_instances(
            request.user,
            Meeting.objects.select_related(
                "current_state", "host_group", "venue", "venue__building"
            )
            .filter(start_datetime__gte=timezone.now())
            .order_by("start_datetime"),
        )
    )[:MEETING_AGENDA_ROWS]
    context.update(
        {
            "upcoming_meetings": upcoming,
            "can_create_meeting": _can_create_form_type(request.user, MeetingForm),
            "export_urls": _meeting_export_urls(request),
        }
    )
    return render(request, "pwms/meetings.html", context)


def meeting_calendar(request):
    """The calendar body as a standalone fragment (reusable outside the page)."""
    return render(
        request,
        "pwms/partials/meetings/calendar.html",
        _meeting_calendar_context(request),
    )


def meeting_export(request):
    """Download the reader's meetings as CSV or Excel (xlsx).

    The calendar's filters (type / mine / q) and an optional ``start``/``end``
    date window select the rows; ``?format=csv|xlsx`` (default xlsx) sets the file
    type. The download is written in the same columns the import reads, so a file
    exported here can be edited and imported back.
    """
    start = _parse_day(request.GET.get("start"), None)
    end = _parse_day(request.GET.get("end"), None)
    meetings = _calendar_ordered(
        _meeting_query(request, start, end), _calendar_week_start(request)
    )
    try:
        return export_response(meetings, request.GET.get("format", "xlsx"))
    except ValueError:
        return HttpResponseBadRequest("Unknown export format.")


def meeting_import_template(request):
    """Download a blank import template (the header row, plus a xlsx Read me)."""
    try:
        return template_response(request.GET.get("format", "csv"))
    except ValueError:
        return HttpResponseBadRequest("Unknown template format.")


def meeting_import(request):
    """Bulk import meetings from a CSV/XLSX file (a create/update upsert).

    Gated on the create capability: importing makes new meetings. Each row is
    validated on its own, so a bad row is reported and skipped rather than
    stopping the file, and the outcome (created / updated / skipped / errors) is
    shown back on the page. Nothing is emailed and nothing is deleted.
    """
    if not _can_create_form_type(request.user, MeetingForm):
        messages.error(request, "You do not have a role that may import meetings.")
        return redirect("pwms:meetings")

    form = MeetingImportForm()
    report = None
    if request.method == "POST":
        form = MeetingImportForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.cleaned_data["file"]
            try:
                report = import_meetings(
                    upload,
                    actor=request.user,
                    filename=upload.name,
                    dry_run=form.cleaned_data["dry_run"],
                )
            except ImportFormatError as error:
                form.add_error("file", str(error))
    return render(
        request,
        "pwms/meeting-import.html",
        {"form": form, "report": report, "columns": MEETING_COLUMNS},
    )


def _meeting_time_label(meeting):
    """A short time label for a meeting chip ("All day" or ``09:30–11:30``)."""
    if meeting.all_day:
        return "All day"
    if meeting.start_datetime is None:
        return ""
    start = timezone.localtime(meeting.start_datetime)
    if meeting.end_datetime:
        end = timezone.localtime(meeting.end_datetime)
        return f"{start:%H:%M}\u2013{end:%H:%M}"
    return f"{start:%H:%M}"


def _meeting_subtitle(meeting):
    """A one-line summary: meeting type, venue and hosting group, joined."""
    parts = [meeting.get_meeting_type_display()]
    venue = meeting.venue_display
    if venue:
        parts.append(venue)
    if meeting.host_group_id:
        parts.append(meeting.host_group.name)
    return " \u00b7 ".join(parts)


def meeting_calendar_json(request):
    """Meetings in a date window as JSON, scoped to what the caller may view.

    ``?start=YYYY-MM-DD&end=YYYY-MM-DD`` (both inclusive), or ``?month=YYYY-MM``.
    Each entry carries the fields an interactive calendar needs; the same
    permission chain as the grid is applied, so the feed can never disclose a
    meeting the caller could not open.

    The feed follows the calendar's week start (``?week_start=`` or
    ``settings.MEETING_CALENDAR_WEEK_START``): a bare ``?month=`` window is
    widened to the whole weeks that contain the month, exactly as the on-screen
    grid draws them, and the entries are ordered day-first from that week start;
    the response reports the ``week_start`` it used so a consumer can lay out its
    own weeks to match.
    """
    today = timezone.localdate()
    week_start = _calendar_week_start(request)
    start_param = request.GET.get("start")
    end_param = request.GET.get("end")
    if start_param:
        try:
            start = date.fromisoformat(start_param)
        except ValueError:
            return JsonResponse({"detail": "Invalid start date."}, status=400)
    else:
        first = _parse_month(request.GET.get("month"), today)
        start = first
    if end_param:
        try:
            end = date.fromisoformat(end_param)
        except ValueError:
            return JsonResponse({"detail": "Invalid end date."}, status=400)
    else:
        end = _shift_month(start.replace(day=1), 1) - timedelta(days=1)
    if not start_param and not end_param:
        # A month (or default) feed is week-aligned to match the grid's coverage.
        start, end = _week_aligned_range(start, end, week_start)

    meetings = _calendar_ordered(_meeting_query(request, start, end), week_start)
    payload = [
        {
            "public_id": str(meeting.public_id),
            "title": meeting.title,
            "reference_number": meeting.reference_number,
            "meeting_type": meeting.meeting_type,
            "meeting_type_label": meeting.get_meeting_type_display(),
            "color": meeting.color,
            "subtitle": _meeting_subtitle(meeting),
            "time_label": _meeting_time_label(meeting),
            "start_datetime": meeting.start_datetime.isoformat()
            if meeting.start_datetime
            else None,
            "end_datetime": meeting.end_datetime.isoformat()
            if meeting.end_datetime
            else None,
            "all_day": meeting.all_day,
            "attendance_mode": meeting.attendance_mode,
            "attendance_mode_label": meeting.get_attendance_mode_display(),
            "venue": meeting.venue_display,
            "off_site_venue": meeting.off_site_venue,
            "off_site_address": meeting.off_site_address,
            "state": meeting.current_state.name if meeting.current_state_id else "",
            "calendar_state": meeting.calendar_state,
            "host_group": meeting.host_group.name if meeting.host_group_id else "",
            "url": _workflow_detail_url(meeting),
        }
        for meeting in meetings
    ]
    return JsonResponse(
        {
            "count": len(payload),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "week_start": week_start,
            "week_start_label": CALENDAR_WEEKDAY_NAMES[week_start],
            "results": payload,
        }
    )


def _meeting_detail_model():
    return Meeting.objects.select_related(
        "workflow_type",
        "workflow_type__group",
        "current_state",
        "owner",
        "assigned_to",
        "host_group",
        "chaired_by",
        "secretary",
        "venue",
        "venue__building",
    )


def _attendance_record_form(attendance):
    """Per-row form, prefixed by the row's pk so many rows can share a page."""
    return MeetingAttendanceRecordForm(
        prefix=f"a{attendance.pk}",
        initial={
            "rsvp_status": ""
            if attendance.rsvp_status == "pending"
            else attendance.rsvp_status,
            "attendance_status": attendance.attendance_status,
            "note": attendance.note,
        },
    )


def _meeting_attendance_context(request, meeting):
    """The attendance register plus the controls the reader may use on it."""
    can_edit = resolve(request.user, meeting, EDIT)
    register = meeting.attendances_active()
    rows = [
        {"attendance": attendance, "form": _attendance_record_form(attendance)}
        for attendance in register
    ]
    context = {
        **_target_identifiers(meeting),
        "meeting": meeting,
        "attendance_rows": rows,
        "attendance_counts": meeting.attendance_counts(),
        "can_edit_attendance": can_edit,
        # The register starts collapsed (the counts stay visible); a response to
        # any panel action re-renders it open so a save does not hide the table.
        "attendance_expanded": False,
    }
    if can_edit:
        context["attendance_form"] = MeetingAttendanceAdderForm(meeting=meeting)
    return context


def _meeting_minutes_context(request, meeting):
    """The minutes panel: current status, filed minutes and the actions."""
    can_edit = resolve(request.user, meeting, EDIT)
    return {
        "meeting": meeting,
        "minutes_attachments": meeting.minutes_attachments.select_related(
            "uploaded_by"
        ),
        "agenda_attachments": meeting.agenda_attachments.select_related("uploaded_by"),
        "minutes_status_choices": Meeting.MINUTES_STATUS_CHOICES,
        "can_edit_minutes": can_edit,
    }


def meeting_detail(request, public_id):
    """Show one parliamentary meeting: schedule, attendance, documents, minutes."""
    meeting = get_object_or_404(_meeting_detail_model(), public_id=public_id)
    require(request.user, meeting, VIEW)
    context = {
        "meeting": meeting,
        "transitions": meeting.get_available_transitions(),
    }
    context.update(_meeting_attendance_context(request, meeting))
    context.update(_meeting_minutes_context(request, meeting))
    context.update(_workflow_detail_context(request, meeting))
    context.update(_attachment_context(request, meeting))
    return render(request, "pwms/meeting-detail.html", context)


def meeting_create(request):
    """Create a meeting; its register is generated and its audience alerted."""
    if request.method == "POST":
        _deny_uncreatable_workflow_type(request)
        form = MeetingForm(request.POST, user=request.user)
        if form.is_valid():
            meeting = form.save()
            # The register starts as the hosting group's members; invitees are
            # added by hand afterwards (and survive a regenerate).
            if meeting.host_group_id is not None:
                meeting.regenerate_attendance(actor=request.user)
            messages.success(request, f'Meeting "{meeting.title}" created.')
            notify_meeting_created(meeting, actor=request.user)
            return redirect("pwms:meeting_detail", public_id=meeting.public_id)
    else:
        if not _can_create_form_type(request.user, MeetingForm):
            messages.error(request, "You do not have a role that may create meetings.")
            return redirect("pwms:meetings")
        initial = {"owner": request.user}
        # The calendar links in with a date (and optional time) to prefill the
        # schedule, so "new meeting on this day" opens with the slot already set.
        initial.update(_meeting_create_initial(request.GET))
        form = MeetingForm(user=request.user, initial=initial)
    return render(
        request,
        "pwms/meeting-form.html",
        {"form": form, "meeting": None, "is_create": True},
    )


def meeting_update(request, public_id):
    """Edit a meeting's scheduling detail."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    if request.method == "POST":
        previous_host_group_id = meeting.host_group_id
        form = MeetingForm(request.POST, instance=meeting, user=request.user)
        if form.is_valid():
            meeting = form.save()
            # Re-generate only when the hosting group changed: an unrelated edit
            # must not re-add a generated row the secretary deliberately removed.
            if meeting.host_group_id != previous_host_group_id:
                meeting.regenerate_attendance(actor=request.user)
            messages.success(request, f'Meeting "{meeting.title}" updated.')
            return redirect("pwms:meeting_detail", public_id=meeting.public_id)
    else:
        form = MeetingForm(instance=meeting, user=request.user)
    return render(
        request,
        "pwms/meeting-form.html",
        {"form": form, "meeting": meeting, "is_create": False},
    )


def meeting_delete(request, public_id):
    """Confirm (GET) then delete (POST) a meeting."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, DELETE)
    if request.method == "POST":
        title = meeting.title
        meeting.delete()
        messages.success(request, f'Meeting "{title}" deleted.')
        return redirect("pwms:meetings")
    return render(request, "pwms/meeting-confirm-delete.html", {"meeting": meeting})


def _meeting_attendance_response(request, meeting, *, success="", error=""):
    """Re-render the attendance register, announcing a change when there is one."""
    context = _meeting_attendance_context(request, meeting)
    context.update(_record_counters(meeting))
    # An action came from inside the register, so hand it back open.
    context["attendance_expanded"] = True
    if error:
        context["attendance_status"] = {"level": "danger", "message": error}
    elif success:
        context["attendance_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/meetings/attendance.html", context)


@require_POST
def meeting_attendance_add(request, public_id):
    """Add a person (account or guest) to a meeting's register (HTMX fragment)."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    form = MeetingAttendanceAdderForm(request.POST, meeting=meeting)
    if not form.is_valid():
        errors = " ".join(
            str(message)
            for messages_list in form.errors.values()
            for message in messages_list
        )
        return _meeting_attendance_response(request, meeting, error=errors)
    attendance = form.save(commit=False)
    attendance.meeting = meeting
    attendance.save()
    return _meeting_attendance_response(
        request, meeting, success=f"{attendance.display_name} added to the register."
    )


def _attendance_for(request, meeting, attendance_id):
    return get_object_or_404(
        MeetingAttendance, public_id=attendance_id, meeting=meeting
    )


@require_POST
def meeting_attendance_record(request, public_id, attendance_id):
    """Record one attendee's RSVP and/or attendance during or after the meeting."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    attendance = _attendance_for(request, meeting, attendance_id)
    form = MeetingAttendanceRecordForm(request.POST, prefix=f"a{attendance.pk}")
    if not form.is_valid():
        return _meeting_attendance_response(
            request, meeting, error="The attendance update was not understood."
        )
    rsvp_status = form.cleaned_data.get("rsvp_status")
    status = form.cleaned_data.get("attendance_status")
    note = form.cleaned_data.get("note", "")
    if rsvp_status:
        attendance.respond(rsvp_status, note=note or None)
    if status:
        attendance.record(
            status,
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
            note=note or None,
        )
    return _meeting_attendance_response(
        request, meeting, success=f"Attendance updated for {attendance.display_name}."
    )


@require_POST
def meeting_attendance_remove(request, public_id, attendance_id):
    """Take a person off a meeting's register (HTMX fragment)."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    attendance = _attendance_for(request, meeting, attendance_id)
    name = attendance.display_name
    attendance.delete()
    return _meeting_attendance_response(
        request, meeting, success=f"{name} removed from the register."
    )


@require_POST
def meeting_attendance_notify(request, public_id):
    """Email the meeting's audience a recorded attendance summary."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    notify_meeting_attendance(meeting, None, actor=request.user, status="manual")
    return _meeting_attendance_response(
        request, meeting, success="Attendance summary sent to the meeting's audience."
    )


@require_POST
def meeting_attendance_regenerate(request, public_id):
    """Rebuild the register from the hosting group, keeping the invitees (HTMX)."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    if meeting.host_group_id is None:
        return _meeting_attendance_response(
            request,
            meeting,
            error=(
                "No hosting group is set, so there is nothing to generate. Set one "
                "on the meeting, or add invitees by hand."
            ),
        )
    summary = meeting.regenerate_attendance(actor=request.user)
    message = (
        f"Register regenerated from {meeting.host_group.name}: "
        f"{summary['added']} added, {summary['removed']} removed — "
        f"{summary['total']} on the register."
    )
    return _meeting_attendance_response(request, meeting, success=message)


def _meeting_minutes_response(request, meeting, *, success="", error=""):
    """Re-render the minutes panel, announcing a change when there is one."""
    context = _meeting_minutes_context(request, meeting)
    context.update(_record_counters(meeting))
    if error:
        context["minutes_status_message"] = {"level": "danger", "message": error}
    elif success:
        context["minutes_status_message"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/meetings/minutes.html", context)


@require_POST
def meeting_minutes_status(request, public_id):
    """Advance the minutes status (draft / filed / approved) and record it."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    status = request.POST.get("status", "")
    if status == "draft":
        meeting.minutes_status = "draft"
        meeting.save(update_fields=["minutes_status", "updated_at"])
    elif status == "filed":
        meeting.record_minutes_filed(actor=request.user)
    elif status == "approved":
        meeting.record_minutes_approved(
            actor=request.user, comment=request.POST.get("comment", "")
        )
    else:
        return _meeting_minutes_response(
            request, meeting, error="Unknown minutes status."
        )
    return _meeting_minutes_response(
        request, meeting, success="Minutes status updated."
    )


def _meeting_agenda_context(request, meeting):
    """Context the agenda panel and its inline editor both render from.

    Carries what the panel needs standalone (it is returned as an HTMX fragment
    as well as included in the page): the meeting, the reader's capabilities, and
    the attachment-target identifiers the "Upload agenda" button posts to.
    """
    meta = meeting._meta
    return {
        "meeting": meeting,
        "perms": permissions_for(request.user, meeting),
        "attachment_content_type": f"{meta.app_label}.{meta.model_name}",
        "attachment_object_id": str(meeting.pk),
    }


def meeting_agenda(request, public_id):
    """The read-only agenda panel (restores it when the inline editor is cancelled)."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, VIEW)
    return render(
        request,
        "pwms/partials/meetings/agenda.html",
        _meeting_agenda_context(request, meeting),
    )


def meeting_agenda_edit(request, public_id):
    """Edit the agenda inline: GET shows the editor, POST saves it (HTMX fragment).

    Editing is an ``edit`` action on the meeting, exactly as the full meeting form
    is. The saved value is the rich-text editor's HTML; it is sanitised when it is
    rendered (see ``Meeting.agenda_html``), never here.
    """
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    context = _meeting_agenda_context(request, meeting)

    if request.method == "POST":
        form = MeetingAgendaForm(request.POST, instance=meeting)
        if form.is_valid():
            meeting = form.save(commit=False)
            meeting.save(update_fields=["agenda_summary", "updated_at"])
            context["meeting"] = meeting
            context["agenda_status"] = {
                "level": "success",
                "message": "Agenda updated.",
            }
            return render(request, "pwms/partials/meetings/agenda.html", context)
    else:
        form = MeetingAgendaForm(instance=meeting)

    context["form"] = form
    return render(request, "pwms/partials/meetings/agenda_editor.html", context)
