"""``reset_groups`` empties groups and memberships without disturbing other data.

The command is destructive by design, so these tests pin the two things that make
it usable: a dry run really writes nothing, and a real run leaves everything that
is not group data intact (users, roles, workflow types) while resolving the two
``PROTECT`` references that would otherwise block the delete.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .models import (
    Bill,
    DutyDelegation,
    Group,
    GroupMembership,
    Role,
    WorkflowReferral,
    WorkflowType,
)


class ResetGroupsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="rg-user", password="pw")
        cls.role = Role.objects.create(name="RG Member")
        cls.group = Group.objects.create(name="RG Committee", group_type="committee")
        cls.child = Group.objects.create(
            name="RG Subcommittee", group_type="committee", parent=cls.group
        )
        cls.membership = GroupMembership.objects.create(
            user=cls.user, group=cls.group, role=cls.role
        )
        cls.delegate = User.objects.create_user(username="rg-delegate", password="pw")
        cls.delegation = DutyDelegation.objects.create(
            group=cls.group,
            delegator=cls.user,
            delegate=cls.delegate,
            end_date=timezone.localdate() + timedelta(days=7),
        )
        cls.workflow_type = WorkflowType.objects.create(
            name="RG Workflow", group=cls.group
        )
        # bulk_create skips WorkflowReferral.save(), which would try to resolve
        # the (deliberately absent) workflow instance through its generic FK.
        WorkflowReferral.objects.bulk_create(
            [
                WorkflowReferral(
                    content_type=ContentType.objects.get_for_model(Bill),
                    object_id=999_999,
                    referred_to=cls.group,
                    referred_by=cls.user,
                )
            ]
        )
        User.objects.filter(pk=cls.user.pk).update(department=cls.group)

    def test_dry_run_writes_nothing(self):
        out = StringIO()

        call_command("reset_groups", dry_run=True, stdout=out)

        output = out.getvalue()
        self.assertIn("DRY RUN", output)
        self.assertIn("WorkflowReferral: 1 deleted", output)
        self.assertTrue(Group.objects.filter(pk=self.group.pk).exists())
        self.assertTrue(Group.objects.filter(pk=self.child.pk).exists())
        self.assertTrue(GroupMembership.objects.filter(pk=self.membership.pk).exists())
        self.assertTrue(DutyDelegation.objects.filter(pk=self.delegation.pk).exists())
        self.assertTrue(WorkflowReferral.objects.exists())
        self.assertEqual(
            WorkflowType.objects.get(pk=self.workflow_type.pk).group_id,
            self.group.pk,
        )
        self.assertEqual(
            get_user_model().objects.get(pk=self.user.pk).department_id,
            self.group.pk,
        )

    def test_reset_deletes_groups_and_memberships(self):
        out = StringIO()

        call_command("reset_groups", interactive=False, stdout=out)

        self.assertIn("Groups reset", out.getvalue())
        self.assertFalse(Group.objects.exists())
        self.assertFalse(GroupMembership.objects.exists())
        self.assertFalse(DutyDelegation.objects.exists())
        self.assertFalse(WorkflowReferral.objects.exists())
        # Workflow types survive; only their owning group is unpointed.
        self.assertIsNone(WorkflowType.objects.get(pk=self.workflow_type.pk).group_id)
        # Non-group data is untouched.
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertTrue(Role.objects.filter(pk=self.role.pk).exists())
        self.assertIsNone(get_user_model().objects.get(pk=self.user.pk).department_id)

    def test_memberships_only_keeps_groups_and_references(self):
        out = StringIO()

        call_command(
            "reset_groups", memberships_only=True, interactive=False, stdout=out
        )

        self.assertIn("groups and their references untouched", out.getvalue())
        self.assertFalse(GroupMembership.objects.exists())
        self.assertFalse(DutyDelegation.objects.exists())
        self.assertTrue(Group.objects.filter(pk=self.group.pk).exists())
        self.assertTrue(WorkflowReferral.objects.exists())
        self.assertEqual(
            WorkflowType.objects.get(pk=self.workflow_type.pk).group_id,
            self.group.pk,
        )
        self.assertEqual(
            get_user_model().objects.get(pk=self.user.pk).department_id,
            self.group.pk,
        )

    def test_confirmation_refusal_cancels(self):
        with (
            patch("builtins.input", return_value="no"),
            self.assertRaises(CommandError),
        ):
            call_command("reset_groups", stdout=StringIO())

        self.assertTrue(Group.objects.filter(pk=self.group.pk).exists())
        self.assertTrue(GroupMembership.objects.filter(pk=self.membership.pk).exists())
