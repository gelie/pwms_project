from django import forms
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import AuthenticationForm, UsernameField
from django.contrib.auth.views import LoginView as BaseLoginView
from django.contrib.auth.views import LogoutView as BaseLogoutView
from django.db.models import Case, CharField, Value, When
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import Group


@login_not_required
def index(request):
    """Home page - the only route reachable without signing in."""
    return render(request, "pwms/index.html")


def dashboard(request):
    return render(request, "pwms/dashboard.html")


def workflows(request):
    return render(request, "pwms/workflows.html")


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
