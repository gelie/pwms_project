import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from ..models import (
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Role,
    WorkflowGroupAccess,
    WorkflowRolePermission,
    WorkflowType,
)
from ..navigation import CREATE_URL_NAMES, creatable_workflow_types
from ..services.permissions import (
    DELETE,
    EDIT,
    TRANSITION,
    VIEW,
    resolve,
)
from .base import make_state


class ViewerGroupAccessTests(TestCase):
    """Type-level viewer groups become read-only access rows on creation."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.creator = User.objects.create_user(username="viewer-creator", password="pw")
        cls.viewer = User.objects.create_user(username="viewer-member", password="pw")
        cls.viewer_group = Group.objects.create(
            name="Interested Portfolio Committee", group_type="portfolio_committee"
        )
        cls.viewer_role = Role.objects.create(name="Interested Viewer")
        GroupMembership.objects.create(
            user=cls.viewer, group=cls.viewer_group, role=cls.viewer_role
        )
        cls.wt = WorkflowType.objects.create(name="Test Viewer Type")
        cls.wt.viewer_groups.add(cls.viewer_group)
        cls.state = make_state(workflow_type=cls.wt, name="Open", is_initial=True)

    def _resolution(self, **overrides):
        data = {
            "workflow_type": self.wt,
            "current_state": self.state,
            "resolution_number": "IR-VIEW-1",
            "title": "Viewer access",
            "owner": self.creator,
        }
        data.update(overrides)
        return InternationalResolution.objects.create(**data)

    def _access(self, instance):
        return WorkflowGroupAccess.objects.get(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
            group=self.viewer_group,
        )

    def test_creation_materialises_readonly_access_for_viewer_groups(self):
        access = self._access(self._resolution())
        self.assertTrue(access.can_view)
        for flag in (
            "can_edit",
            "can_delete",
            "can_share",
            "can_comment",
            "can_manage",
            "can_transition",
        ):
            self.assertFalse(getattr(access, flag))

    def test_viewer_group_member_can_view_but_nothing_else(self):
        resolution = self._resolution()
        self.assertTrue(resolve(self.viewer, resolution, VIEW))
        self.assertFalse(resolve(self.viewer, resolution, EDIT))
        self.assertFalse(resolve(self.viewer, resolution, DELETE))
        self.assertFalse(resolve(self.viewer, resolution, TRANSITION))

    def test_type_without_viewer_groups_creates_no_access_rows(self):
        plain_type = WorkflowType.objects.create(name="Test Plain Type")
        plain_state = make_state(workflow_type=plain_type, name="Open", is_initial=True)
        resolution = InternationalResolution.objects.create(
            workflow_type=plain_type,
            current_state=plain_state,
            resolution_number="IR-PLAIN-1",
            title="No viewers",
            owner=self.creator,
        )
        self.assertFalse(
            WorkflowGroupAccess.objects.filter(
                content_type=ContentType.objects.get_for_model(InternationalResolution),
                object_id=resolution.pk,
            ).exists()
        )

    def test_subclass_save_override_still_materialises_access(self):
        # InternationalAgreement overrides save(); the hook must still run via
        # super() so viewer sharing is not lost on subclasses.
        agreement = InternationalAgreement.objects.create(
            workflow_type=self.wt,
            current_state=self.state,
            title="Overridden save",
            owner=self.creator,
        )
        self.assertTrue(self._access(agreement).can_view)

    def test_later_save_does_not_duplicate_or_reset_access(self):
        resolution = self._resolution()
        access = self._access(resolution)
        # An administrator upgrades the materialised grant; a later save of the
        # instance must neither duplicate the row nor reset the edited flags.
        access.can_comment = True
        access.save()

        resolution.title = "Updated"
        resolution.save()

        access.refresh_from_db()
        self.assertTrue(access.can_comment)
        self.assertEqual(
            WorkflowGroupAccess.objects.filter(
                content_type=ContentType.objects.get_for_model(InternationalResolution),
                object_id=resolution.pk,
                group=self.viewer_group,
            ).count(),
            1,
        )

    def test_creation_materialises_primary_access_for_owner_group(self):
        owner_group = Group.objects.create(name="Owning Unit", group_type="division")
        wt = WorkflowType.objects.create(name="Test Owned Type", group=owner_group)
        state = make_state(workflow_type=wt, name="Open", is_initial=True)
        resolution = InternationalResolution.objects.create(
            workflow_type=wt,
            current_state=state,
            resolution_number="IR-OWN-1",
            title="Owned",
            owner=self.creator,
        )

        access = WorkflowGroupAccess.objects.get(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=resolution.pk,
            group=owner_group,
        )
        self.assertTrue(access.is_primary)
        self.assertTrue(access.can_view)
        # The owning unit works on its own records: it may edit them and move them
        # on. Every other capability still starts off.
        self.assertTrue(access.can_edit)
        self.assertTrue(access.can_transition)
        for flag in (
            "can_delete",
            "can_share",
            "can_comment",
            "can_manage",
        ):
            self.assertFalse(getattr(access, flag))

    def test_owner_group_also_listed_as_viewer_is_not_downgraded(self):
        owner_group = Group.objects.create(name="Dual Role Unit", group_type="division")
        wt = WorkflowType.objects.create(name="Test Dual Type", group=owner_group)
        wt.viewer_groups.add(owner_group)
        state = make_state(workflow_type=wt, name="Open", is_initial=True)
        resolution = InternationalResolution.objects.create(
            workflow_type=wt,
            current_state=state,
            resolution_number="IR-DUAL-1",
            title="Dual role",
            owner=self.creator,
        )

        rows = WorkflowGroupAccess.objects.filter(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=resolution.pk,
            group=owner_group,
        )
        self.assertEqual(rows.count(), 1)
        self.assertTrue(rows.first().is_primary)

    def test_owner_group_may_edit_and_move_its_records_but_a_viewer_group_may_not(
        self,
    ):
        owner_group = Group.objects.create(name="Owning Editors", group_type="division")
        owner_role = Role.objects.create(name="Owning Editor")
        member = get_user_model().objects.create_user(
            username="owning-editor", password="pw"
        )
        GroupMembership.objects.create(user=member, group=owner_group, role=owner_role)
        wt = WorkflowType.objects.create(name="Test Owned Edit Type", group=owner_group)
        wt.viewer_groups.add(self.viewer_group)
        state = make_state(workflow_type=wt, name="Open", is_initial=True)
        resolution = InternationalResolution.objects.create(
            workflow_type=wt,
            current_state=state,
            resolution_number="IR-OWN-EDIT-1",
            title="Owned to edit",
            owner=self.creator,
        )

        # The type's owning unit can work on its records and move them on. The
        # rights are the owning group's alone, and neither extends to deleting.
        self.assertTrue(resolve(member, resolution, VIEW))
        self.assertTrue(resolve(member, resolution, EDIT))
        self.assertTrue(resolve(member, resolution, TRANSITION))
        self.assertFalse(resolve(member, resolution, DELETE))
        # A viewer group stays read-only.
        self.assertTrue(resolve(self.viewer, resolution, VIEW))
        self.assertFalse(resolve(self.viewer, resolution, EDIT))
        self.assertFalse(resolve(self.viewer, resolution, TRANSITION))

    def test_materialisation_honours_the_type_capability_policy(self):
        # The owning group's capabilities are the type's policy, so a type can
        # narrow or widen them for every instance at once instead of per instance.
        owner_group = Group.objects.create(
            name="Policy Owning Unit", group_type="division"
        )
        owner_role = Role.objects.create(name="Policy Owner")
        member = get_user_model().objects.create_user(
            username="policy-owner", password="pw"
        )
        GroupMembership.objects.create(user=member, group=owner_group, role=owner_role)
        wt = WorkflowType.objects.create(
            name="Test Policy Type",
            group=owner_group,
            owner_can_edit=False,
            owner_can_transition=False,
            owner_can_delete=True,
        )
        state = make_state(workflow_type=wt, name="Open", is_initial=True)
        resolution = InternationalResolution.objects.create(
            workflow_type=wt,
            current_state=state,
            resolution_number="IR-POLICY-1",
            title="Policy",
            owner=self.creator,
        )

        access = WorkflowGroupAccess.objects.get(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=resolution.pk,
            group=owner_group,
        )
        self.assertTrue(access.can_view)
        self.assertFalse(access.can_edit)
        self.assertFalse(access.can_transition)
        self.assertTrue(access.can_delete)
        # The resolver reads the same row, so the policy is what members get.
        self.assertTrue(resolve(member, resolution, VIEW))
        self.assertFalse(resolve(member, resolution, EDIT))
        self.assertFalse(resolve(member, resolution, TRANSITION))
        self.assertTrue(resolve(member, resolution, DELETE))

    def test_owner_access_defaults_maps_the_type_policy_to_access_flags(self):
        wt = WorkflowType.objects.create(name="Test Defaults Type")

        self.assertEqual(
            wt.owner_access_defaults(),
            {
                "is_primary": True,
                "can_view": True,
                "can_edit": True,
                "can_transition": True,
                "can_delete": False,
                "can_share": False,
                "can_comment": False,
                "can_manage": False,
            },
        )


class SyncTypeGroupAccessCommandTests(TestCase):
    """The backfill command re-materialises type access for existing instances."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="sync-owner", password="pw")
        cls.owner_group = Group.objects.create(
            name="Sync Owning Unit", group_type="division"
        )
        cls.viewer_group = Group.objects.create(
            name="Sync Viewer Committee", group_type="portfolio_committee"
        )
        cls.wt = WorkflowType.objects.create(
            name="Test Sync Type", group=cls.owner_group
        )
        cls.wt.viewer_groups.add(cls.viewer_group)
        cls.state = make_state(workflow_type=cls.wt, name="Open", is_initial=True)
        cls.resolution = InternationalResolution.objects.create(
            workflow_type=cls.wt,
            current_state=cls.state,
            resolution_number="IR-SYNC-1",
            title="Backfill me",
            owner=cls.owner,
        )

    def _rows(self):
        return WorkflowGroupAccess.objects.filter(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=self.resolution.pk,
        )

    def test_creation_materialised_both_owner_and_viewer_rows(self):
        self.assertEqual(self._rows().count(), 2)
        self.assertTrue(self._rows().get(group=self.owner_group).is_primary)

    def test_command_restores_missing_rows(self):
        self._rows().delete()

        out = StringIO()
        call_command("sync_type_group_access", stdout=out)

        groups = set(self._rows().values_list("group__name", flat=True))
        self.assertEqual(groups, {self.owner_group.name, self.viewer_group.name})
        self.assertTrue(self._rows().get(group=self.owner_group).is_primary)
        self.assertIn("added 2 row(s)", out.getvalue())

    def test_dry_run_creates_nothing(self):
        self._rows().delete()

        out = StringIO()
        call_command("sync_type_group_access", "--dry-run", stdout=out)

        self.assertEqual(self._rows().count(), 0)
        self.assertIn("DRY RUN", out.getvalue())

    def test_command_is_idempotent(self):
        original = self._rows().count()

        out = StringIO()
        call_command("sync_type_group_access", stdout=out)

        self.assertEqual(self._rows().count(), original)
        self.assertIn("added 0 row(s)", out.getvalue())

    def test_command_scopes_to_one_workflow_type(self):
        out = StringIO()
        call_command(
            "sync_type_group_access",
            "--workflow-type",
            "Test Sync Type",
            stdout=out,
        )
        self.assertIn("Test Sync Type", out.getvalue())

    def test_command_does_not_overwrite_a_narrowed_owner_grant(self):
        # The command only creates missing rows, so it never re-widens a grant an
        # administrator has narrowed; migration 0037 handles the pre-change rows.
        owner_access = self._rows().get(group=self.owner_group)
        owner_access.can_edit = False
        owner_access.save(update_fields=["can_edit"])

        out = StringIO()
        call_command("sync_type_group_access", stdout=out)

        owner_access.refresh_from_db()
        self.assertFalse(owner_access.can_edit)
        self.assertIn("added 0 row(s)", out.getvalue())

    def test_update_existing_reconciles_the_primary_grant_to_the_type_policy(self):
        owner_access = self._rows().get(group=self.owner_group)
        # An administrator narrows edit and adds delete by hand.
        owner_access.can_edit = False
        owner_access.can_delete = True
        owner_access.save(update_fields=["can_edit", "can_delete"])

        # Without the flag an edited row is left alone, as documented.
        call_command("sync_type_group_access", stdout=StringIO())
        owner_access.refresh_from_db()
        self.assertFalse(owner_access.can_edit)
        self.assertTrue(owner_access.can_delete)

        # With it the type's policy wins in both directions: edit comes back and
        # the hand-granted delete goes.
        out = StringIO()
        call_command("sync_type_group_access", "--update-existing", stdout=out)
        owner_access.refresh_from_db()
        self.assertTrue(owner_access.can_edit)
        self.assertFalse(owner_access.can_delete)
        self.assertIn("refreshed owning-group grant", out.getvalue())
        self.assertIn("can_delete, can_edit", out.getvalue())

    def test_update_existing_touches_no_other_row(self):
        # Give the owning group a role override and narrow the viewer's row, then
        # reconcile: only the owning group's own base flags may change.
        owner_access = self._rows().get(group=self.owner_group)
        owner_access.can_edit = False
        owner_access.save(update_fields=["can_edit"])
        role = Role.objects.create(name="Sync Owner Role")
        role_perm = WorkflowRolePermission.objects.create(
            group_access=owner_access, role=role, can_edit=True
        )
        viewer_access = self._rows().get(group=self.viewer_group)
        viewer_access.can_view = False
        viewer_access.save(update_fields=["can_view"])

        call_command("sync_type_group_access", "--update-existing", stdout=StringIO())

        owner_access.refresh_from_db()
        viewer_access.refresh_from_db()
        role_perm.refresh_from_db()
        self.assertTrue(owner_access.can_edit)
        self.assertTrue(role_perm.can_edit)
        # A viewer group is not the owning group, so its row is not the policy's
        # business: the narrowed view stays narrowed.
        self.assertFalse(viewer_access.can_view)

    def test_update_existing_dry_run_writes_nothing(self):
        owner_access = self._rows().get(group=self.owner_group)
        owner_access.can_edit = False
        owner_access.save(update_fields=["can_edit"])

        out = StringIO()
        call_command(
            "sync_type_group_access", "--update-existing", "--dry-run", stdout=out
        )

        owner_access.refresh_from_db()
        self.assertFalse(owner_access.can_edit)
        self.assertIn("would refresh", out.getvalue())

    def test_unknown_workflow_type_raises(self):
        with self.assertRaises(CommandError):
            call_command("sync_type_group_access", "--workflow-type", "Nope")


