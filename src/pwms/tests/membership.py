import json
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from ..management.commands.compare_org_hierarchy import (
    Command as CompareOrgHierarchyCommand,
)
from ..membership.org_hierarchy import (
    compare_with_stored,
    infer_unit_parents,
    person_from_row,
    unit_parent_map,
)
from ..models import (
    Group,
)

# The IRP subtree as the ERP's "Organization Hierarchy" screen reports it: the
# manager chain plus each person's organisational unit (PARENT_ORG_NAME). Lets
# the supervisor-derived tree be checked against real data without Oracle.
IRP_DIVISION = "IRP: Office Of The IR And P Division Manager"
IRP_MR_SECTION = "IRP: Multilateral Relations (MR) Section"
IRP_PA_SECTION = "IRP: IR Policy Analysis (PA)"
IRP_OM_SECTION = "IRP: IRP Operations Management (OM) Section"
IRP_PCS_SECTION = "IRP: Protocol And Ceremonial Services (P And CS) Section"

#: (employee_id, supervisor_id, cost centre, organisational unit)
IRP_ORACLE_ROWS = (
    ("E1", "", IRP_DIVISION, IRP_DIVISION),  # Division Manager, no supervisor
    ("E2", "E1", IRP_DIVISION, IRP_DIVISION),
    ("E3", "E1", IRP_DIVISION, IRP_DIVISION),
    ("E4", "E1", IRP_DIVISION, IRP_DIVISION),
    ("E5", "E1", IRP_DIVISION, IRP_DIVISION),  # sbrown
    ("E6", "E1", IRP_MR_SECTION, IRP_MR_SECTION),  # Section Manager
    ("E7", "E1", IRP_MR_SECTION, IRP_MR_SECTION),
    ("E8", "E6", IRP_MR_SECTION, IRP_MR_SECTION),
    ("E9", "E6", IRP_MR_SECTION, IRP_MR_SECTION),
    ("E10", "E6", IRP_MR_SECTION, IRP_MR_SECTION),
    ("E11", "E6", IRP_MR_SECTION, IRP_MR_SECTION),
    ("E12", "E6", IRP_MR_SECTION, IRP_MR_SECTION),
    ("E13", "E1", IRP_PA_SECTION, IRP_PA_SECTION),
    ("E14", "E1", IRP_PA_SECTION, IRP_PA_SECTION),
    ("E15", "E1", IRP_PA_SECTION, IRP_PA_SECTION),
    ("E16", "E6", IRP_PA_SECTION, IRP_PA_SECTION),  # functional line to MR
    ("E17", "E1", IRP_OM_SECTION, IRP_OM_SECTION),
    ("E18", "E6", IRP_OM_SECTION, IRP_OM_SECTION),  # functional line to MR
    ("E19", "E6", IRP_PCS_SECTION, IRP_PCS_SECTION),  # lone member
)


def _irp_people():
    return [person_from_row(row) for row in IRP_ORACLE_ROWS]


