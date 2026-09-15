"""
Active Directory sign-in.

Credentials live in the directory (``GracefulLDAPBackend``), so what is tested
here is the translation of a successful bind into a *correct* PWMS session: the
backend ordering that puts the directory first, the ``is_active`` rule the
library leaves out, and the identity linking that finds the row the ERP sync
already created instead of building a duplicate.

Nothing here contacts a directory. LDAP is mocked at the ``django_auth_ldap``
boundary, so the suite runs anywhere - except
``test_unreachable_directory_does_not_raise``, which dials a port that refuses
connections to prove the "directory is down" path really is graceful.
"""

from types import SimpleNamespace
from unittest import mock

import ldap
from django.conf import settings
from django.contrib.auth import authenticate, get_backends, get_user_model
from django.test import TestCase, override_settings
from django_auth_ldap.backend import LDAPBackend
from django_auth_ldap.config import LDAPSearch

from .backends import FallbackModelBackend, GracefulLDAPBackend


class LDAPConfigurationTests(TestCase):
    """How the directory backend is wired into the project."""

    def test_directory_is_tried_before_the_local_database(self):
        """
        Order is what makes the directory authoritative: the first backend to
        return a user wins, so LDAP must be asked before the local table.
        """
        self.assertEqual(
            settings.AUTHENTICATION_BACKENDS,
            [
                "pwms.backends.GracefulLDAPBackend",
                "pwms.backends.FallbackModelBackend",
            ],
        )
        # Resolving the dotted paths is what happens at request time.
        self.assertEqual(len(get_backends()), 2)

    def test_directory_is_not_used_for_authorisation(self):
        """
        PWMS authorises through Role/GroupMembership. Mirroring AD groups into
        auth.Group would fill that table with rows nothing reads and load
        LDAP-group permissions on every check.
        """
        self.assertFalse(getattr(settings, "AUTH_LDAP_FIND_GROUP_PERMS", False))
        self.assertFalse(getattr(settings, "AUTH_LDAP_MIRROR_GROUPS", False))

    def test_directory_does_not_own_the_erp_username(self):
        """
        ``sync_users_oracle`` owns ``User.username`` (it writes the lower-cased
        Oracle value), so the attribute map must not let AD rewrite it at login.
        """
        self.assertNotIn("username", getattr(settings, "AUTH_LDAP_USER_ATTR_MAP", {}))

    def test_backend_stands_aside_when_no_directory_is_configured(self):
        """
        Every LDAP setting is optional, so a checkout with no directory must not
        dial anything: the backend returns None and the local one is asked.
        """
        with (
            override_settings(AUTH_LDAP_SERVER_URI="", AUTH_LDAP_USER_SEARCH=None),
            mock.patch.object(LDAPBackend, "authenticate") as ldap_authenticate,
        ):
            self.assertIsNone(GracefulLDAPBackend().authenticate(None, "u", "p"))

        ldap_authenticate.assert_not_called()


class LDAPAuthenticateTests(TestCase):
    """What the backend returns once the directory has accepted a password."""

    #: Enough configuration to get past the "is LDAP set up?" guard.
    configured = {
        "AUTH_LDAP_SERVER_URI": "ldap://ldap.example.invalid",
        "AUTH_LDAP_USER_SEARCH": LDAPSearch(
            "dc=example,dc=invalid", ldap.SCOPE_SUBTREE
        ),
    }

    def test_inactive_account_is_rejected_despite_valid_credentials(self):
        """
        django-auth-ldap never consults ``is_active`` (it is not a ModelBackend),
        so without this check a user deactivated here - by ``sync_users_oracle``
        after leaving the payroll, say - keeps signing in for as long as AD
        still has the account enabled.
        """
        user = get_user_model().objects.create_user(username="leaver", password="pw")
        user.is_active = False
        user.save(update_fields=["is_active"])

        with (
            override_settings(**self.configured),
            mock.patch.object(LDAPBackend, "authenticate", return_value=user),
        ):
            self.assertIsNone(GracefulLDAPBackend().authenticate(None, "leaver", "pw"))

    def test_active_account_is_returned_unchanged(self):
        user = get_user_model().objects.create_user(username="active", password="pw")

        with (
            override_settings(**self.configured),
            mock.patch.object(LDAPBackend, "authenticate", return_value=user),
        ):
            self.assertEqual(
                GracefulLDAPBackend().authenticate(None, "active", "pw"), user
            )

    def test_local_password_works_when_the_directory_has_nothing(self):
        """
        The local backend is the safety net for identities with no AD account and
        for a break-glass superuser during an outage. Both backends run through
        Django's own authenticate(), so the ordering under test is the real one.
        """
        user = get_user_model().objects.create_user(
            username="no-ad-account", password="local-secret"
        )

        with (
            override_settings(**self.configured),
            mock.patch.object(LDAPBackend, "authenticate", return_value=None),
        ):
            authenticated = authenticate(
                username="no-ad-account", password="local-secret"
            )

        self.assertEqual(authenticated, user)

    def test_unreachable_directory_does_not_raise(self):
        """
        An outage has to look like wrong credentials, not a 500 on the sign-in
        form: django-auth-ldap absorbs the connection error, and the local
        backend then gets its turn. Port 1 refuses immediately.
        """
        user = get_user_model().objects.create_user(
            username="resilient", password="local-secret"
        )

        dead_directory = self.configured | {
            "AUTH_LDAP_SERVER_URI": "ldap://127.0.0.1:1"
        }
        with override_settings(**dead_directory):
            authenticated = authenticate(username="resilient", password="local-secret")

        self.assertEqual(authenticated, user)


