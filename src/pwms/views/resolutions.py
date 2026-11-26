import logging

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from ..forms import (
    InternationalResolutionForm,
)
from ..models import (
    InternationalResolution,
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
from .common import _can_create_form_type, _deny_uncreatable_workflow_type
from .workflow_views import _workflow_detail_context


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