class OrgHierarchyInferenceTests(SimpleTestCase):
    """The unit tree the supervisor chain implies matches the ERP's IRP chart."""

    def _infer(self):
        return infer_unit_parents(_irp_people(), lambda name: name)

    def test_sections_nest_under_the_unit_their_manager_reports_into(self):
        inferences = self._infer()
        # Each section manager reports to the division manager.
        self.assertEqual(inferences[IRP_MR_SECTION].parent, IRP_DIVISION)
        self.assertEqual(inferences[IRP_PA_SECTION].parent, IRP_DIVISION)
        self.assertEqual(inferences[IRP_OM_SECTION].parent, IRP_DIVISION)
        # The division manager reports outside this data: a root, not a section.
        self.assertIsNone(inferences[IRP_DIVISION].parent)

    def test_span_of_control_breaks_a_tie_between_two_supervisors(self):
        # OM has one report into the division and one into the section; the
        # division manager supervises more people, so the division wins.
        om = self._infer()[IRP_OM_SECTION]
        self.assertEqual(om.votes, {IRP_DIVISION: 1, IRP_MR_SECTION: 1})
        self.assertEqual(om.parent, IRP_DIVISION)
        self.assertFalse(om.tied)

    def test_a_lone_functional_reporting_line_is_flagged_low_confidence(self):
        pcs = self._infer()[IRP_PCS_SECTION]
        self.assertEqual(pcs.parent, IRP_MR_SECTION)
        self.assertTrue(pcs.low_confidence)

    def test_normalisation_is_applied_to_every_unit_name(self):
        seen = []
        infer_unit_parents(_irp_people(), lambda name: seen.append(name) or name)
        self.assertIn(IRP_MR_SECTION, seen)
        self.assertIn(IRP_DIVISION, seen)

    def test_compare_reports_mismatch_missing_and_match(self):
        inferences = self._infer()
        stored = {IRP_DIVISION: "Administration", IRP_MR_SECTION: "Administration"}
        rows = {row.unit: row for row in compare_with_stored(inferences, stored)}
        # Stored flat under Administration; Oracle implies the division.
        self.assertEqual(rows[IRP_MR_SECTION].status, "differs")
        self.assertIsNone(rows[IRP_MR_SECTION].stored_parent)
        self.assertEqual(rows[IRP_MR_SECTION].inferred_parent, IRP_DIVISION)
        # A top-level unit stored under Administration is a match, not a gap.
        self.assertEqual(rows[IRP_DIVISION].status, "match")
        # No PWMS group for the PA section yet.
        self.assertEqual(rows[IRP_PA_SECTION].status, "missing")

    def test_compare_prefix_limits_the_scope(self):
        rows = compare_with_stored(self._infer(), {}, prefix="IRP: Multilateral")
        self.assertEqual({row.unit for row in rows}, {IRP_MR_SECTION})

    def test_compare_matches_names_that_differ_only_by_acronym_case(self):
        # The live tree holds "...The Ir And P..." (stored before "IR" joined the
        # acronym table); the sync normalises to "...The IR And P...". Same unit.
        stored = {"IRP: Office Of The Ir And P Division Manager": "Administration"}
        rows = {row.unit: row for row in compare_with_stored(self._infer(), stored)}
        self.assertEqual(rows[IRP_DIVISION].status, "match")

    def test_unit_parent_map_drops_single_report_edges(self):
        # Acting on a one-report edge is what nests a division under a foreign
        # unit on real data, so the map keeps only corroborated edges by default.
        inferences = self._infer()
        self.assertEqual(
            unit_parent_map(inferences, min_votes=2),
            {IRP_MR_SECTION: IRP_DIVISION, IRP_PA_SECTION: IRP_DIVISION},
        )
        self.assertEqual(
            set(unit_parent_map(inferences, min_votes=1)),
            {IRP_MR_SECTION, IRP_PA_SECTION, IRP_OM_SECTION, IRP_PCS_SECTION},
        )


class CompareOrgHierarchyCommandTests(TestCase):
    """``compare_org_hierarchy`` diffs stored parents against Oracle's implied ones."""

    @classmethod
    def setUpTestData(cls):
        cls.administration = Group.objects.create(
            name="Administration", group_type="administration"
        )
        cls.division = Group.objects.create(
            name=IRP_DIVISION, group_type="division", parent=cls.administration
        )
        # The MR section is stored flat under Administration: the bug.
        cls.mr = Group.objects.create(
            name=IRP_MR_SECTION, group_type="division", parent=cls.administration
        )

    def _run(self, *args):
        out = StringIO()
        with (
            mock.patch.object(CompareOrgHierarchyCommand, "validate_environment"),
            mock.patch.object(CompareOrgHierarchyCommand, "connect_to_oracle"),
            mock.patch.object(
                CompareOrgHierarchyCommand, "fetch_people", return_value=_irp_people()
            ),
        ):
            call_command("compare_org_hierarchy", *args, stdout=out)
        return out

    def test_reports_the_section_that_should_sit_under_the_division(self):
        payload = json.loads(self._run("--json").getvalue())
        rows = {row["unit"]: row for row in payload["rows"]}
        self.assertEqual(rows[IRP_MR_SECTION]["status"], "differs")
        self.assertEqual(rows[IRP_MR_SECTION]["inferred_parent"], IRP_DIVISION)
        self.assertIsNone(rows[IRP_MR_SECTION]["stored_parent"])
        self.assertEqual(rows[IRP_PA_SECTION]["status"], "missing")
        self.assertEqual(payload["summary"]["differs"], 1)

    def test_prefix_limits_the_scope(self):
        payload = json.loads(
            self._run("--prefix", "IRP: Multilateral", "--json").getvalue()
        )
        self.assertEqual({row["unit"] for row in payload["rows"]}, {IRP_MR_SECTION})

    def test_text_output_names_both_trees(self):
        text = self._run("--prefix", "IRP: Multilateral").getvalue()
        self.assertIn(IRP_MR_SECTION, text)
        self.assertIn(IRP_DIVISION, text)

    def test_strict_exits_non_zero_on_a_difference(self):
        with self.assertRaises(CommandError):
            self._run("--strict")