class LDAPIdentityLinkingTests(TestCase):
    """Finding the PWMS identity that an authenticated AD account belongs to."""

    @staticmethod
    def directory_user(**attributes):
        """A stand-in for django_auth_ldap's _LDAPUser, exposing just ``attrs``."""
        return SimpleNamespace(
            attrs={name: [value] for name, value in attributes.items()}
        )

    def test_account_name_links_to_the_existing_erp_row(self):
        """
        AD keeps whatever case the account was created with, the ERP stores
        usernames lower-cased, so the match must ignore case - otherwise every
        such user would silently get a second, empty account.
        """
        erp_user = get_user_model().objects.create_user(
            username="jsmith", email="j.smith@example.com"
        )

        user, built = GracefulLDAPBackend().get_or_build_user(
            "JSmith",
            self.directory_user(sAMAccountName="JSmith", mail="j.smith@example.com"),
        )

        self.assertFalse(built)
        self.assertEqual(user, erp_user)
        # The ERP owns the username: signing in must not rename the row.
        self.assertEqual(user.username, "jsmith")

    def test_mail_links_accounts_named_differently(self):
        """
        Some AD accounts are administered under a name the ERP never used. The
        mail address still identifies the person; without this fallback they
        would land on a new account carrying none of their roles.
        """
        erp_user = get_user_model().objects.create_user(
            username="p.smith", email="p.smith@example.com"
        )

        user, built = GracefulLDAPBackend().get_or_build_user(
            "PSmith1",
            self.directory_user(sAMAccountName="PSmith1", mail="p.smith@example.com"),
        )

        self.assertFalse(built)
        self.assertEqual(user, erp_user)

    def test_ambiguous_mail_address_is_not_used_to_link(self):
        """
        ``User.email`` is not unique, and attaching a session to the wrong row
        would hand one person another's roles, so a shared address is ignored.
        """
        User = get_user_model()
        User.objects.create_user(username="shared-a", email="shared@example.com")
        User.objects.create_user(username="shared-b", email="shared@example.com")

        user, built = GracefulLDAPBackend().get_or_build_user(
            "NewPerson",
            self.directory_user(sAMAccountName="NewPerson", mail="shared@example.com"),
        )

        self.assertTrue(built)
        self.assertEqual(user.username, "newperson")

    def test_unknown_account_is_built_with_the_lower_cased_directory_name(self):
        User = get_user_model()

        user, built = GracefulLDAPBackend().get_or_build_user(
            "NNewcomer", self.directory_user(sAMAccountName="NNewcomer")
        )

        self.assertTrue(built)
        self.assertEqual(user.username, "nnewcomer")
        self.assertFalse(User.objects.filter(username="nnewcomer").exists())

    def test_missing_directory_attributes_do_not_break_linking(self):
        """
        AD omits ``givenName`` and ``mail`` on some accounts, and no attributes
        at all are available when the search failed, so an entry carrying only
        the account name still has to link.
        """
        erp_user = get_user_model().objects.create_user(username="noatts")

        user, built = GracefulLDAPBackend().get_or_build_user(
            "noatts", self.directory_user()
        )

        self.assertFalse(built)
        self.assertEqual(user, erp_user)


class FallbackModelBackendTests(TestCase):
    """The local backend, which is what a directory-less deployment relies on."""

    def test_valid_local_password_authenticates(self):
        user = get_user_model().objects.create_user(
            username="local-admin", password="local-secret"
        )

        self.assertEqual(
            FallbackModelBackend().authenticate(None, "local-admin", "local-secret"),
            user,
        )

    def test_wrong_local_password_is_rejected(self):
        get_user_model().objects.create_user(
            username="local-admin", password="local-secret"
        )

        self.assertIsNone(
            FallbackModelBackend().authenticate(None, "local-admin", "wrong")
        )