class CreatableWorkflowMenuTests(TestCase):
    """The "Create a workflow" menu matches a type by name, not by its slug.

    ``AutoSlugField`` writes a workflow type's slug once, on creation, so a type
    that is renamed — or re-created while an older row still holds the slug —
    keeps a diverged slug. A menu keyed on the slug silently dropped that type; it
    is now keyed on the name the create view itself gates on
    (``WorkflowInstanceFormMixin.initial_workflow_type``).
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.group = Group.objects.create(name="Bill Owning Unit", group_type="division")
        cls.role = Role.objects.create(name="Bill Creator")
        cls.user = User.objects.create_user(username="bill-creator", password="pw")
        GroupMembership.objects.create(user=cls.user, group=cls.group, role=cls.role)
        cls.bill_type = WorkflowType.objects.get(name="Bill")
        cls.bill_type.group = cls.group
        cls.bill_type.create_roles.add(cls.role)
        # The collision that hid the type: it is named "Bill" but its slug is not
        # "bill" (an older, renamed type still held the slug when this one was
        # created, so the uniquifier suffixed it).
        cls.bill_type.slug = "bill-1"
        cls.bill_type.save(update_fields=["slug", "group"])

    def test_menu_lists_a_registered_type_whose_slug_diverged(self):
        entries = {e["name"]: e["url"] for e in creatable_workflow_types(self.user)}
        self.assertEqual(entries.get("Bill"), reverse("pwms:bill_create"))

    def test_menu_omits_a_type_with_no_registered_create_view(self):
        unregistered = WorkflowType.objects.create(
            name="Unregistered Type", group=self.group
        )
        unregistered.create_roles.add(self.role)

        names = [e["name"] for e in creatable_workflow_types(self.user)]
        self.assertNotIn("Unregistered Type", names)

    def test_registry_keys_are_the_type_names_the_create_views_pin(self):
        # The registry is derived from the forms, so its keys are exactly the
        # names those create views bind their instances to — no drift possible.
        self.assertEqual(
            set(CREATE_URL_NAMES),
            {
                "Delegation Report",
                "International Resolution",
                "International Agreement",
                "Bill",
                "Meeting",
            },
        )


class AuditWorkflowAccessCommandTests(TestCase):
    """The audit command explains the three RBAC gates in one report.

    Every call is scoped to this class's own type: the data migrations seed the
    real workflow types, whose group (the Oracle-synced IRPD unit) does not exist
    in a test database, so an unscoped run would rightly flag those too.
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner_group = Group.objects.create(
            name="Audit Owning Unit", group_type="division"
        )
        cls.other_group = Group.objects.create(
            name="Audit Other Unit", group_type="division"
        )
        cls.role = Role.objects.create(name="Audit Creator")
        cls.wt = WorkflowType.objects.create(name="Audit Type", group=cls.owner_group)
        cls.wt.create_roles.add(cls.role)
        cls.state = make_state(workflow_type=cls.wt, name="Open", is_initial=True)
        cls.owner = User.objects.create_user(username="audit-owner", password="pw")
        GroupMembership.objects.create(
            user=cls.owner, group=cls.owner_group, role=cls.role
        )
        cls.resolution = InternationalResolution.objects.create(
            workflow_type=cls.wt,
            current_state=cls.state,
            resolution_number="IR-AUDIT-1",
            title="Audit me",
            owner=cls.owner,
        )

    def _run(self, *args, out=None):
        """Run the command scoped to this class's type, capturing its output."""
        out = out if out is not None else StringIO()
        call_command(
            "audit_workflow_access",
            "--workflow-type",
            "Audit Type",
            *args,
            stdout=out,
        )
        return out

    def test_reports_a_correctly_configured_user_can_create(self):
        out = self._run("--user", "audit-owner")
        text = out.getvalue()
        self.assertIn("Audit Type", text)
        self.assertIn("active member of 'Audit Owning Unit'", text)
        self.assertIn("groups   : active in 'Audit Owning Unit'", text)

    def test_flags_a_create_role_no_active_member_of_the_group_holds(self):
        # The role is declared, but the group has nobody active holding it.
        GroupMembership.objects.filter(user=self.owner, role=self.role).delete()
        out = StringIO()
        with self.assertRaises(CommandError):
            self._run(out=out)
        self.assertIn("has no active member", out.getvalue())

    def test_explains_a_user_holding_the_role_in_the_wrong_group(self):
        # The classic trap: the role is right, the group is not.
        User = get_user_model()
        sbrown = User.objects.create_user(username="audit-sbrown", password="pw")
        GroupMembership.objects.create(
            user=sbrown, group=self.other_group, role=self.role
        )

        text = self._run("--user", "audit-sbrown").getvalue()
        self.assertIn("does not belong to 'Audit Owning Unit'", text)
        self.assertIn("holds a create role elsewhere", text)
        self.assertIn("Audit Creator in 'Audit Other Unit'", text)
        self.assertIn("groups   : active in 'Audit Other Unit'", text)
        self.assertIn("cannot create 'Audit Type'", text)

    def test_strict_turns_the_warning_into_a_failure(self):
        User = get_user_model()
        sbrown = User.objects.create_user(username="audit-sbrown", password="pw")
        GroupMembership.objects.create(
            user=sbrown, group=self.other_group, role=self.role
        )

        # Not strict: the wrong-group holder is only a warning.
        self._run("--user", "audit-sbrown")
        with self.assertRaises(CommandError):
            self._run("--user", "audit-sbrown", "--strict")

    def test_inactive_membership_denies_creation(self):
        # A colleague keeps the role alive, so the failure is the user's alone.
        User = get_user_model()
        colleague = User.objects.create_user(username="audit-colleague", password="pw")
        GroupMembership.objects.create(
            user=colleague, group=self.owner_group, role=self.role
        )
        GroupMembership.objects.filter(user=self.owner, role=self.role).update(
            is_active=False
        )

        text = self._run("--user", "audit-owner").getvalue()
        self.assertIn(
            "membership in 'Audit Owning Unit' (Audit Creator) is not active", text
        )

    def test_warns_when_a_creator_cannot_see_existing_records(self):
        # Gate 3: the owner may create, but their group lost the record grant.
        WorkflowGroupAccess.objects.filter(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=self.resolution.pk,
        ).delete()

        text = self._run("--user", "audit-owner").getvalue()
        self.assertIn("cannot see all of its existing records", text)
        self.assertIn("sync_type_group_access", text)

    def test_warns_when_a_createable_type_has_no_registered_create_view(self):
        # "Audit Type" is enabled with create roles but, being test-only, its name
        # is not in pwms.navigation.CREATE_VIEWS, so it never reaches the menu.
        text = self._run("--user", "audit-owner").getvalue()
        self.assertIn("no registered create view", text)
        self.assertIn("Create-a-workflow menu", text)

    def test_strict_turns_the_missing_create_view_into_a_failure(self):
        with self.assertRaises(CommandError):
            self._run("--user", "audit-owner", "--strict")

    def test_json_output_is_machine_readable(self):
        out = self._run("--user", "audit-owner", "--json")
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["user"], "audit-owner")
        self.assertEqual(payload["errors"], 0)
        entry = payload["types"][0]
        self.assertEqual(entry["name"], "Audit Type")
        self.assertTrue(entry["user"]["can_create"])
        self.assertEqual(entry["create_roles"][0]["active_members"], 1)

    def test_scopes_to_one_workflow_type(self):
        out = self._run()
        self.assertIn("Audit Type", out.getvalue())
        self.assertNotIn("International Resolution", out.getvalue())

    def test_unknown_workflow_type_raises(self):
        with self.assertRaises(CommandError):
            call_command("audit_workflow_access", "--workflow-type", "Nope")

    def test_unknown_user_raises(self):
        with self.assertRaises(CommandError):
            self._run("--user", "nobody")
