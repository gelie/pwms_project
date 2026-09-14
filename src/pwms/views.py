from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import AuthenticationForm, UsernameField
from django.contrib.auth.views import LoginView as BaseLoginView
from django.contrib.auth.views import LogoutView as BaseLogoutView
from django.core.exceptions import PermissionDenied
from django.db.models import Case, CharField, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .forms import (
    DelegationReportForm,
    InternationalAgreementForm,
    InternationalResolutionForm,
)
from .models import (
    City,
    Country,
    DelegationReport,
    Group,
    InternationalAgreement,
    InternationalResolution,
    User,
    WorkflowType,
)
from .services.permissions import (
    DELETE,
    EDIT,
    VIEW,
    permissions_for,
    require,
    resolve,
    visible_instances,
)


@login_not_required
def index(request):
    """Home page - the only route reachable without signing in."""
    return render(request, "pwms/index.html")


def dashboard(request):
    return render(request, "pwms/dashboard.html")


def workflows(request):
    """Overview of the concrete workflow registers.

    Counts and recent tables are limited to instances the signed-in user may
    view, so the page never advertises rows whose detail page would 403.
    """
    viewable_reports = visible_instances(
        request.user,
        DelegationReport.objects.select_related("current_state", "owner").order_by(
            "-created_at"
        ),
    )
    viewable_resolutions = visible_instances(
        request.user,
        InternationalResolution.objects.select_related(
            "current_state", "owner"
        ).order_by("-created_at"),
    )
    viewable_agreements = visible_instances(
        request.user,
        InternationalAgreement.objects.select_related(
            "current_state", "owner"
        ).order_by("-created_at"),
    )
    context = {
        "report_count": len(viewable_reports),
        "resolution_count": len(viewable_resolutions),
        "agreement_count": len(viewable_agreements),
        "recent_reports": viewable_reports[:5],
        "recent_resolutions": viewable_resolutions[:5],
        "recent_agreements": viewable_agreements[:5],
        # The create views redirect away unless the user holds a create role for
        # at least one enabled workflow type, so gate the "New ..." buttons on
        # that same condition instead of offering a dead action.
        "can_create_workflows": WorkflowType.creatable_by(request.user).exists(),
    }
    return render(request, "pwms/workflows.html", context)


def all_groups(request):
    """List every group in the organisational hierarchy (sign-in required)."""
    groups = Group.objects.select_related("parent").order_by("name")
    return render(request, "pwms/all-groups.html", {"groups": groups})


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
    return None


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
    # Contained resolutions are separate instances with their own access, so
    # only advertise the ones the user may actually open.
    resolutions = [
        {"object": item, "url": _workflow_detail_url(item)}
        for item in report.sub_workflows
        if resolve(request.user, item, VIEW)
    ]
    context = {
        "report": report,
        "perms": permissions_for(request.user, report),
        "participants": report.participants.select_related("user"),
        "resolutions": resolutions,
        "updates": report.updates.select_related("resulting_state", "recorded_by"),
        "referrals": report.referrals().select_related("referred_to", "referred_by"),
        "transitions": report.get_available_transitions(),
        "transition_logs": report.audit_logs().select_related(
            "from_state", "to_state", "actor"
        ),
    }
    return render(request, "pwms/delegation-report-detail.html", context)


def delegation_report_create(request):
    """Create a delegation report; the initial state is derived from its type."""
    if request.method == "POST":
        _deny_uncreatable_workflow_type(request)
        form = DelegationReportForm(request.POST, user=request.user)
        if form.is_valid():
            report = form.save()
            messages.success(
                request, f'Delegation report "{report.reference_number}" created.'
            )
            return redirect("pwms:delegation_report_detail", public_id=report.public_id)
    else:
        if not WorkflowType.creatable_by(request.user).exists():
            messages.error(
                request,
                "You do not have a role that may create delegation reports.",
            )
            return redirect("pwms:delegation_reports")
        form = DelegationReportForm(user=request.user, initial={"owner": request.user})
    return render(
        request,
        "pwms/delegation-report-form.html",
        {"form": form, "is_create": True},
    )


def delegation_report_update(request, public_id):
    """Edit a delegation report."""
    report = get_object_or_404(DelegationReport, public_id=public_id)
    require(request.user, report, EDIT)
    if request.method == "POST":
        form = DelegationReportForm(request.POST, instance=report)
        if form.is_valid():
            report = form.save()
            messages.success(
                request, f'Delegation report "{report.reference_number}" updated.'
            )
            return redirect("pwms:delegation_report_detail", public_id=report.public_id)
    else:
        form = DelegationReportForm(instance=report)
    return render(
        request,
        "pwms/delegation-report-form.html",
        {"form": form, "report": report, "is_create": False},
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
    parent = resolution.parent_workflow
    # The parent is a separate instance: don't reveal or link it unless the user
    # may view it too.
    parent_viewable = parent is not None and resolve(request.user, parent, VIEW)
    context = {
        "resolution": resolution,
        "perms": permissions_for(request.user, resolution),
        "parent": parent if parent_viewable else None,
        "parent_url": _workflow_detail_url(parent) if parent_viewable else None,
        "referrals": resolution.referrals().select_related(
            "referred_to", "referred_by"
        ),
        "transitions": resolution.get_available_transitions(),
        "transition_logs": resolution.audit_logs().select_related(
            "from_state", "to_state", "actor"
        ),
    }
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
            return redirect(
                "pwms:international_resolution_detail",
                public_id=resolution.public_id,
            )
    else:
        if not WorkflowType.creatable_by(request.user).exists():
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
        form = InternationalResolutionForm(request.POST, instance=resolution)
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
    parent = agreement.parent_workflow
    # The parent is a separate instance: don't reveal or link it unless the user
    # may view it too.
    parent_viewable = parent is not None and resolve(request.user, parent, VIEW)
    context = {
        "agreement": agreement,
        "perms": permissions_for(request.user, agreement),
        "parent": parent if parent_viewable else None,
        "parent_url": _workflow_detail_url(parent) if parent_viewable else None,
        "referrals": agreement.referrals().select_related("referred_to", "referred_by"),
        "transitions": agreement.get_available_transitions(),
        "transition_logs": agreement.audit_logs().select_related(
            "from_state", "to_state", "actor"
        ),
    }
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
            return redirect(
                "pwms:international_agreement_detail",
                public_id=agreement.public_id,
            )
    else:
        if not WorkflowType.creatable_by(request.user).exists():
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


def reports(request):
    return render(request, "pwms/reports.html")


def about(request):
    """About page (sign-in required)."""
    return render(request, "pwms/about.html")


def contact(request):
    """Contact page (sign-in required)."""
    return render(request, "pwms/about.html")


class LoginForm(AuthenticationForm):
    """Authentication form wired to the project's Bootstrap form styling."""

    username = UsernameField(
        label=_("Username"),
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": _("Username"),
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
    http_method_names = ["get", "post", "options"]

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
