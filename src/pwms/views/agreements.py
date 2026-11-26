import logging

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from ..forms import (
    InternationalAgreementForm,
)
from ..models import (
    InternationalAgreement,
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
