from auditlog.context import set_actor
from auditlog.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase

from ..models import (
    Bill,
    DelegationReport,
    InternationalAgreement,
    InternationalResolution,
    Transition,
    TransitionLog,
    WorkflowRelationship,
    WorkflowType,
)
from .base import make_state


class WorkflowAuditingTests(TestCase):
    """
    Verifies both audit mechanisms are wired for workflow instances:

    1. CRUD auditing via django-auditlog -> ``LogEntry`` rows on
       create / update / delete of a workflow instance.
    2. Domain state-machine auditing via ``TransitionLog`` on
       ``perform_transition()`` (which also surfaces as an auditlog UPDATE
       because ``current_state`` changed).
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="alice", email="alice@example.com", password="pw"
        )
        cls.wt = WorkflowType.objects.create(name="Test International Resolution")
        cls.draft = make_state(workflow_type=cls.wt, name="Drafting", is_initial=True)
        cls.gazetted = make_state(workflow_type=cls.wt, name="Gazetted")
        cls.submit = Transition.objects.create(
            workflow_type=cls.wt,
            name="Submit",
            from_state=cls.draft,
            to_state=cls.gazetted,
            requires_comment=True,
        )
        cls.ct = ContentType.objects.get_for_model(InternationalResolution)

    def _logs_for(self, object_id):
        return LogEntry.objects.filter(content_type=self.ct, object_id=object_id)

    def _make_resolution(self, number="IR-1", title="A test resolution"):
        return InternationalResolution.objects.create(
            workflow_type=self.wt,
            current_state=self.draft,
            resolution_number=number,
            title=title,
            owner=self.user,
        )

    def test_crud_create_update_delete_are_audited(self):
        # CREATE
        with set_actor(self.user):
            res = self._make_resolution()
        create = self._logs_for(res.pk).filter(action=LogEntry.Action.CREATE)
        self.assertEqual(create.count(), 1)
        self.assertEqual(create.first().actor, self.user)

        # UPDATE (plain field edit, no transition)
        with set_actor(self.user):
            res.title = "A test resolution (amended)"
            res.save(update_fields=["title"])
        update = self._logs_for(res.pk).filter(action=LogEntry.Action.UPDATE)
        self.assertEqual(update.count(), 1)
        self.assertIn("title", update.first().changes)

        # DELETE
        pk = res.pk
        with set_actor(self.user):
            res.delete()
        self.assertEqual(
            self._logs_for(pk).filter(action=LogEntry.Action.DELETE).count(), 1
        )

    def test_transition_writes_transitionlog_and_auditlog_update(self):
        res = self._make_resolution(number="IR-2", title="Second resolution")
        self.assertEqual(TransitionLog.objects.count(), 0)

        with set_actor(self.user):
            entry = res.perform_transition(
                self.submit, actor=self.user, comment="moving on"
            )

        # Domain log records the state change with actor + comment
        self.assertEqual(res.current_state, self.gazetted)
        log = TransitionLog.objects.get(pk=entry.pk)
        self.assertEqual(log.action, "STATE_TRANSITION")
        self.assertEqual(log.from_state, self.draft)
        self.assertEqual(log.to_state, self.gazetted)
        self.assertEqual(log.actor, self.user)
        self.assertEqual(log.notes, "moving on")
        self.assertEqual(list(res.audit_logs()), [log])

        # auditlog separately records the current_state field change as an UPDATE
        self.assertTrue(
            self._logs_for(res.pk).filter(action=LogEntry.Action.UPDATE).exists()
        )


class WorkflowHierarchyTests(TestCase):
    """
    Parent/child relationships between concrete workflow instances, e.g. a
    ``DelegationReport`` containing many ``InternationalResolution`` rows.
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="hierarchy", email="hierarchy@example.com", password="pw"
        )
        cls.report_type = WorkflowType.objects.create(name="Test Delegation Report")
        cls.resolution_type = WorkflowType.objects.create(
            name="Test International Resolution", parent_type=cls.report_type
        )
        cls.other_type = WorkflowType.objects.create(name="Press Statement")

        cls.report_state = make_state(workflow_type=cls.report_type, name="Draft")
        cls.resolution_state = make_state(
            workflow_type=cls.resolution_type, name="Draft"
        )
        cls.other_state = make_state(workflow_type=cls.other_type, name="Draft")

    def _report(self, title="Delegation to the UN"):
        return DelegationReport.objects.create(
            workflow_type=self.report_type,
            current_state=self.report_state,
            title=title,
            owner=self.user,
        )

    def _resolution(self, number="IR-1", title="A resolution"):
        return InternationalResolution.objects.create(
            workflow_type=self.resolution_type,
            current_state=self.resolution_state,
            resolution_number=number,
            title=title,
            owner=self.user,
        )

    def test_report_contains_many_resolutions(self):
        report = self._report()
        first = self._resolution(number="IR-1")
        second = self._resolution(number="IR-2", title="Another resolution")

        report.add_sub_workflow(first)
        report.add_sub_workflow(second)

        self.assertEqual(first.parent_workflow, report)
        self.assertEqual(second.parent_workflow, report)
        self.assertEqual(report.sub_workflows, [first, second])
        self.assertTrue(report.has_sub_workflows)
        self.assertTrue(report.is_root_workflow)
        self.assertFalse(first.is_root_workflow)
        self.assertEqual(report.hierarchy_level, 0)
        self.assertEqual(first.hierarchy_level, 1)
        self.assertEqual(first.get_workflow_hierarchy_path(), [report, first])
        self.assertEqual(report.get_all_descendants(), [first, second])

    def test_setting_parent_replaces_previous_parent(self):
        original = self._report(title="Original report")
        replacement = self._report(title="Replacement report")
        res = self._resolution()
        res.set_parent_workflow(original)

        res.set_parent_workflow(replacement, relationship_type="follows_up")

        self.assertEqual(res.parent_workflow, replacement)
        self.assertEqual(res.relationship_type, "follows_up")
        self.assertEqual(original.sub_workflows, [])
        self.assertEqual(WorkflowRelationship.objects.count(), 1)

    def test_cycle_is_rejected(self):
        report = self._report()
        res = self._resolution()
        report.add_sub_workflow(res)

        with self.assertRaises(ValidationError):
            res.add_sub_workflow(report)

    def test_self_parent_is_rejected(self):
        report = self._report()

        with self.assertRaises(ValidationError):
            report.set_parent_workflow(report)

    def test_type_hierarchy_helpers(self):
        self.assertEqual(self.resolution_type.get_ancestor_types(), [self.report_type])
        self.assertEqual(
            self.report_type.get_descendant_types(), [self.resolution_type]
        )
        self.assertIn(self.resolution_type, self.report_type.allowed_child_types())
        self.assertTrue(self.report_type.is_root_type)
        self.assertFalse(self.resolution_type.is_root_type)

    def test_type_restriction_rejects_undeclared_child(self):
        report = self._report()
        other = InternationalResolution.objects.create(
            workflow_type=self.other_type,
            current_state=self.other_state,
            resolution_number="IR-OTHER",
            title="Not an international resolution",
            owner=self.user,
        )

        with self.assertRaises(ValidationError):
            report.add_sub_workflow(other)


