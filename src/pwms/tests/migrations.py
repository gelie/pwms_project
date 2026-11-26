from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from ..models import (
    Group,
    InternationalResolution,
    WorkflowGroupAccess,
    WorkflowType,
)
from .base import make_state


class OwnerGroupEditGrantMigrationTests(TestCase):
    """Migration 0037 backfills the edit right onto already-materialised grants."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="mig-owner", password="pw")
        self.owner_group = Group.objects.create(
            name="Migration Owning Unit", group_type="division"
        )
        self.viewer_group = Group.objects.create(
            name="Migration Viewer Committee", group_type="portfolio_committee"
        )
        workflow_type = WorkflowType.objects.create(
            name="Migration Edit Type", group=self.owner_group
        )
        workflow_type.viewer_groups.add(self.viewer_group)
        state = make_state(workflow_type=workflow_type, name="Open", is_initial=True)
        self.resolution = InternationalResolution.objects.create(
            workflow_type=workflow_type,
            current_state=state,
            resolution_number="IR-MIG-1",
            title="Materialised before the change",
            owner=self.owner,
        )
        # Put the rows back in their pre-0037 shape: every materialised row was
        # view-only, the owning group's included.
        WorkflowGroupAccess.objects.filter(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=self.resolution.pk,
        ).update(can_edit=False)

    def _access(self, group):
        return WorkflowGroupAccess.objects.get(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=self.resolution.pk,
            group=group,
        )

    def _migration(self):
        return import_module("pwms.migrations.0037_widen_owner_group_edit_grant")

    def test_forward_widens_the_primary_grant_only(self):
        self._migration().widen_owner_group_edit(apps, None)

        self.assertTrue(self._access(self.owner_group).can_edit)
        # Viewer groups stay read-only — only the primary row is widened.
        self.assertFalse(self._access(self.viewer_group).can_edit)

    def test_reverse_narrows_the_primary_grant_again(self):
        migration = self._migration()
        migration.widen_owner_group_edit(apps, None)
        migration.narrow_owner_group_edit(apps, None)

        self.assertFalse(self._access(self.owner_group).can_edit)
        self.assertFalse(self._access(self.viewer_group).can_edit)


class OwnerGroupTransitionGrantMigrationTests(TestCase):
    """Migration 0040 backfills the transition right onto materialised grants."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="mig-trans-owner", password="pw")
        self.owner_group = Group.objects.create(
            name="Migration Transition Unit", group_type="division"
        )
        self.viewer_group = Group.objects.create(
            name="Migration Transition Committee", group_type="portfolio_committee"
        )
        workflow_type = WorkflowType.objects.create(
            name="Migration Transition Type", group=self.owner_group
        )
        workflow_type.viewer_groups.add(self.viewer_group)
        state = make_state(workflow_type=workflow_type, name="Open", is_initial=True)
        self.resolution = InternationalResolution.objects.create(
            workflow_type=workflow_type,
            current_state=state,
            resolution_number="IR-MIG-TR-1",
            title="Materialised before the transition default",
            owner=self.owner,
        )
        # Put the rows back in their pre-0040 shape: the primary grant already
        # carried the edit right (migration 0037) but not the transition one.
        WorkflowGroupAccess.objects.filter(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=self.resolution.pk,
        ).update(can_transition=False)

    def _access(self, group):
        return WorkflowGroupAccess.objects.get(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=self.resolution.pk,
            group=group,
        )

    def _migration(self):
        return import_module("pwms.migrations.0040_widen_owner_group_transition_grant")

    def test_forward_widens_the_primary_grant_only(self):
        self._migration().widen_owner_group_transition(apps, None)

        self.assertTrue(self._access(self.owner_group).can_transition)
        # Viewer groups stay read-only — only the primary row is widened.
        self.assertFalse(self._access(self.viewer_group).can_transition)

    def test_reverse_narrows_the_primary_grant_again(self):
        migration = self._migration()
        migration.widen_owner_group_transition(apps, None)
        migration.narrow_owner_group_transition(apps, None)

        self.assertFalse(self._access(self.owner_group).can_transition)
        self.assertFalse(self._access(self.viewer_group).can_transition)
