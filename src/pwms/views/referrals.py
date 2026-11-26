import logging

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import (
    Http404,
)
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from ..forms import (
    ReferralForm,
)
from ..models import (
    WorkflowReferral,
)
from ..services.permissions import (
    EDIT,
    resolve,
)

logger = logging.getLogger(__name__)

from .common import _record_counters, _target_identifiers, _workflow_target


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