class SeededWorkflowDefinitionTests(TestCase):
    """The BRS workflow definitions seeded by migration 0005."""

    def test_delegation_report_lifecycle_matches_brs(self):
        wt = WorkflowType.objects.get(name="Delegation Report")
        states = list(wt.states.order_by("order").values_list("name", flat=True))
        self.assertEqual(
            states,
            [
                "Awaiting PGIR approval",
                "Submitted for tabling",
                "Tabled and referred to Committee",
                "Closed – House approved",
            ],
        )
        self.assertEqual(wt.get_initial_state().name, "Awaiting PGIR approval")
        closed = wt.states.get(name="Closed – House approved")
        self.assertTrue(closed.is_terminal)
        self.assertEqual(wt.transitions.count(), 3)

    def test_resolution_lifecycle_and_type_hierarchy(self):
        wt = WorkflowType.objects.get(name="International Resolution")
        states = list(wt.states.order_by("order").values_list("name", flat=True))
        self.assertEqual(
            states, ["Captured", "Assigned", "In Progress", "Implemented", "Closed"]
        )
        self.assertEqual(wt.get_initial_state().name, "Captured")
        self.assertTrue(wt.states.get(name="Closed").is_terminal)
        self.assertEqual(wt.transitions.count(), 4)
        # Type-level hierarchy: resolutions nest under delegation reports.
        self.assertEqual(wt.parent_type.name, "Delegation Report")

    def test_international_agreement_lifecycle_matches_brs(self):
        wt = WorkflowType.objects.get(name="International Agreement")
        states = list(wt.states.order_by("order").values_list("name", flat=True))
        self.assertEqual(
            states,
            [
                "Submitted for tabling",
                "Agreement Tabled – referred to Committee",
                "Committee considering and processing",
                "Committee submitted report for tabling",
                "House adopted – referred to Department",
                "Closed – House approved",
            ],
        )
        # BR02: a newly created agreement starts "Agreement Tabled – referred
        # to Committee".
        self.assertEqual(
            wt.get_initial_state().name,
            "Agreement Tabled – referred to Committee",
        )
        self.assertTrue(wt.states.get(name="Closed – House approved").is_terminal)
        self.assertEqual(wt.transitions.count(), 5)

    def test_international_agreement_transitions_chain_end_to_end(self):
        wt = WorkflowType.objects.get(name="International Agreement")
        user = get_user_model().objects.create_user(
            username="agreement-seed", password="pw"
        )
        agreement = InternationalAgreement.objects.create(
            workflow_type=wt,
            current_state=wt.get_initial_state(),
            title="Seeded agreement lifecycle",
            owner=user,
        )

        for step in [
            "Committee considering and processing",
            "Committee submitted report for tabling",
            "House adopted – referred to Department",
            "Closed – House approved",
        ]:
            transitions = list(agreement.get_available_transitions())
            self.assertEqual(len(transitions), 1)
            agreement.perform_transition(transitions[0], comment=f"to {step}")

        self.assertEqual(agreement.current_state.name, "Closed – House approved")
        self.assertTrue(agreement.current_state.is_terminal)

    def test_bill_lifecycle_and_public_statuses(self):
        wt = WorkflowType.objects.get(name="Bill")
        states = wt.states.order_by("order")
        self.assertEqual(
            list(states.values_list("name", flat=True)),
            [
                "Introduced",
                "Referred to Committee",
                "Public Participation",
                "Committee Deliberation",
                "Committee Report",
                "House Debate and Voting",
                "NCOP Consideration",
                "Mediation / Reconsideration",
                "Awaiting Presidential Assent",
                "Referred Back / Constitutional Review",
                "Signed into Law",
                "Withdrawn",
            ],
        )
        self.assertEqual(wt.get_initial_state().name, "Introduced")
        self.assertTrue(wt.states.get(name="Signed into Law").is_terminal)
        self.assertTrue(wt.states.get(name="Withdrawn").is_terminal)
        self.assertEqual(wt.transitions.count(), 20)

        # Every BRS §12 public status is reachable from the internal machine.
        public_statuses = set(states.values_list("public_status__name", flat=True))
        self.assertTrue(
            {
                "Introduced",
                "Under Parliamentary Consideration",
                "National Council of Provinces",
                "Mediation / Reconsideration",
                "Awaiting Presidential Assent",
                "Signed into Law",
                "Referred Back / Constitutional Review",
            }.issubset(public_statuses)
        )
        # The detailed committee stages stay distinct internally while all
        # publishing as the same simplified status.
        internal_stages = states.filter(
            public_status__name="Under Parliamentary Consideration"
        )
        self.assertEqual(internal_stages.count(), 5)

    def test_bill_mainline_reaches_signed_into_law(self):
        wt = WorkflowType.objects.get(name="Bill")
        user = get_user_model().objects.create_user(
            username="bill-mainline", password="pw"
        )
        bill = Bill.objects.create(
            workflow_type=wt,
            current_state=wt.get_initial_state(),
            bill_number="B 1—2026",
            title="Mainline walk",
            owner=user,
        )

        mainline = [
            "Referred to Committee",
            "Public Participation",
            "Committee Deliberation",
            "Committee Report",
            "House Debate and Voting",
            "NCOP Consideration",
            "Awaiting Presidential Assent",
            "Signed into Law",
        ]
        for target in mainline:
            transition = bill.get_available_transitions().get(to_state__name=target)
            bill.perform_transition(transition, comment=f"to {target}")

        self.assertTrue(bill.current_state.is_terminal)
        self.assertEqual(bill.public_status, "Signed into Law")

    def test_bill_withdrawal_requires_a_reason(self):
        wt = WorkflowType.objects.get(name="Bill")
        user = get_user_model().objects.create_user(
            username="bill-withdraw", password="pw"
        )
        bill = Bill.objects.create(
            workflow_type=wt,
            current_state=wt.get_initial_state(),
            bill_number="B 2—2026",
            title="Withdrawn bill",
            owner=user,
        )

        withdrawal = bill.get_available_transitions().get(to_state__name="Withdrawn")
        self.assertTrue(withdrawal.requires_comment)
        with self.assertRaises(ValidationError):
            bill.perform_transition(withdrawal)

        bill.perform_transition(withdrawal, comment="Sponsor withdrew the bill")
        self.assertEqual(bill.current_state.name, "Withdrawn")
        self.assertEqual(bill.public_status, "Withdrawn")

    def test_seeded_transitions_chain_end_to_end(self):
        wt = WorkflowType.objects.get(name="International Resolution")
        user = get_user_model().objects.create_user(username="seeded", password="pw")
        resolution = InternationalResolution.objects.create(
            workflow_type=wt,
            current_state=wt.get_initial_state(),
            resolution_number="IR-SEED-1",
            title="Seeded lifecycle",
            owner=user,
        )

        for step in ["Assigned", "In Progress", "Implemented", "Closed"]:
            transitions = list(resolution.get_available_transitions())
            self.assertEqual(len(transitions), 1)
            resolution.perform_transition(transitions[0], comment=f"to {step}")

        self.assertEqual(resolution.current_state.name, "Closed")
        self.assertTrue(resolution.current_state.is_terminal)
