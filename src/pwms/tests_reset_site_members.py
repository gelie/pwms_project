"""``reset_site_members`` clears manually curated membership safely.

Membership is not synced from SharePoint and is edited by hand in the admin,
so these tests pin the guard rails of the bulk counterpart: a dry run writes
nothing, a real run leaves sites and users intact, and the ``--site`` /
``--inactive-only`` narrowing does exactly what it says.
"""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .models import (
    SharepointSite,
    SharepointSiteMember,
)


class ResetSiteMembersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.member = User.objects.create_user(username="rsp-member", password="pw")
        cls.former = User.objects.create_user(username="rsp-former", password="pw")
        cls.site = SharepointSite.objects.create(
            name="Committee Library",
            url="https://contoso.sharepoint.com/sites/committee",
            site_id="contoso,committee",
        )
        cls.other_site = SharepointSite.objects.create(
            name="Research Library",
            url="https://contoso.sharepoint.com/sites/research",
            site_id="contoso,research",
        )
        cls.active = SharepointSiteMember.objects.create(site=cls.site, user=cls.member)
        cls.inactive = SharepointSiteMember.objects.create(
            site=cls.site, user=cls.former, is_active=False
        )
        cls.other = SharepointSiteMember.objects.create(
            site=cls.other_site, user=cls.member
        )

    def test_dry_run_writes_nothing(self):
        out = StringIO()

        call_command("reset_site_members", dry_run=True, stdout=out)

        output = out.getvalue()
        self.assertIn("DRY RUN", output)
        self.assertIn("SharepointSiteMember: 3 row(s) deleted across 2 site(s)", output)
        self.assertIn("1 active, 1 inactive", output)
        self.assertEqual(SharepointSiteMember.objects.count(), 3)

    def test_reset_deletes_memberships_but_keeps_sites_and_users(self):
        out = StringIO()

        call_command("reset_site_members", interactive=False, stdout=out)

        self.assertIn("Site members reset", out.getvalue())
        self.assertFalse(SharepointSiteMember.objects.exists())
        self.assertTrue(SharepointSite.objects.filter(pk=self.site.pk).exists())
        self.assertTrue(SharepointSite.objects.filter(pk=self.other_site.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.member.pk).exists())

    def test_site_selector_matches_name_url_and_id(self):
        for selector in (self.site.name, self.site.url, self.site.site_id):
            with self.subTest(selector=selector):
                SharepointSiteMember.objects.all().delete()
                SharepointSiteMember.objects.create(site=self.site, user=self.member)
                SharepointSiteMember.objects.create(
                    site=self.other_site, user=self.member
                )

                call_command(
                    "reset_site_members",
                    site=[selector],
                    interactive=False,
                    stdout=StringIO(),
                )

                self.assertFalse(
                    SharepointSiteMember.objects.filter(site=self.site).exists()
                )
                self.assertTrue(
                    SharepointSiteMember.objects.filter(site=self.other_site).exists()
                )

    def test_inactive_only_keeps_current_memberships(self):
        out = StringIO()

        call_command(
            "reset_site_members",
            inactive_only=True,
            interactive=False,
            stdout=out,
        )

        self.assertIn("1 inactive row(s) deleted", out.getvalue())
        self.assertTrue(SharepointSiteMember.objects.filter(pk=self.active.pk).exists())
        self.assertTrue(SharepointSiteMember.objects.filter(pk=self.other.pk).exists())
        self.assertFalse(
            SharepointSiteMember.objects.filter(pk=self.inactive.pk).exists()
        )

    def test_unknown_site_is_rejected(self):
        with self.assertRaisesMessage(CommandError, "No mirrored site matches"):
            call_command(
                "reset_site_members", site=["does-not-exist"], stdout=StringIO()
            )

        self.assertEqual(SharepointSiteMember.objects.count(), 3)

    def test_ambiguous_site_is_rejected(self):
        SharepointSite.objects.create(
            name="Committee Library",
            url="https://contoso.sharepoint.com/sites/committee-archive",
            site_id="contoso,committee-archive",
        )

        with self.assertRaisesMessage(CommandError, "is ambiguous"):
            call_command(
                "reset_site_members",
                site=[self.site.name],
                interactive=False,
                stdout=StringIO(),
            )

        self.assertEqual(SharepointSiteMember.objects.count(), 3)

    def test_confirmation_refusal_cancels(self):
        with (
            patch("builtins.input", return_value="no"),
            self.assertRaises(CommandError),
        ):
            call_command("reset_site_members", stdout=StringIO())

        self.assertEqual(SharepointSiteMember.objects.count(), 3)
