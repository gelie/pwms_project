import shutil
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from ..management.commands.generate_group_tree import (
    Command as GenerateGroupTreeCommand,
)
from ..models import (
    DelegationReport,
    Group,
    GroupMembership,
    InternationalResolution,
    Role,
    WorkflowReferral,
    WorkflowType,
)


class ShowWorkflowHierarchyCommandTests(TestCase):
    """``show_workflow_hierarchy`` over the concrete workflow registers."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="hierarchy-owner", password="pw")
        report_type = WorkflowType.objects.get(name="Delegation Report")
        resolution_type = WorkflowType.objects.get(name="International Resolution")

        cls.report = DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Shadow budget report",
            owner=cls.owner,
        )
        cls.resolution = InternationalResolution.objects.create(
            workflow_type=resolution_type,
            current_state=resolution_type.get_initial_state(),
            title="Referred resolution",
            resolution_number="IR-HIER-1",
            owner=cls.owner,
        )
        cls.report.add_sub_workflow(cls.resolution)

        # A root with neither parent nor children: the orphan case.
        cls.orphan = DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Lonely report",
            owner=cls.owner,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("show_workflow_hierarchy", *args, stdout=out)
        return out.getvalue()

    def test_specific_workflow_prints_its_path(self):
        output = self._run("--workflow-id", str(self.resolution.public_id))

        self.assertIn("Hierarchy path:", output)
        # The parent and the target both appear, parent first.
        self.assertIn("Shadow budget report", output)
        self.assertIn("Referred resolution", output)
        self.assertIn("Is root: False", output)
        self.assertIn("Total descendants: 0", output)

    def test_default_listing_shows_roots_with_their_children(self):
        output = self._run()

        self.assertIn("Root Workflows", output)
        self.assertIn("Shadow budget report", output)
        # The child is not a root, but it is drawn under its parent.
        self.assertIn("Referred resolution", output)
        self.assertIn("Lonely report", output)

    def test_orphans_lists_workflows_without_links(self):
        output = self._run("--show-orphans")

        self.assertIn("Lonely report", output)
        self.assertNotIn("Shadow budget report", output)
        self.assertNotIn("Referred resolution", output)

    def test_show_all_reports_descendant_counts(self):
        output = self._run("--show-all")

        self.assertIn("Shadow budget report", output)
        self.assertIn("Total descendants: 1", output)

    def test_workflow_type_filter_excludes_other_types(self):
        output = self._run("--workflow-type", "Bill")

        self.assertNotIn("Shadow budget report", output)
        self.assertIn("No root workflows found.", output)

    def test_unknown_workflow_id_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command(
                "show_workflow_hierarchy",
                "--workflow-id",
                "00000000-0000-0000-0000-000000000000",
                stdout=StringIO(),
            )


class CheckReferralDeadlinesCommandTests(TestCase):
    """``check_referral_deadlines`` against the typed WorkflowReferral rows."""

    def run_queued_emails(self):
        """Deliver the alerts the command queued - what ``process_tasks`` does.

        Alert email is a background task now, so the command only queues it, and
        a ``TestCase`` neither commits nor has a worker. Running the queue
        synchronously here is what makes the mail observable.
        """
        from background_task.models import Task
        from background_task.tasks import tasks as bg_tasks
        from django.test import override_settings

        with override_settings(BACKGROUND_TASK_RUN_ASYNC=False):
            for task in list(
                Task.objects.filter(task_name="pwms.tasks.deliver_notification_email")
            ):
                bg_tasks.run_task(task)

    def run_command(self, *args):
        """Run the command and let its queued alerts actually go out."""
        call_command("check_referral_deadlines", *args, stdout=StringIO())
        self.run_queued_emails()

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(
            username="deadline-owner", email="owner@example.com", password="pw"
        )
        cls.member = User.objects.create_user(
            username="deadline-member",
            email="member@example.com",
            password="pw",
            is_staff_member=True,
        )
        cls.committee = Group.objects.create(
            name="Deadline Committee XZ", group_type="portfolio_committee"
        )
        cls.role = Role.objects.create(name="Deadline Committee Member")
        GroupMembership.objects.create(
            user=cls.member, group=cls.committee, role=cls.role
        )
        cls.report_type = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=cls.report_type,
            current_state=cls.report_type.get_initial_state(),
            title="Referred report",
            owner=cls.owner,
        )

    def _referral(self, *, due_in, notified_at=None, status="open"):
        return WorkflowReferral.objects.create(
            content_type=ContentType.objects.get_for_model(DelegationReport),
            object_id=self.report.pk,
            referred_to=self.committee,
            referred_by=self.owner,
            due_date=timezone.now() + due_in,
            deadline_notified_at=notified_at,
            status=status,
        )

    def test_reminder_is_emailed_and_recorded(self):
        referral = self._referral(due_in=timedelta(hours=2))

        self.run_command()

        # Only the referred committee's staff are emailed; the owner is told
        # in-app but is not a member of an affected group.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["member@example.com"])
        self.assertIn("Referred report", mail.outbox[0].subject)
        referral.refresh_from_db()
        self.assertIsNotNone(referral.deadline_notified_at)

    def test_reminder_is_not_repeated(self):
        self._referral(due_in=timedelta(hours=2))

        self.run_command()
        self.run_command()

        # One reminder run, one message per recipient; the second run is silent.
        self.assertEqual(len(mail.outbox), 1)

    def test_second_reminder_fires_inside_the_last_hour(self):
        self._referral(
            due_in=timedelta(minutes=30),
            notified_at=timezone.now() - timedelta(hours=2),
        )

        self.run_command()

        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].to == ["member@example.com"])
        self.assertIn("Referred report", mail.outbox[0].subject)

    def test_passed_deadline_is_marked_expired(self):
        referral = self._referral(due_in=timedelta(hours=-2))

        self.run_command()

        referral.refresh_from_db()
        self.assertEqual(referral.status, "expired")
        # mark_expired() raises the notice itself, so exactly one per recipient.
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Expired", mail.outbox[0].subject)

    def test_answered_referrals_are_left_alone(self):
        referral = self._referral(due_in=timedelta(hours=-2), status="responded")

        self.run_command()

        referral.refresh_from_db()
        self.assertEqual(referral.status, "responded")
        self.assertEqual(mail.outbox, [])

    def test_dry_run_changes_nothing(self):
        referral = self._referral(due_in=timedelta(hours=2))
        expired = self._referral(due_in=timedelta(hours=-2))

        self.run_command("--dry-run")

        self.assertEqual(mail.outbox, [])
        referral.refresh_from_db()
        expired.refresh_from_db()
        self.assertIsNone(referral.deadline_notified_at)
        self.assertEqual(expired.status, "open")


class BuildUatWorkbookCommandTests(TestCase):
    """The UAT results workbook is generated from the printable acceptance form.

    The form's HTML is the source of truth for the scenarios, so these tests assert
    the workbook *mirrors* it rather than pinning a scenario count: adding a
    scenario to the form must not break the build.
    """

    @staticmethod
    def _sheet_rows(sheet, *, first_row):
        """The non-empty rows of a five-column sheet, from ``first_row`` down."""
        rows = []
        for row in range(first_row, sheet.max_row + 1):
            values = [
                sheet.cell(row=row, column=column).value for column in range(1, 6)
            ]
            if values[0]:
                rows.append(values)
        return rows

    def _build(self, **options):
        """Run the command, writing the workbook into a temporary directory."""
        from openpyxl import load_workbook

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        output = Path(tmp.name) / "UAT Form.xlsx"
        out = StringIO()
        call_command("build_uat_workbook", output=str(output), stdout=out, **options)
        return load_workbook(output), out.getvalue()

    def test_it_builds_the_sheets_and_opens_on_results(self):
        workbook, _ = self._build()

        self.assertEqual(
            workbook.sheetnames,
            [
                "Instructions",
                "Scenarios",
                "Results",
                "Defect log",
                "Summary",
                "Expected behaviour",
                "Sign-off",
            ],
        )
        # The section works in the matrix, so that is the sheet presented.
        self.assertEqual(workbook.active.title, "Results")

    def test_the_matrix_carries_every_scenario_the_form_defines(self):
        workbook, _ = self._build()
        scenarios = self._sheet_rows(workbook["Scenarios"], first_row=4)
        results = self._sheet_rows(workbook["Results"], first_row=10)

        self.assertTrue(scenarios, "the form should define at least one scenario")
        self.assertEqual(len(results), len(scenarios))
        # Same scenarios, in the same order, with the same critical marks.
        self.assertEqual(
            [(row[0], row[2]) for row in results],
            [(row[0], row[2]) for row in scenarios],
        )
        # The reference sheet carries the steps and the acceptance criteria.
        for row in scenarios:
            self.assertTrue(row[3] and row[4])

    def test_the_tester_cells_offer_only_the_four_results(self):
        workbook, _ = self._build()
        sheet = workbook["Results"]
        validation = next(
            candidate
            for candidate in sheet.data_validations.dataValidation
            if candidate.formula1 == '"P,F,B,N"'
        )

        self.assertTrue(validation.allow_blank)
        # It covers the tester columns of every scenario row, and the working
        # columns stay put while a tester scrolls to their own column.
        self.assertTrue(str(validation.sqref).startswith("H10:"))
        self.assertEqual(sheet.freeze_panes, "H10")

    def test_the_verdict_counts_the_rows_that_actually_hold_the_conditions(self):
        """The verdict reads its conditions by reference, not by layout arithmetic.

        Regression: it used to be built by subtracting from its own row number,
        which pointed at blank and heading rows rather than the counts.
        """
        import re

        workbook, _ = self._build()
        sheet = workbook["Summary"]
        verdict_row = next(
            row
            for row in range(1, sheet.max_row + 1)
            if isinstance(sheet.cell(row=row, column=3).value, str)
            and "ACCEPTANCE CRITERIA MET" in sheet.cell(row=row, column=3).value
        )
        verdict = sheet.cell(row=verdict_row, column=3).value
        referenced = re.findall(r"C(\d+)", verdict)

        # The three critical counts, plus the open S1 and S2 counts.
        self.assertGreaterEqual(len(referenced), 5)
        conditions = [sheet.cell(row=int(row), column=3).value for row in referenced]
        for condition in conditions:
            self.assertIn("COUNTIF", condition)
        self.assertGreaterEqual(
            sum("'Defect log'" in condition for condition in conditions), 2
        )

    def test_it_reads_the_result_key_and_the_severities_from_the_form(self):
        workbook, _ = self._build()
        sheet = workbook["Instructions"]
        text = "\n".join(
            str(sheet.cell(row=row, column=column).value or "")
            for row in range(1, sheet.max_row + 1)
            for column in (2, 3)
        )

        self.assertIn("Pass: as expected", text)
        self.assertIn("S1 — Blocking", text)

    def test_a_missing_form_is_reported(self):
        with self.assertRaises(CommandError):
            call_command(
                "build_uat_workbook",
                form="does-not-exist.html",
                stdout=StringIO(),
            )


class GenerateGroupTreeCommandTests(TestCase):
    """The group-hierarchy diagram command (``generate_group_tree``).

    Tree structure is asserted against the pruned in-memory forest, which needs
    no Graphviz; the render itself runs under ``skipUnless`` so the suite still
    passes on a host without the ``dot`` binary.
    """

    @classmethod
    def setUpTestData(cls):
        Group.objects.all().delete()
        cls.gov = Group.objects.create(name="Tree Gov", group_type="legislature")
        cls.parl = Group.objects.create(
            name="Tree Parliament", group_type="parliament", parent=cls.gov
        )
        cls.na = Group.objects.create(
            name="Tree NA", group_type="house", parent=cls.parl
        )
        cls.pc = Group.objects.create(
            name="Tree PC", group_type="portfolio_committee", parent=cls.na
        )
        cls.admin = Group.objects.create(
            name="Tree Admin", group_type="administration", parent=cls.gov
        )
        cls.div = Group.objects.create(
            name="Tree Div", short_name="DIV", group_type="division", parent=cls.admin
        )
        cls.sec = Group.objects.create(
            name="Tree Sec", group_type="section", parent=cls.div
        )
        cls.retired = Group.objects.create(
            name="Tree Retired", group_type="section", parent=cls.admin, is_active=False
        )

    def _forest(self, group=None, group_types=None, active_only=False, max_depth=None):
        return GenerateGroupTreeCommand()._build_forest(
            group, group_types, active_only, max_depth
        )

    def _ids(self, forest):
        return [node.id for node in GenerateGroupTreeCommand()._all_nodes(forest)]

    def _node(self, forest, group_id):
        """The freshly-loaded instance of ``group_id`` inside ``forest``.

        The class-level ``cls`` attributes are separate Python objects; the
        pruned-tree links (``_children`` / ``_tree_parent``) live on the
        instances ``_build_forest`` loaded, so assertions must read those.
        """
        for node in GenerateGroupTreeCommand()._all_nodes(forest):
            if node.id == group_id:
                return node
        raise AssertionError(f"group {group_id} not present in the forest")

    def test_no_group_draws_every_root_and_descendant(self):
        forest = self._forest()
        self.assertEqual([n.id for n in forest], [self.gov.id])
        self.assertEqual(
            set(self._ids(forest)),
            {
                self.gov.id,
                self.parl.id,
                self.na.id,
                self.pc.id,
                self.admin.id,
                self.div.id,
                self.sec.id,
                self.retired.id,
            },
        )

    def test_group_scopes_to_that_subtree(self):
        forest = self._forest(group="Tree NA")
        self.assertEqual([n.id for n in forest], [self.na.id])
        self.assertEqual(set(self._ids(forest)), {self.na.id, self.pc.id})

    def test_group_matches_slug_as_well_as_name(self):
        # The reference is resolved by slug, exactly as --group accepts it.
        self.assertEqual(self.na.slug, "tree-na")
        forest = self._forest(group=self.na.slug)
        self.assertEqual([n.id for n in forest], [self.na.id])

    def test_group_type_filter_reparents_around_excluded_nodes(self):
        forest = self._forest(group="Tree Gov", group_types=["section", "division"])
        # The division survives although its ancestors do not, so it becomes a
        # root; its section stays attached. The retired section's parent is
        # excluded too, so it is promoted to a root rather than dropped.
        self.assertEqual({n.id for n in forest}, {self.div.id, self.retired.id})
        self.assertEqual(
            [n.id for n in self._node(forest, self.div.id)._children], [self.sec.id]
        )

    def test_active_only_drops_inactive_groups(self):
        forest = self._forest(active_only=True)
        self.assertNotIn(self.retired.id, self._ids(forest))

    def test_max_depth_prunes_below_the_root(self):
        forest = self._forest(group="Tree Gov", max_depth=1)
        self.assertEqual(
            set(self._ids(forest)), {self.gov.id, self.parl.id, self.admin.id}
        )
        self.assertEqual(self._node(forest, self.parl.id)._children, [])
        self.assertEqual(self._node(forest, self.admin.id)._children, [])

    def test_unknown_group_type_raises(self):
        with self.assertRaises(CommandError):
            call_command("generate_group_tree", "--group-type", "nonsense")

    def test_unknown_group_raises(self):
        with self.assertRaises(CommandError):
            call_command("generate_group_tree", "--group", "No Such Group")

    def test_ambiguous_group_reference_raises(self):
        Group.objects.create(name="Tree Twin", group_type="division", parent=self.admin)
        Group.objects.create(name="Tree Twin", group_type="division", parent=self.gov)
        with self.assertRaises(CommandError):
            call_command("generate_group_tree", "--group", "Tree Twin")

    @skipUnless(shutil.which("dot"), "Graphviz 'dot' binary not installed")
    def test_render_writes_svg_and_mermaid(self):
        Group.objects.create(name="Tree Outsider", group_type="party")
        member = get_user_model().objects.create_user(
            username="tree-member", password="pw"
        )
        GroupMembership.objects.create(
            user=member,
            group=self.div,
            role=Role.objects.create(name="Tree Member"),
            is_active=True,
        )

        tmp = tempfile.mkdtemp()
        call_command(
            "generate_group_tree",
            "--group",
            "Tree Gov",
            "--show-members",
            "--output-dir",
            tmp,
            stdout=StringIO(),
        )

        svg_path = Path(tmp) / "tree_gov.svg"
        md_path = Path(tmp) / "tree_gov.md"
        self.assertTrue(svg_path.exists())
        self.assertTrue(md_path.exists())

        svg = svg_path.read_text()
        self.assertIn("Tree Div", svg)  # in scope
        self.assertIn("DIV", svg)  # short name rendered beneath the node name
        self.assertIn("1 members", svg)  # member count from --show-members
        self.assertNotIn("Tree Outsider", svg)  # a sibling root is out of scope
        self.assertIn("Legend", svg)

        mermaid = md_path.read_text()
        self.assertIn("flowchart TD", mermaid)
        self.assertIn("classDef", mermaid)
        self.assertIn("--> ", mermaid)
