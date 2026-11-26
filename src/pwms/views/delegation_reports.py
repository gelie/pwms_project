import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..forms import (
    ChildResolutionForm,
    ChildResolutionFormSet,
    DelegateAdderForm,
    DelegationParticipantAdderForm,
    DelegationParticipantFormSet,
    DelegationReportForm,
    DelegationReportUpdateForm,
    InternationalResolutionForm,
    ResolutionAdderForm,
)
from ..models import (
    DelegationParticipant,
    DelegationReport,
)
from ..notifications import (
    notify_workflow_created,
)
from ..services.permissions import (
    DELETE,
    EDIT,
    VIEW,
    require,
    resolve,
    visible_instances,
)

logger = logging.getLogger(__name__)

from .attachments import _attachment_context
from .common import (
    _can_create_form_type,
    _creatable_form_type,
    _deny_uncreatable_workflow_type,
    _record_counters,
    _viewable_sub_workflows,
)
from .workflow_views import _workflow_detail_context


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
