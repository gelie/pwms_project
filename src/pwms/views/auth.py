import logging
from typing import ClassVar

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UsernameField
from django.contrib.auth.views import LoginView as BaseLoginView
from django.contrib.auth.views import LogoutView as BaseLogoutView
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)


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
