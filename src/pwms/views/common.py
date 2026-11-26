import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import (
    Http404,
)
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _

from ..models import (
    AbstractLegislativeWorkflow,
    Bill,
    DelegationReport,
    InternationalAgreement,
    InternationalResolution,
    Meeting,
    WorkflowType,
)
from ..services.history import instance_timeline
from ..services.permissions import (
    EDIT,
    VIEW,
    require,
    resolve,
)
from ..services.progress import workflow_progress

logger = logging.getLogger(__name__)


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


def _return_to(request):
    """Where a POST from the register or a detail page sends the reader back.

    The ``next`` the form carried, if it stays on this site; otherwise the
    workflow register, so a forged value cannot redirect anywhere else.
    """
    destination = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(
        destination,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return destination
    return reverse("pwms:workflows")


def _can_offer_assignment(user, instance):
    """
    Whether a detail page should offer "Assign" on ``instance``.

    Only for an open record that has nobody assigned yet and whose reader may edit
    it. A closed record owes no further action, so nobody needs assigning to it.
    """
    if instance.assigned_to_id is not None or instance.current_state.is_terminal:
        return False
    return resolve(user, instance, EDIT)


def _can_offer_deadline(user, instance):
    """
    Whether the register should offer "Set deadline" on ``instance``.

    Only for an open record with no deadline yet whose reader may edit it. A
    meeting is left out: it is scheduled by its start time and its form has no
    deadline. The permission lookup is the last test, so it only costs a query for
    the rows that could show the button.
    """
    if (
        instance.deadline is not None
        or instance.current_state.is_terminal
        or isinstance(instance, Meeting)
    ):
        return False
    return resolve(user, instance, EDIT)


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


def _viewable_sub_workflows(instance, user):
    """Children of ``instance`` the user may open, paired with their URLs."""
    return [
        {"object": child, "url": _workflow_detail_url(child)}
        for child in instance.sub_workflows
        if resolve(user, child, VIEW)
    ]


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
