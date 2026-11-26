from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from ..models import (
    DelegationReport,
    Group,
    GroupMembership,
    InternationalResolution,
    Role,
    WorkflowGroupAccess,
    WorkflowRolePermission,
    WorkflowType,
)
from ..services.permissions import (
    COMMENT,
    DELETE,
    EDIT,
    MANAGE,
    RESOURCE_ACTIONS,
    SHARE,
    TRANSITION,
    VIEW,
    permissions_for,
    require,
    resolve,
)
from .base import make_state


class PermissionResolverTests(TestCase):
    """The unified permission resolver service (view/edit/delete/share/comment/manage)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="resolver", password="pw")
        cls.group = Group.objects.create(
            name="Resolver Committee", group_type="portfolio_committee"
        )
        cls.role = Role.objects.create(name="Resolver Member")
        GroupMembership.objects.create(user=cls.user, group=cls.group, role=cls.role)

        cls.wt = WorkflowType.objects.create(name="Resolver Test Type")
        cls.state = make_state(workflow_type=cls.wt, name="Open", is_initial=True)
        cls.resolution = InternationalResolution.objects.create(
            workflow_type=cls.wt,
            current_state=cls.state,
            resolution_number="IR-RESOLVER-1",
            title="Resolver resolution",
            owner=cls.user,
        )
        cls.access = WorkflowGroupAccess.objects.create(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=cls.resolution.pk,
            group=cls.group,
            can_view=True,
            can_edit=True,
            can_comment=True,
        )

    def test_permissions_for_returns_the_six_capabilities(self):
        result = permissions_for(self.user, self.resolution)
        self.assertEqual(set(result), set(RESOURCE_ACTIONS))
        self.assertTrue(result[VIEW])
        self.assertTrue(result[EDIT])
        self.assertTrue(result[COMMENT])
        # The owner may always view/edit/delete their own workflow.
        self.assertTrue(result[DELETE])
        self.assertFalse(result[SHARE])
        self.assertFalse(result[MANAGE])

    def test_resolve_reads_group_defaults(self):
        self.assertTrue(resolve(self.user, self.resolution, VIEW))
        self.assertTrue(resolve(self.user, self.resolution, EDIT))
        self.assertTrue(resolve(self.user, self.resolution, COMMENT))
        # Owner grant, not the group defaults, authorises delete here.
        self.assertTrue(resolve(self.user, self.resolution, DELETE))
        self.assertFalse(resolve(self.user, self.resolution, SHARE))
        self.assertFalse(resolve(self.user, self.resolution, MANAGE))

    def test_superuser_is_granted_everything(self):
        User = get_user_model()
        root = User.objects.create_superuser(username="resolver_root", password="pw")
        for action in RESOURCE_ACTIONS:
            self.assertTrue(resolve(root, self.resolution, action), action)

    def test_anonymous_is_denied_everything(self):
        for action in RESOURCE_ACTIONS:
            self.assertFalse(resolve(None, self.resolution, action), action)

    def test_manage_via_global_role_flag(self):
        self.role.can_manage_permissions = True
        self.role.save(update_fields=["can_manage_permissions"])
        self.assertTrue(resolve(self.user, self.resolution, MANAGE))
        # The global flag does not implicitly grant the other mutations.
        self.assertFalse(resolve(self.user, self.resolution, SHARE))

    def test_manage_via_instance_flag(self):
        self.access.can_manage = True
        self.access.save(update_fields=["can_manage"])
        self.assertTrue(resolve(self.user, self.resolution, MANAGE))

    def test_role_override_wins_for_a_new_action(self):
        WorkflowRolePermission.objects.create(
            group_access=self.access,
            role=self.role,
            can_share=True,
        )
        self.assertTrue(resolve(self.user, self.resolution, SHARE))

    def test_unknown_action_raises(self):
        with self.assertRaises(ValueError):
            resolve(self.user, self.resolution, "explode")

    def test_require_raises_permission_denied(self):
        with self.assertRaises(PermissionDenied):
            require(self.user, self.resolution, SHARE)
        require(self.user, self.resolution, VIEW)  # should not raise

    def test_default_resolver_for_unregistered_resource(self):
        # A Group has no registered resolver: view/comment are open, mutations
        # are gated on the global manage capability.
        self.assertTrue(resolve(self.user, self.group, VIEW))
        self.assertTrue(resolve(self.user, self.group, COMMENT))
        self.assertFalse(resolve(self.user, self.group, EDIT))
        self.assertFalse(resolve(self.user, self.group, MANAGE))


class WorkflowPermissionInheritanceTests(TestCase):
    """A child workflow inherits its parent's effective permissions."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.parent_owner = User.objects.create_user(
            username="inherit_parent_owner", password="pw"
        )
        cls.child_owner = User.objects.create_user(
            username="inherit_child_owner", password="pw"
        )
        cls.colleague = User.objects.create_user(
            username="inherit_colleague", password="pw"
        )
        cls.group = Group.objects.create(
            name="Inheritance Committee", group_type="portfolio_committee"
        )
        cls.role = Role.objects.create(name="Inheritance Member")
        GroupMembership.objects.create(
            user=cls.colleague, group=cls.group, role=cls.role
        )

        cls.report_type = WorkflowType.objects.get(name="Delegation Report")
        cls.resolution_type = WorkflowType.objects.get(name="International Resolution")
        cls.report = DelegationReport.objects.create(
            workflow_type=cls.report_type,
            current_state=cls.report_type.get_initial_state(),
            title="Parent report",
            owner=cls.parent_owner,
        )
        cls.resolution = InternationalResolution.objects.create(
            workflow_type=cls.resolution_type,
            current_state=cls.resolution_type.get_initial_state(),
            resolution_number="IR-INHERIT-1",
            title="Child resolution",
            owner=cls.child_owner,
        )
        cls.report.add_sub_workflow(cls.resolution)

    def test_parent_group_grant_cascades_to_child(self):
        """A group grant on the report reaches the resolution it contains."""
        self.assertFalse(resolve(self.colleague, self.resolution, VIEW))

        WorkflowGroupAccess.objects.create(
            content_type=ContentType.objects.get_for_model(DelegationReport),
            object_id=self.report.pk,
            group=self.group,
            can_view=True,
        )

        self.assertTrue(resolve(self.colleague, self.report, VIEW))
        self.assertTrue(resolve(self.colleague, self.resolution, VIEW))

    def test_parent_owner_view_cascades_to_child_only(self):
        """The report owner may view the child, but not edit/delete it."""
        self.assertEqual(self.resolution.owner, self.child_owner)
        self.assertTrue(resolve(self.parent_owner, self.resolution, VIEW))
        self.assertFalse(resolve(self.parent_owner, self.resolution, EDIT))
        self.assertFalse(resolve(self.parent_owner, self.resolution, DELETE))

    def test_only_view_cascades_to_child(self):
        """A parent grant lends view access only; mutations stay per-instance."""
        WorkflowGroupAccess.objects.create(
            content_type=ContentType.objects.get_for_model(DelegationReport),
            object_id=self.report.pk,
            group=self.group,
            can_view=True,
            can_edit=True,
            can_transition=True,
        )
        self.assertTrue(resolve(self.colleague, self.resolution, VIEW))
        self.assertFalse(resolve(self.colleague, self.resolution, EDIT))
        self.assertFalse(resolve(self.colleague, self.resolution, "transition"))

        # The child's own grant still governs its own mutations.
        WorkflowGroupAccess.objects.create(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=self.resolution.pk,
            group=self.group,
            can_edit=True,
        )
        self.assertTrue(resolve(self.colleague, self.resolution, EDIT))

    def test_child_grant_does_not_leak_up_to_parent(self):
        """Inheritance is downward only."""
        WorkflowGroupAccess.objects.create(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=self.resolution.pk,
            group=self.group,
            can_view=True,
        )

        self.assertTrue(resolve(self.colleague, self.resolution, VIEW))
        self.assertFalse(resolve(self.colleague, self.report, VIEW))

    def test_unrelated_user_is_still_denied(self):
        User = get_user_model()
        stranger = User.objects.create_user(username="inherit_stranger", password="pw")
        self.assertFalse(resolve(stranger, self.report, VIEW))
        self.assertFalse(resolve(stranger, self.resolution, VIEW))

    def test_assignment_confers_view_only(self):
        """Assigned work becomes visible to the assignee, and nothing more."""
        self.assertFalse(resolve(self.colleague, self.report, VIEW))

        self.report.assigned_to = self.colleague
        self.report.save(update_fields=["assigned_to"])

        self.assertTrue(resolve(self.colleague, self.report, VIEW))
        # Assignment must not widen anyone's authority over the record.
        self.assertFalse(resolve(self.colleague, self.report, EDIT))
        self.assertFalse(resolve(self.colleague, self.report, DELETE))
        self.assertFalse(resolve(self.colleague, self.report, TRANSITION))

    def test_assignment_view_cascades_to_child_workflows(self):
        """An assignee can read what the assigned workflow contains."""
        self.report.assigned_to = self.colleague
        self.report.save(update_fields=["assigned_to"])

        self.assertTrue(resolve(self.colleague, self.resolution, VIEW))
        self.assertFalse(resolve(self.colleague, self.resolution, EDIT))

    def test_assignment_does_not_leak_up_to_the_parent(self):
        """Assigning a child does not reveal the report it sits under."""
        self.resolution.assigned_to = self.colleague
        self.resolution.save(update_fields=["assigned_to"])

        self.assertTrue(resolve(self.colleague, self.resolution, VIEW))
        self.assertFalse(resolve(self.colleague, self.report, VIEW))

    def test_assigned_user_can_find_and_open_their_work(self):
        """The rule reaches the views: the assignee sees the row and the page."""
        self.report.assigned_to = self.colleague
        self.report.save(update_fields=["assigned_to"])
        self.client.force_login(self.colleague)

        listing = self.client.get(reverse("pwms:delegation_reports"))
        self.assertContains(listing, self.report.reference_number)

        detail_url = reverse(
            "pwms:delegation_report_detail", args=[self.report.public_id]
        )
        self.assertEqual(self.client.get(detail_url).status_code, 200)

        # Reading only: the edit page stays closed to an assignee, so the
        # permission-driven buttons are absent from the page as well.
        edit_url = reverse(
            "pwms:delegation_report_update", args=[self.report.public_id]
        )
        self.assertEqual(self.client.get(edit_url).status_code, 403)
        self.assertNotContains(self.client.get(detail_url), edit_url)

    def test_parent_owner_can_open_child_detail_page(self):
        """The cascade is wired into the views, not just the resolver."""
        self.client.force_login(self.parent_owner)
        report_page = self.client.get(
            reverse("pwms:delegation_report_detail", args=[self.report.public_id])
        )
        self.assertEqual(report_page.status_code, 200)
        # The contained resolution now appears on the report page...
        self.assertContains(report_page, "IR-INHERIT-1")
        # ...and opens directly for the report owner.
        resolution_page = self.client.get(
            reverse(
                "pwms:international_resolution_detail", args=[self.resolution.public_id]
            )
        )
        self.assertEqual(resolution_page.status_code, 200)

        # The child's own owner gains nothing on the parent (downward only).
        self.client.force_login(self.child_owner)
        parent_page = self.client.get(
            reverse("pwms:delegation_report_detail", args=[self.report.public_id])
        )
        self.assertEqual(parent_page.status_code, 403)
