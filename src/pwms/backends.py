"""
Authentication backends: Active Directory first, local database second.

Credentials live in the directory, so ``GracefulLDAPBackend`` is listed first in
``AUTHENTICATION_BACKENDS``. ``FallbackModelBackend`` catches whatever LDAP
cannot serve: identities with no AD account at all, and a locally created
superuser signing in while the directory is unreachable.
"""

import logging

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django_auth_ldap.backend import LDAPBackend

logger = logging.getLogger(__name__)


class GracefulLDAPBackend(LDAPBackend):
    """
    Authenticate against Active Directory, standing aside when AD cannot serve.

    django-auth-ldap already collapses both outcomes to ``None`` - a rejected
    password (``AuthenticationFailed``) and a server that is down, timing out or
    misbehaving (``ldap.LDAPError``) - so Django moves on to the next backend by
    itself. This class therefore does *not* wrap ``authenticate()`` in a blanket
    ``except``: doing so would also swallow configuration errors, and an
    unreachable directory should degrade to the local backend rather than hide
    that LDAP was never configured properly.

    What it does add are the two checks the library leaves out:

    * ``is_active``. ``LDAPBackend`` is not a ``ModelBackend`` and never consults
      it, so a user deactivated here (``sync_users_oracle`` closing an account
      after someone leaves) could still sign in while AD has not disabled them
      yet.
    * Identity linkage. The AD account name is matched against the ERP username
      case-insensitively, with the AD mail address as a fallback, so directory
      users land on the identity row the ERP already created instead of a
      duplicate (see :meth:`get_or_build_user`).
    """

    #: AD attribute naming the account. ``sync_users_oracle`` stores ERP
    #: usernames lower-cased, so it is compared case-insensitively and a newly
    #: built account uses the same lower-cased form.
    username_attribute = "sAMAccountName"

    def _is_configured(self) -> bool:
        """
        True when a server and a way of locating users are both configured.

        Every LDAP setting is optional (see the block in ``settings.py``), so a
        checkout with no directory - a developer's machine, CI - must not spend
        every login attempt dialling an empty URI. Without this guard the flow
        would still end in a failed bind and ``None``, but only after a pointless
        connection attempt, and a half-configured deployment (URI but no search
        base) would raise ``ImproperlyConfigured`` instead of failing over.
        """
        if not getattr(settings, "AUTH_LDAP_SERVER_URI", ""):
            return False
        return bool(
            getattr(settings, "AUTH_LDAP_USER_SEARCH", None)
            or getattr(settings, "AUTH_LDAP_USER_DN_TEMPLATE", None)
        )

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not self._is_configured():
            return None

        user = super().authenticate(
            request, username=username, password=password, **kwargs
        )

        if user is not None and not user.is_active:
            # Worth distinguishing from a rejected password in the logs: the
            # directory accepted the credentials and this application is the
            # one refusing, usually because the ERP sync closed the account.
            logger.warning(
                "Refusing LDAP login for inactive user %r: the account is "
                "deactivated in PWMS.",
                user.get_username(),
            )
            return None

        return user

    def get_or_build_user(self, username, ldap_user):
        """
        Match an authenticated AD account to its PWMS user, or build one.

        Django's default lookup is wrong for this integration either way: it
        compares the *typed* identifier, so signing in with an email address
        would build a second account for someone the ERP already knows, or - with
        ``AUTH_LDAP_USER_QUERY_FIELD`` set - it compares the AD account name
        case-sensitively against the lower-cased ERP usernames.

        Resolution order:

        1. the AD account name against :attr:`~django.contrib.auth.models.User.username`
           (case-insensitive). Most ERP identities are administered under the
           same name in both systems, so this is the normal path;
        2. the AD ``mail`` attribute against
           :attr:`~django.contrib.auth.models.User.email`, but only when it
           identifies exactly one account. This recovers the ERP users whose AD
           account was raised under a different name, who would otherwise get a
           duplicate, empty PWMS account;
        3. otherwise build a new user named after the AD account.

        The returned user is never renamed: ``username`` is owned by the ERP sync
        and is absent from ``AUTH_LDAP_USER_ATTR_MAP``.
        """
        model = self.get_user_model()
        username_field = model.USERNAME_FIELD
        account_name = self._ldap_value(ldap_user, self.username_attribute) or username

        user = model.objects.filter(
            **{f"{username_field}__iexact": account_name}
        ).first()
        if user is None:
            user = self._user_by_mail(model, ldap_user)
        if user is not None:
            return user, False

        return model(**{username_field: account_name.lower()}), True

    @staticmethod
    def _ldap_value(ldap_user, attribute):
        """
        First value of ``attribute``, or ``None`` when the directory has none.

        ``LDAPSearch.execute`` decodes attribute values to ``str`` before they
        reach us, so the value is used as-is. AD does not populate every
        attribute on every account (``givenName`` and ``mail`` are both routinely
        absent), hence the defensive accessors.
        """
        try:
            value = ldap_user.attrs[attribute][0]
        except AttributeError, KeyError, IndexError, TypeError:
            return None
        return value or None

    def _user_by_mail(self, model, ldap_user):
        """
        The single PWMS account carrying the AD account's mail address.

        ``User.email`` is not unique, so an address shared by several rows is
        treated as no match: the wrong account must never be handed to whoever
        just authenticated.
        """
        mail = self._ldap_value(ldap_user, "mail")
        if not mail:
            return None

        matches = model.objects.filter(email__iexact=mail)[:2]
        return matches[0] if len(matches) == 1 else None


class FallbackModelBackend(ModelBackend):
    """
    Local database credentials, tried after LDAP.

    This is the break-glass path and the safety net:

    * a locally created superuser can still sign in when the directory is
      unreachable (otherwise a directory outage would lock everyone out,
      including whoever administers the site);
    * ERP identities with no AD account at all keep the password
      ``sync_users_oracle`` last set for them, where LDAP has nothing to offer.

    It also answers Django's own permission checks for directory users.
    ``LDAPBackend`` is not a ``ModelBackend`` and only knows about LDAP group
    permissions - which this project does not use - so ``is_superuser`` and staff
    permissions are resolved here: Django consults *every* backend in
    ``AUTHENTICATION_BACKENDS``, not just the one that authenticated the user.
    Removing this backend would strip those permissions from everyone.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        """Authenticate against the local database, with logging for debugging."""
        logger.debug("Attempting local authentication for %r", username)
        user = super().authenticate(
            request, username=username, password=password, **kwargs
        )

        if user:
            logger.info("Local authentication succeeded for %r", username)
        else:
            logger.debug("Local authentication failed for %r", username)

        return user
