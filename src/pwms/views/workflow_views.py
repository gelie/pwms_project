import logging
from datetime import datetime, time

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import (
    FileResponse,
    Http404,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from ..forms import (
    WorkflowAssignForm,
    WorkflowDeadlineForm,
    WorkflowShareForm,
)
from ..models import (
    AbstractLegislativeWorkflow,
    Bill,
    User,
)
from ..reporting import (
    response_for_instrument,
    send_instrument_share_email,
)
from ..services.bill_updates import (
    EVENT_STATUS_CHANGE,
    queue_bill_update,
)
from ..services.history import TIMELINE_PREVIEW_LIMIT, instance_timeline
from ..services.permissions import (
    EDIT,
    SHARE,
    TRANSITION,
    VIEW,
    permissions_for,
    require,
    resolve,
    visible_instances,
)
from ..services.progress import workflow_progress
from ..utils.diagrams import workflow_type_diagram_path

logger = logging.getLogger(__name__)

from .common import (
    _WORKFLOW_MODELS,
    _can_offer_assignment,
    _can_offer_deadline,
    _return_to,
    _target_identifiers,
    _workflow_by_public_id,
    _workflow_detail_url,
    _workflow_identifier,
    _workflow_search_text,
    _workflow_target,
)
from .events import _events_context
from .notes import _notes_context
from .referrals import _referral_context


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
            "can_set_deadline": _can_offer_deadline(request.user, instance),
            **_target_identifiers(instance),
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


@require_POST
def workflow_assign(request):
    """Give a record that has nobody assigned someone, from its detail page.

    A closed record takes no assignee, and one that already has an assignee is
    left alone: reassigning is an edit, made on the record's own form. Either way
    the reader goes back to the page they came from.
    """
    target = _workflow_target(request, EDIT)
    destination = _return_to(request)

    if target.current_state.is_terminal:
        messages.error(request, _("A closed workflow does not take an assignee."))
    elif target.assigned_to_id is not None:
        messages.error(
            request,
            _("That workflow is already assigned. Edit it on its own page."),
        )
    else:
        form = WorkflowAssignForm(request.POST)
        if form.is_valid():
            person = form.cleaned_data["assigned_to"]
            target.assigned_to = person
            target.save(update_fields=["assigned_to", "updated_at"])
            messages.success(
                request, _("Assigned to %(name)s.") % {"name": person.display_name}
            )
        else:
            messages.error(request, _("Choose a person from the list."))
    return redirect(destination)


@require_POST
def workflow_set_deadline(request):
    """Give a record that has no deadline one, from the workflow register.

    The deadline is the end of the chosen day in the server's time zone. A closed
    record takes none (its deadline no longer matters), and one that already has a
    deadline is left alone: changing it is an edit, made on the record's own form.
    Either way the reader goes back to the page they came from.
    """
    target = _workflow_target(request, EDIT)
    destination = _return_to(request)

    if target.current_state.is_terminal:
        messages.error(request, _("A closed workflow does not take a deadline."))
    elif target.deadline is not None:
        messages.error(
            request, _("That workflow already has a deadline. Edit it on its own page.")
        )
    else:
        form = WorkflowDeadlineForm(request.POST)
        if form.is_valid():
            end_of_day = datetime.combine(form.cleaned_data["deadline"], time(23, 59))
            target.deadline = timezone.make_aware(end_of_day)
            target.save(update_fields=["deadline", "updated_at"])
            messages.success(request, _("Deadline set."))
        else:
            messages.error(request, _("Enter a valid date for the deadline."))
    return redirect(destination)


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
        # The "Set deadline" button beside an empty Due date (same rule as the
        # workflow register).
        "can_set_deadline": _can_offer_deadline(request.user, instance),
        # The "Assign" button beside an empty Assigned to, and the picker it opens.
        "can_assign": _can_offer_assignment(request.user, instance),
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
    if context["can_assign"]:
        context["assign_form"] = WorkflowAssignForm()
    if context["perms"].get("share"):
        # The Share modal's email form; only built for a reader who may share.
        context["share_form"] = WorkflowShareForm()
    context.update(_referral_context(request, instance))
    context.update(_notes_context(request, instance))
    context.update(_events_context(request, instance))
    return context


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


def workflow_share_recipients(request, public_id):
    """
    Internal users matching a search, as HTMX buttons for the Share modal.

    Only people with an email address are offered. Requires SHARE on the record,
    the same right the send itself needs, so the endpoint cannot be used by
    someone who may not share to browse the directory.
    """
    instance = _workflow_by_public_id(public_id)
    require(request.user, instance, SHARE)

    search = request.GET.get("search", "").strip()
    users = []
    if len(search) >= 2:
        users = list(
            User.objects.filter(is_active=True)
            .exclude(email="")
            .filter(
                Q(username__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
            )
            .order_by("first_name", "last_name", "username")[:10]
        )
    return render(
        request,
        "pwms/partials/share_recipient_results.html",
        {"users": users, "searched": len(search) >= 2},
    )


@require_POST
def workflow_share(request, public_id):
    """
    Email a link to a workflow record (optionally with a copy) to 1+ people.

    Recipients may be internal users or external addresses. Needs both VIEW and
    SHARE on the record: a copy attached to an email leaves the system, so it is
    never available to someone who could not open the record, and the share
    right is what lets a group's members do that.

    Answers JSON for the modal. A recipient who cannot open the link (an external
    address, or an internal user without access) is reported as a warning rather
    than refused, because sending them the link is still allowed — the modal
    just tells the sender to attach a copy if they need it read.
    """
    instance = _workflow_by_public_id(public_id)
    require(request.user, instance, VIEW)
    require(request.user, instance, SHARE)

    form = WorkflowShareForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"success": False, "errors": form.errors.get_json_data()}, status=400
        )

    data = form.cleaned_data
    recipients = data["recipients"]
    attach_format = data["attach_format"]
    link = request.build_absolute_uri(_workflow_detail_url(instance))

    # Who could actually open the link? Match recipients to accounts by address.
    cannot_open = []
    if not attach_format:
        for address in recipients:
            account = User.objects.filter(is_active=True, email__iexact=address).first()
            if account is None or not resolve(account, instance, VIEW):
                cannot_open.append(address)

    try:
        sent = send_instrument_share_email(
            instance,
            sender=request.user,
            recipients=recipients,
            link=link,
            message=data["message"],
            attachment_format=attach_format,
        )
    except Exception as error:
        logger.exception("Workflow %s could not be shared by email", instance.pk)
        return JsonResponse(
            {"success": False, "error": f"The email could not be sent: {error}"},
            status=502,
        )

    warnings = []
    if cannot_open:
        warnings.append(
            "These recipients have no access to the record, so the link will not "
            "open for them (attach a copy to share it with them): "
            + ", ".join(cannot_open)
            + "."
        )
    return JsonResponse(
        {"success": True, "sent": sent, "link": link, "warnings": warnings}
    )


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
        if isinstance(instance, Bill):
            queue_bill_update(
                instance,
                EVENT_STATUS_CHANGE,
                actor=request.user,
                transition=transition,
                comment=comment,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        messages.success(
            request,
            f"{instance.identifier} moved to “{transition.to_state.name}”.",
        )
        # Land on the Timeline, where the state change is recorded.
        return HttpResponseRedirect(f"{_workflow_detail_url(instance)}#pane-timeline")

    return _transition_page(request, instance, transition, unmet=unmet)
