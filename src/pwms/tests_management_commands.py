"""Management commands that touch workflow definitions and memberships."""

import json
import shutil
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .models import (
    EventType,
    Group,
    GroupMembership,
    PublicStatus,
    Role,
    User,
    WorkflowType,
)


class WorkflowTypeDefinitionCommandTests(TestCase):
    """export_workflow_type → import_workflow_type round-trips current models."""

    @classmethod
    def setUpTestData(cls):
        cls.owner_group = Group.objects.create(
            name="Definition Owner", group_type=Group.Type.SECTION
        )
        cls.viewer_group = Group.objects.create(
            name="Definition Viewer", group_type=Group.Type.COMMITTEE
        )
        cls.creator_role = Role.objects.create(name="Definition Creator")
        cls.actor_role = Role.objects.create(name="Definition Actor")
        cls.evidence = EventType.objects.create(name="Test evidence")
        cls.new_status = PublicStatus.objects.get(name="New")
        cls.in_progress_status = PublicStatus.objects.get(name="In Progress")

        cls.parent_type = WorkflowType.objects.create(
            name="Definition Parent",
            group=cls.owner_group,
        )
        cls.workflow_type = WorkflowType.objects.create(
            name="Definition Type",
            description="A type for the round-trip test",
            group=cls.owner_group,
            parent_type=cls.parent_type,
            owner_can_edit=False,
            owner_can_delete=True,
            oversight_channel=WorkflowType.OVERSIGHT_BOTH,
            notify_next_actors=False,
            notify_named_people=False,
        )
        cls.workflow_type.viewer_groups.add(cls.viewer_group)
        cls.workflow_type.create_roles.add(cls.creator_role)

        cls.start = cls.workflow_type.states.create(
            name="Start",
            public_status=cls.new_status,
            is_initial=True,
            order=0,
        )
        cls.end = cls.workflow_type.states.create(
            name="End",
            public_status=cls.in_progress_status,
            is_terminal=True,
            order=1,
        )
        cls.transition = cls.workflow_type.transitions.create(
            name="Finish",
            from_state=cls.start,
            to_state=cls.end,
            requires_comment=False,
            order=0,
        )
        cls.transition.allowed_roles.add(cls.actor_role)
        cls.transition.required_event_types.add(cls.evidence)

    def test_export_import_round_trip_preserves_current_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "definition.json"
            call_command(
                "export_workflow_type",
                "--workflow-type",
                self.workflow_type.name,
                "--output",
                str(output),
                stdout=StringIO(),
            )

            payload = json.loads(output.read_text())
            self.assertEqual(payload["format_version"], 2)
            self.assertEqual(
                payload["workflow_type"]["viewer_groups_slugs"],
                [self.viewer_group.slug],
            )
            self.assertFalse(payload["workflow_type"]["owner_can_edit"])
            self.assertEqual(
                payload["states"][0]["public_status_slug"], self.new_status.slug
            )
            self.assertEqual(
                payload["transitions"][0]["required_event_type_slugs"],
                [self.evidence.slug],
            )

            self.workflow_type.delete()
            call_command("import_workflow_type", str(output), stdout=StringIO())

        imported = WorkflowType.objects.get(name="Definition Type")
        self.assertEqual(imported.group, self.owner_group)
        self.assertEqual(imported.parent_type, self.parent_type)
        self.assertEqual(
            set(imported.create_roles.values_list("slug", flat=True)),
            {self.creator_role.slug},
        )
        self.assertEqual(
            set(imported.viewer_groups.values_list("slug", flat=True)),
            {self.viewer_group.slug},
        )
        self.assertFalse(imported.owner_can_edit)
        self.assertTrue(imported.owner_can_delete)
        self.assertEqual(imported.oversight_channel, WorkflowType.OVERSIGHT_BOTH)
        self.assertFalse(imported.notify_next_actors)
        self.assertFalse(imported.notify_named_people)

        states = {state.name: state for state in imported.states.all()}
        self.assertEqual(states["Start"].public_status.slug, self.new_status.slug)
        self.assertTrue(states["Start"].is_initial)
        self.assertEqual(
            states["End"].public_status.slug, self.in_progress_status.slug
        )
        self.assertTrue(states["End"].is_terminal)

        transition = imported.transitions.get(name="Finish")
        self.assertEqual(transition.from_state, states["Start"])
        self.assertEqual(transition.to_state, states["End"])
        self.assertFalse(transition.requires_comment)
        self.assertEqual(
            set(transition.allowed_roles.values_list("slug", flat=True)),
            {self.actor_role.slug},
        )
        self.assertEqual(
            set(transition.required_event_types.values_list("slug", flat=True)),
            {self.evidence.slug},
        )

    def test_import_accepts_a_format_one_backup(self):
        payload = {
            "format_version": 1,
            "workflow_type": {
                "name": "Format One Type",
                "slug": "format-one-type",
                "description": "",
                "enabled": True,
                "group_slug": self.owner_group.slug,
                "create_roles_slugs": [self.creator_role.slug],
                "notify_roles_slugs": [],
            },
            "states": [
                {
                    "slug": "draft",
                    "name": "Draft",
                    "description": "",
                    "is_initial": True,
                    "is_terminal": False,
                    "allows_referrals": True,
                    "order": 0,
                    "color": "#5b6b7f",
                },
                {
                    "slug": "done",
                    "name": "Done",
                    "description": "",
                    "is_initial": False,
                    "is_terminal": True,
                    "allows_referrals": True,
                    "order": 1,
                    "color": "#14532d",
                },
            ],
            "transitions": [
                {
                    "slug": "finish",
                    "name": "Finish",
                    "from_state_slug": "draft",
                    "to_state_slug": "done",
                    "allowed_roles_slugs": [self.actor_role.slug],
                    "notify_roles_slugs": [],
                    "requires_comment": True,
                    "order": 0,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "format-one.json"
            path.write_text(json.dumps(payload))
            call_command("import_workflow_type", str(path), stdout=StringIO())

        imported = WorkflowType.objects.get(name="Format One Type")
        self.assertEqual(
            set(imported.states.values_list("public_status__slug", flat=True)),
            {self.new_status.slug},
        )
        transition = imported.transitions.get(name="Finish")
        self.assertEqual(
            set(transition.allowed_roles.values_list("slug", flat=True)),
            {self.actor_role.slug},
        )
        self.assertEqual(transition.required_event_types.count(), 0)


class ImportStatesCommandTests(TestCase):
    def setUp(self):
        self.group = Group.objects.create(
            name="States Owner", group_type=Group.Type.SECTION
        )
        self.workflow_type = WorkflowType.objects.create(
            name="States Type", group=self.group
        )
        self.in_progress = PublicStatus.objects.get(name="In Progress")
        self.new_status = PublicStatus.objects.get(name="New")

    def _write_csv(self, text):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "states.csv"
        path.write_text(text)
        self.addCleanup(shutil.rmtree, directory)
        return path

    def test_import_states_maps_the_public_status_column(self):
        path = self._write_csv(
            "name,description,is_initial,is_terminal,allows_referrals,order,color,public_status\n"
            "Draft,Being prepared,true,false,true,0,,New\n"
            "Active,In progress,false,false,true,1,,in-progress\n"
        )

        call_command(
            "import_states",
            str(path),
            "--workflow-type",
            self.workflow_type.name,
            stdout=StringIO(),
        )

        states = {state.name: state for state in self.workflow_type.states.all()}
        self.assertEqual(states["Draft"].public_status, self.new_status)
        self.assertTrue(states["Draft"].is_initial)
        self.assertEqual(states["Active"].public_status, self.in_progress)
        self.assertEqual(states["Active"].order, 1)

    def test_rows_without_a_public_status_use_the_default(self):
        path = self._write_csv(
            "name,is_initial,is_terminal,allows_referrals,order\n"
            "Solo,true,false,true,0\n"
        )

        call_command(
            "import_states",
            str(path),
            "--workflow-type",
            self.workflow_type.name,
            stdout=StringIO(),
        )

        state = self.workflow_type.states.get(name="Solo")
        self.assertEqual(state.public_status, self.new_status)

    def test_an_unknown_public_status_is_an_error(self):
        path = self._write_csv(
            "name,is_initial,is_terminal,allows_referrals,order,public_status\n"
            "Solo,true,false,true,0,does-not-exist\n"
        )

        with self.assertRaisesMessage(CommandError, "not found"):
            call_command(
                "import_states",
                str(path),
                "--workflow-type",
                self.workflow_type.name,
                stdout=StringIO(),
            )

    def test_dry_run_writes_nothing(self):
        path = self._write_csv(
            "name,is_initial,is_terminal,allows_referrals,order\n"
            "Solo,true,false,true,0\n"
        )

        call_command(
            "import_states",
            str(path),
            "--workflow-type",
            self.workflow_type.name,
            "--dry-run",
            stdout=StringIO(),
        )

        self.assertFalse(self.workflow_type.states.exists())


class ValidateMembershipsCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.group = Group.objects.create(
            name="Validation Group", group_type=Group.Type.SECTION
        )
        cls.role = Role.objects.create(name="Validation Role")
        cls.user = User.objects.create_user(username="validation-user", password="pw")

    def test_expired_active_membership_fails_validation(self):
        GroupMembership.objects.create(
            user=self.user,
            group=self.group,
            role=self.role,
            start_date=timezone.localdate() - timedelta(days=30),
            end_date=timezone.localdate() - timedelta(days=1),
            is_active=True,
        )

        with self.assertRaisesMessage(CommandError, "Membership validation failed"):
            call_command("validate_memberships", stdout=StringIO())

    def test_report_file_is_written_even_when_validation_fails(self):
        GroupMembership.objects.create(
            user=self.user,
            group=self.group,
            role=self.role,
            start_date=timezone.localdate() - timedelta(days=30),
            end_date=timezone.localdate() - timedelta(days=1),
            is_active=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            with self.assertRaises(CommandError):
                call_command(
                    "validate_memberships",
                    "--report-file",
                    str(path),
                    stdout=StringIO(),
                )
            report = json.loads(path.read_text())

        self.assertIn("errors", report)
        self.assertTrue(report["errors"])

    def test_fix_duplicates_keeps_the_newest_active_row(self):
        older = GroupMembership.objects.create(
            user=self.user,
            group=self.group,
            role=self.role,
            start_date=timezone.localdate() - timedelta(days=20),
            is_active=True,
        )
        newer = GroupMembership.objects.create(
            user=self.user,
            group=self.group,
            role=self.role,
            start_date=timezone.localdate() - timedelta(days=5),
            is_active=True,
        )

        call_command(
            "validate_memberships",
            "--fix-duplicates",
            stdout=StringIO(),
        )

        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertFalse(older.is_active)
        self.assertEqual(older.end_date, timezone.localdate())
        self.assertTrue(newer.is_active)

    def test_a_clean_set_passes(self):
        GroupMembership.objects.create(
            user=self.user,
            group=self.group,
            role=self.role,
            start_date=timezone.localdate() - timedelta(days=5),
            is_active=True,
        )

        call_command("validate_memberships", stdout=StringIO())


class FixMissingGroupAccessShimTests(TestCase):
    def test_the_deprecated_command_delegates(self):
        group = Group.objects.create(
            name="Shim Owner", group_type=Group.Type.SECTION
        )
        workflow_type = WorkflowType.objects.create(name="Shim Type", group=group)
        workflow_type.states.create(
            name="Start",
            public_status=PublicStatus.objects.get(name="New"),
            is_initial=True,
        )
        # No instance rows exist, so the delegated command's dry run is a no-op;
        # running it proves the shim resolves and forwards correctly.
        out = StringIO()
        call_command("fix_missing_group_access", "--dry-run", stdout=out)

        self.assertIn("deprecated", out.getvalue().lower())
