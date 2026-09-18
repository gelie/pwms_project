import tempfile
from datetime import date, timedelta
from importlib import import_module
from io import StringIO
from pathlib import Path
from unittest import mock

from auditlog.context import set_actor
from auditlog.models import LogEntry
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_flatpickr.widgets import DatePickerInput, DateTimePickerInput

from .models import (
    Bill,
    BillVersion,
    City,
    Country,
    DelegationParticipant,
    DelegationReport,
    EventType,
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Role,
    State,
    Transition,
    TransitionCondition,
    TransitionLog,
    WorkflowEvent,
    WorkflowGroupAccess,
    WorkflowReferral,
    WorkflowRelationship,
    WorkflowRolePermission,
    WorkflowType,
)
from .models.workflows import OVERDUE_IDENTIFIER
from .services.permissions import (
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
from .services.progress import machine_for


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
        cls.draft = State.objects.create(
            workflow_type=cls.wt, name="Drafting", is_initial=True
        )
        cls.gazetted = State.objects.create(workflow_type=cls.wt, name="Gazetted")
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

        cls.report_state = State.objects.create(
            workflow_type=cls.report_type, name="Draft"
        )
        cls.resolution_state = State.objects.create(
            workflow_type=cls.resolution_type, name="Draft"
        )
        cls.other_state = State.objects.create(
            workflow_type=cls.other_type, name="Draft"
        )

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
        public_statuses = set(states.values_list("public_name", flat=True))
        self.assertTrue(
            {
                "introduced",
                "under_consideration",
                "ncop",
                "mediation",
                "awaiting_assent",
                "signed_into_law",
                "constitutional_review",
            }.issubset(public_statuses)
        )
        # The detailed committee stages stay distinct internally while all
        # publishing as the same simplified status.
        internal_stages = states.filter(public_name="under_consideration")
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


class DelegationReportTests(TestCase):
    """BRS attributes and validation for delegation reports (BR02/BR03/BR12)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="irpd", email="irpd@example.com", password="pw"
        )
        cls.wt = WorkflowType.objects.get(name="Delegation Report")
        cls.initial = cls.wt.get_initial_state()
        cls.switzerland = Country.objects.create(
            code="CH", iso3="CHE", name="Switzerland", continent="EU"
        )
        cls.geneva = City.objects.create(
            country=cls.switzerland,
            name="Geneva",
            ascii_name="Geneva",
            latitude="46.204391",
            longitude="6.143158",
            population=201818,
        )

    def _report(self, **overrides):
        data = {
            "workflow_type": self.wt,
            "current_state": self.initial,
            "title": "Delegation to the IPU",
            "owner": self.user,
            "assigned_to": self.user,
            "engagement_name": "IPU Assembly",
            "engagement_start_date": date(2026, 3, 1),
            "engagement_end_date": date(2026, 3, 5),
            "location_city": self.geneva,
            "location_country": self.switzerland,
        }
        data.update(overrides)
        return DelegationReport.objects.create(**data)

    def test_reference_number_is_generated_sequentially(self):
        first = self._report()
        second = self._report(title="Second delegation")
        year = timezone.now().year
        self.assertEqual(first.reference_number, f"DR-{year}-0001")
        self.assertEqual(second.reference_number, f"DR-{year}-0002")

    def test_future_or_inverted_engagement_dates_are_rejected(self):
        future = self._report()
        future.engagement_start_date = timezone.localdate() + timedelta(days=1)
        with self.assertRaises(ValidationError):
            future.full_clean()

        inverted = self._report(title="Inverted dates")
        inverted.engagement_start_date = date(2026, 3, 5)
        inverted.engagement_end_date = date(2026, 3, 1)
        with self.assertRaises(ValidationError):
            inverted.full_clean()

    def test_overdue_identifier_when_due_date_expired_and_open(self):
        report = self._report(deadline=timezone.now() - timedelta(days=1))
        self.assertTrue(report.is_overdue)
        self.assertEqual(report.overdue_identifier, OVERDUE_IDENTIFIER)

        report.deadline = timezone.now() + timedelta(days=1)
        self.assertFalse(report.is_overdue)
        self.assertEqual(report.overdue_identifier, "")

    def test_closed_report_is_not_overdue(self):
        closed = self.wt.states.get(name="Closed – House approved")
        report = self._report(
            current_state=closed, deadline=timezone.now() - timedelta(days=10)
        )
        self.assertFalse(report.is_overdue)

    def test_participants_and_resolutions_attach_to_report(self):
        report = self._report()
        participant = DelegationParticipant.objects.create(
            delegation_report=report,
            participant_type=DelegationParticipant.MEMBER,
            title="Hon.",
            first_name="Naledi",
            last_name="Mokoena",
            delegation_role="Leader of the Delegation",
        )
        self.assertEqual(participant.full_name, "Hon. Naledi Mokoena")
        self.assertEqual(list(report.participants.all()), [participant])

        res_type = WorkflowType.objects.get(name="International Resolution")
        resolution = InternationalResolution.objects.create(
            workflow_type=res_type,
            current_state=res_type.get_initial_state(),
            resolution_number="IR-BRS-1",
            title="Implement the resolution",
            owner=self.user,
        )
        report.add_sub_workflow(resolution)
        self.assertEqual(report.get_all_descendants(), [resolution])

    def test_the_same_person_cannot_be_added_twice(self):
        """The database refuses a duplicate delegate, whatever route creates it."""
        report = self._report()
        participant = {
            "first_name": "Naledi",
            "last_name": "Mokoena",
            "user": self.user,
        }
        DelegationParticipant.objects.create(delegation_report=report, **participant)

        with self.assertRaises(IntegrityError), transaction.atomic():
            DelegationParticipant.objects.create(
                delegation_report=report, **participant
            )

        # Somebody can still join a different delegation.
        DelegationParticipant.objects.create(
            delegation_report=self._report(), **participant
        )

    def test_assigned_official_email_is_exposed(self):
        report = self._report()
        self.assertEqual(report.assigned_to_email, "irpd@example.com")


class InternationalAgreementTests(TestCase):
    """BRS attributes for international agreements (BR02/BR12)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="agreement", email="agreement@example.com", password="pw"
        )
        cls.wt = WorkflowType.objects.get(name="International Agreement")
        cls.initial = cls.wt.get_initial_state()

    def _agreement(self, **overrides):
        data = {
            "workflow_type": self.wt,
            "current_state": self.initial,
            "title": "SADC Trade Protocol",
            "owner": self.user,
            "assigned_to": self.user,
            "agreement_type": InternationalAgreement.SECTION_231_2,
            "submitting_department": "Department of International Relations",
            "responsible_minister_name": "Minister of International Relations",
        }
        data.update(overrides)
        return InternationalAgreement.objects.create(**data)

    def test_reference_number_is_generated_sequentially(self):
        first = self._agreement()
        second = self._agreement(title="Second agreement")
        year = timezone.now().year
        self.assertEqual(first.reference_number, f"IA-{year}-0001")
        self.assertEqual(second.reference_number, f"IA-{year}-0002")

    def test_new_agreement_starts_agreement_tabled(self):
        agreement = self._agreement()
        self.assertEqual(
            agreement.current_state.name,
            "Agreement Tabled – referred to Committee",
        )

    def test_overdue_identifier_when_due_date_expired_and_open(self):
        agreement = self._agreement(deadline=timezone.now() - timedelta(days=1))
        self.assertTrue(agreement.is_overdue)
        self.assertEqual(agreement.overdue_identifier, OVERDUE_IDENTIFIER)

        agreement.deadline = timezone.now() + timedelta(days=1)
        self.assertFalse(agreement.is_overdue)
        self.assertEqual(agreement.overdue_identifier, "")

    def test_closed_agreement_is_not_overdue(self):
        closed = self.wt.states.get(name="Closed – House approved")
        agreement = self._agreement(
            current_state=closed, deadline=timezone.now() - timedelta(days=10)
        )
        self.assertFalse(agreement.is_overdue)

    def test_referral_committees_attach_to_agreement(self):
        committee = Group.objects.create(
            name="Portfolio Committee on Trade", group_type="portfolio_committee"
        )
        agreement = self._agreement()
        agreement.referral_committees.add(committee)
        self.assertEqual(list(agreement.referral_committees.all()), [committee])

    def test_assigned_official_email_is_exposed(self):
        agreement = self._agreement()
        self.assertEqual(agreement.assigned_to_email, "agreement@example.com")


class BillTests(TestCase):
    """Bill profile fields and the public-status mapping (Online Bill Tracking BRS)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="bill", email="bill@example.com", password="pw"
        )
        cls.wt = WorkflowType.objects.get(name="Bill")
        cls.initial = cls.wt.get_initial_state()

    def _bill(self, **overrides):
        data = {
            "workflow_type": self.wt,
            "current_state": self.initial,
            "bill_number": "B 12—2026",
            "title": "National Health Amendment Bill",
            "short_title": "Health Amendment",
            "bill_type": Bill.SECTION_76,
            "house_of_origin": Bill.NA,
            "sponsor_name": "Minister of Health",
            "introduced_date": date(2026, 6, 1),
            "owner": self.user,
            "assigned_to": self.user,
        }
        data.update(overrides)
        return Bill.objects.create(**data)

    def test_bill_profile_fields_are_captured(self):
        committee = Group.objects.create(
            name="Portfolio Committee on Health", group_type="portfolio_committee"
        )
        bill = self._bill(responsible_committee=committee)
        self.assertEqual(bill.short_title, "Health Amendment")
        self.assertEqual(bill.bill_type, Bill.SECTION_76)
        self.assertEqual(bill.house_of_origin, Bill.NA)
        self.assertEqual(bill.sponsor_name, "Minister of Health")
        self.assertEqual(bill.responsible_committee, committee)
        self.assertEqual(bill.__str__(), "B 12—2026 – National Health Amendment Bill")

    def test_public_status_collapses_internal_stages(self):
        bill = self._bill()
        self.assertEqual(bill.public_status, "Introduced")

        # Each detailed committee stage publishes as the same status (BRS §12).
        for stage in (
            "Referred to Committee",
            "Committee Deliberation",
            "House Debate and Voting",
        ):
            bill.current_state = self.wt.states.get(name=stage)
            self.assertEqual(bill.public_status, "Under Parliamentary Consideration")

        bill.current_state = self.wt.states.get(name="NCOP Consideration")
        self.assertEqual(bill.public_status, "National Council of Provinces")

    def test_public_status_is_empty_without_a_state(self):
        bill = self._bill()
        bill.current_state = None
        self.assertEqual(bill.public_status, "")

    def test_overdue_identifier_when_due_date_expired_and_open(self):
        bill = self._bill(deadline=timezone.now() - timedelta(days=1))
        self.assertTrue(bill.is_overdue)
        self.assertEqual(bill.overdue_identifier, OVERDUE_IDENTIFIER)

        closed = self.wt.states.get(name="Signed into Law")
        bill.current_state = closed
        self.assertFalse(bill.is_overdue)

    def test_assigned_official_email_is_exposed(self):
        bill = self._bill()
        self.assertEqual(bill.assigned_to_email, "bill@example.com")


class BillVersionTests(TestCase):
    """Preserved bill version history and the derived current version (BRS §15A)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="bill-version", password="pw")
        cls.wt = WorkflowType.objects.get(name="Bill")
        cls.bill = Bill.objects.create(
            workflow_type=cls.wt,
            current_state=cls.wt.get_initial_state(),
            bill_number="B 20—2026",
            title="Version integrity bill",
            owner=cls.user,
        )

    def test_current_version_is_empty_until_versions_exist(self):
        self.assertEqual(self.bill.current_version, "")

    def test_current_version_falls_back_to_the_latest_version(self):
        BillVersion.objects.create(
            bill=self.bill,
            version_label="B 20—2026",
            version_type=BillVersion.INTRODUCED,
            version_date=date(2026, 5, 1),
            recorded_by=self.user,
        )
        latest = BillVersion.objects.create(
            bill=self.bill,
            version_label="B 20—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            version_date=date(2026, 6, 1),
            recorded_by=self.user,
        )
        self.assertEqual(self.bill.current_version, latest.version_label)

    def test_flagging_a_version_current_demotes_the_previous_one(self):
        introduced = BillVersion.objects.create(
            bill=self.bill,
            version_label="B 20—2026",
            version_type=BillVersion.INTRODUCED,
            version_date=date(2026, 5, 1),
            is_current=True,
        )
        amended = BillVersion.objects.create(
            bill=self.bill,
            version_label="B 20—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            version_date=date(2026, 6, 1),
            is_current=True,
        )

        introduced.refresh_from_db()
        self.assertFalse(introduced.is_current)
        self.assertTrue(amended.is_current)
        self.assertEqual(
            BillVersion.objects.filter(bill=self.bill, is_current=True).count(), 1
        )
        self.assertEqual(self.bill.current_version, amended.version_label)

    def test_amendment_schedule_is_distinguished_from_the_amended_bill(self):
        schedule = BillVersion.objects.create(
            bill=self.bill,
            version_label="B 20—2026 (amendment schedule)",
            version_type=BillVersion.AMENDMENT_SCHEDULE,
            version_date=date(2026, 6, 1),
        )
        self.assertEqual(schedule.get_version_type_display(), "Amendment schedule")
        self.assertFalse(schedule.is_current)

    def test_versions_are_listed_newest_first(self):
        older = BillVersion.objects.create(
            bill=self.bill, version_label="older", version_date=date(2026, 1, 1)
        )
        newer = BillVersion.objects.create(
            bill=self.bill, version_label="newer", version_date=date(2026, 2, 1)
        )
        self.assertEqual(list(self.bill.versions.all()), [newer, older])


class ImportBillVersionsCommandTests(TestCase):
    """The bill-version bulk importer (``import_bill_versions``)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="version-import", password="pw")
        cls.wt = WorkflowType.objects.get(name="Bill")
        cls.bill = Bill.objects.create(
            workflow_type=cls.wt,
            current_state=cls.wt.get_initial_state(),
            bill_number="B 40—2026",
            title="Imported versions",
            owner=cls.user,
        )

    def _csv(self, text):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as handle:
            handle.write(text)
        self.addCleanup(Path(handle.name).unlink)
        return handle.name

    def _rows(self):
        return BillVersion.objects.filter(bill=self.bill)

    def test_import_creates_versions_and_honours_is_current(self):
        path = self._csv(
            "bill_number,version_label,version_type,version_date,document_url,notes,is_current\n"
            "B 40—2026,B 40—2026,introduced,2026-05-01,,Introduced,yes\n"
            "B 40—2026,B 40—2026 (1st amendment),amended,2026-06-01,"
            "https://example.com/v2,Amended clause 4,yes\n"
        )
        out = StringIO()
        call_command("import_bill_versions", path, stdout=out)

        self.assertEqual(self._rows().count(), 2)
        # The model normalises is_current, so only the last row stays current.
        self.assertEqual(self._rows().filter(is_current=True).count(), 1)
        self.assertEqual(
            self._rows().get(is_current=True).version_label,
            "B 40—2026 (1st amendment)",
        )
        self.assertEqual(self.bill.current_version, "B 40—2026 (1st amendment)")
        self.assertIn("Created:", out.getvalue())

    def test_accepts_version_type_labels_and_blank_date(self):
        path = self._csv(
            "bill_number,version_label,version_type,version_date\n"
            "B 40—2026,B 40—2026 (schedule),Amendment schedule,\n"
        )
        call_command("import_bill_versions", path, stdout=StringIO())

        version = self._rows().get()
        self.assertEqual(version.version_type, BillVersion.AMENDMENT_SCHEDULE)
        self.assertEqual(version.version_date, timezone.localdate())

    def test_reimport_is_idempotent(self):
        path = self._csv(
            "bill_number,version_label,version_date\nB 40—2026,B 40—2026,2026-05-01\n"
        )
        call_command("import_bill_versions", path, stdout=StringIO())
        call_command("import_bill_versions", path, stdout=StringIO())
        self.assertEqual(self._rows().count(), 1)

    def test_dry_run_writes_nothing(self):
        path = self._csv("bill_number,version_label\nB 40—2026,B 40—2026\n")
        out = StringIO()
        call_command("import_bill_versions", path, "--dry-run", stdout=out)
        self.assertEqual(self._rows().count(), 0)
        self.assertIn("DRY RUN", out.getvalue())

    def test_unknown_bill_number_is_reported_and_skipped(self):
        path = self._csv("bill_number,version_label\nB 999—2026,Nope\n")
        out = StringIO()
        call_command("import_bill_versions", path, stdout=out)
        self.assertEqual(BillVersion.objects.count(), 0)
        self.assertIn("no bill with number", out.getvalue())

    def test_invalid_version_type_is_reported(self):
        path = self._csv(
            "bill_number,version_label,version_type\nB 40—2026,B 40—2026,sideways\n"
        )
        out = StringIO()
        call_command("import_bill_versions", path, stdout=out)
        self.assertEqual(self._rows().count(), 0)
        self.assertIn("unknown version type", out.getvalue())

    def test_missing_required_column_is_rejected(self):
        path = self._csv("version_label\nOnly a label\n")
        with self.assertRaises(CommandError):
            call_command("import_bill_versions", path, stdout=StringIO())


class WorkflowEventTests(TestCase):
    """Append-only domain events and their use as transition guards."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="events", email="events@example.com", password="pw"
        )
        cls.resolution_type = WorkflowType.objects.get(name="International Resolution")
        cls.report_type = WorkflowType.objects.get(name="Delegation Report")
        cls.atc_type = EventType.objects.get(slug="atc-update-published")
        cls.attach_type = EventType.objects.get(slug="report-document-attached")

    def _resolution(self, number="IR-EVT-1"):
        return InternationalResolution.objects.create(
            workflow_type=self.resolution_type,
            current_state=self.resolution_type.get_initial_state(),
            resolution_number=number,
            title="Event test",
            owner=self.user,
        )

    def test_record_event_stores_context_and_lists_newest_first(self):
        resolution = self._resolution()
        first = resolution.record_event(
            self.attach_type,
            actor=self.user,
            payload={"document_url": "https://sp.example/doc"},
        )
        second = resolution.record_event(self.atc_type, origin="integration")

        self.assertEqual(first.actor, self.user)
        self.assertEqual(first.origin, "user")
        self.assertEqual(first.payload, {"document_url": "https://sp.example/doc"})
        self.assertEqual(second.origin, "integration")
        self.assertEqual(
            [event.pk for event in resolution.events()], [second.pk, first.pk]
        )

    def test_events_are_scoped_to_their_instance(self):
        first = self._resolution(number="IR-EVT-1")
        second = self._resolution(number="IR-EVT-2")
        first.record_event(self.attach_type)

        self.assertEqual(first.events().count(), 1)
        self.assertEqual(second.events().count(), 0)

    def test_events_are_append_only(self):
        event = self._resolution().record_event(self.attach_type)
        event.notes = "edited afterwards"

        with self.assertRaises(ValidationError):
            event.save()
        self.assertTrue(WorkflowEvent.objects.filter(pk=event.pk).exists())

    def test_unguarded_transitions_have_no_conditions(self):
        resolution = self._resolution()
        for transition in self.resolution_type.transitions.all():
            self.assertEqual(resolution.unmet_transition_conditions(transition), [])

    def test_required_event_blocks_transition_until_recorded(self):
        close = self.report_type.transitions.get(name="Close – House approved")
        self.assertIn(self.atc_type, close.required_event_types.all())

        report = DelegationReport.objects.create(
            workflow_type=self.report_type,
            current_state=self.report_type.states.get(
                name="Tabled and referred to Committee"
            ),
            title="Report awaiting closure",
            owner=self.user,
        )

        # Blocked: the ATC update has not been published yet.
        self.assertEqual(len(report.unmet_transition_conditions(close)), 1)
        with self.assertRaises(ValidationError):
            report.perform_transition(close, actor=self.user, comment="closing")
        report.refresh_from_db()
        self.assertEqual(report.current_state.name, "Tabled and referred to Committee")
        self.assertEqual(TransitionLog.objects.filter(object_id=report.pk).count(), 0)

        # Recording the evidence unlocks the transition.
        report.record_event(self.atc_type, actor=self.user, notes="ATC 2026, p. 14")
        self.assertEqual(report.unmet_transition_conditions(close), [])
        log = report.perform_transition(close, actor=self.user, comment="ATC published")

        report.refresh_from_db()
        self.assertEqual(report.current_state.name, "Closed – House approved")
        self.assertEqual(log.to_state, report.current_state)


class ReferralTests(TestCase):
    """Typed referrals — the replacement for the referred_to_groups M2M."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="referrer", email="referrer@example.com", password="pw"
        )
        cls.wt = WorkflowType.objects.get(name="International Resolution")
        cls.group = Group.objects.create(
            name="Referrals Portfolio Committee", group_type="portfolio_committee"
        )

    def _resolution(self, number="IR-REF-1"):
        return InternationalResolution.objects.create(
            workflow_type=self.wt,
            current_state=self.wt.get_initial_state(),
            resolution_number=number,
            title="Referral test",
            owner=self.user,
        )

    def test_refer_creates_open_referral_and_event(self):
        resolution = self._resolution()
        referral = resolution.refer(
            self.group,
            referred_by=self.user,
            due_date=timezone.now() + timedelta(days=7),
            notes="Please consider",
        )

        self.assertTrue(referral.is_open)
        self.assertEqual(list(resolution.open_referrals()), [referral])
        event = resolution.events().get()
        self.assertEqual(event.event_type.slug, "referral-created")
        self.assertEqual(event.actor, self.user)
        self.assertEqual(event.payload["referred_to"], self.group.name)

    def test_refer_is_gated_by_state_allows_referrals(self):
        wt = WorkflowType.objects.create(name="Test No Referral Type")
        state = State.objects.create(
            workflow_type=wt, name="Locked", is_initial=True, allows_referrals=False
        )
        resolution = InternationalResolution.objects.create(
            workflow_type=wt,
            current_state=state,
            resolution_number="IR-REF-2",
            title="Locked",
            owner=self.user,
        )

        with self.assertRaises(ValidationError):
            resolution.refer(self.group, referred_by=self.user)

    def test_respond_sets_fields_and_emits_event_once(self):
        resolution = self._resolution(number="IR-REF-3")
        referral = resolution.refer(self.group, referred_by=self.user)

        referral.respond(
            responded_by=self.user,
            document_url="https://sp.example/response.pdf",
            notes="Committee supports the resolution.",
        )
        referral.save()  # unchanged status must not re-emit

        self.assertEqual(
            resolution.events().filter(event_type__slug="referral-responded").count(), 1
        )
        referral.refresh_from_db()
        self.assertEqual(referral.status, "responded")
        self.assertEqual(referral.responded_by, self.user)
        self.assertIsNotNone(referral.responded_at)
        self.assertEqual(
            referral.response_document_url, "https://sp.example/response.pdf"
        )
        self.assertEqual(resolution.open_referrals().count(), 0)

    def test_recall_and_expiry_lifecycle(self):
        resolution = self._resolution(number="IR-REF-4")
        recalled = resolution.refer(self.group)
        recalled.recall(recalled_by=self.user, reason="No longer needed")

        self.assertEqual(recalled.status, "recalled")
        self.assertEqual(recalled.recall_reason, "No longer needed")
        self.assertTrue(
            resolution.events().filter(event_type__slug="referral-recalled").exists()
        )
        with self.assertRaises(ValidationError):
            recalled.respond(responded_by=self.user)

        overdue = resolution.refer(
            self.group, due_date=timezone.now() - timedelta(days=1)
        )
        self.assertTrue(overdue.is_overdue)
        overdue.mark_expired()

        self.assertEqual(overdue.status, "expired")
        self.assertTrue(
            resolution.events().filter(event_type__slug="referral-expired").exists()
        )


class TransitionConditionTests(TestCase):
    """TransitionCondition handlers evaluated by unmet_transition_conditions()."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="conditions", password="pw")
        cls.wt = WorkflowType.objects.create(name="Test Condition Type")
        cls.open_state = State.objects.create(
            workflow_type=cls.wt, name="Open", is_initial=True
        )
        cls.done_state = State.objects.create(
            workflow_type=cls.wt, name="Done", is_terminal=True
        )
        cls.finish = Transition.objects.create(
            workflow_type=cls.wt,
            name="Finish",
            from_state=cls.open_state,
            to_state=cls.done_state,
            requires_comment=False,
        )
        cls.group = Group.objects.create(
            name="Conditions Committee", group_type="portfolio_committee"
        )

    def _resolution(self, number="IR-CON-1"):
        return InternationalResolution.objects.create(
            workflow_type=self.wt,
            current_state=self.open_state,
            resolution_number=number,
            title="Condition test",
            owner=self.user,
        )

    def test_no_open_referrals_condition(self):
        TransitionCondition.objects.create(
            transition=self.finish, condition_type="no_open_referrals"
        )
        resolution = self._resolution()
        referral = resolution.refer(self.group)

        unmet = resolution.unmet_transition_conditions(self.finish)
        self.assertEqual(len(unmet), 1)
        self.assertIn("open referral", unmet[0])
        with self.assertRaises(ValidationError):
            resolution.perform_transition(self.finish, actor=self.user)

        referral.respond(responded_by=self.user)
        self.assertEqual(resolution.unmet_transition_conditions(self.finish), [])
        resolution.perform_transition(self.finish, actor=self.user)
        self.assertEqual(resolution.current_state, self.done_state)

    def test_field_set_condition(self):
        TransitionCondition.objects.create(
            transition=self.finish,
            condition_type="field_set",
            field_name="adoption_date",
        )
        resolution = self._resolution(number="IR-CON-2")

        self.assertEqual(len(resolution.unmet_transition_conditions(self.finish)), 1)
        resolution.adoption_date = date(2026, 1, 15)
        self.assertEqual(resolution.unmet_transition_conditions(self.finish), [])

    def test_disabled_condition_is_ignored(self):
        TransitionCondition.objects.create(
            transition=self.finish,
            condition_type="field_set",
            field_name="adoption_date",
            enabled=False,
        )

        self.assertEqual(
            self._resolution(number="IR-CON-3").unmet_transition_conditions(
                self.finish
            ),
            [],
        )

    def test_all_children_closed_condition(self):
        TransitionCondition.objects.create(
            transition=self.finish, condition_type="all_children_closed"
        )
        parent = self._resolution(number="IR-CON-4")
        child = self._resolution(number="IR-CON-5")
        parent.add_sub_workflow(child)

        unmet = parent.unmet_transition_conditions(self.finish)
        self.assertEqual(len(unmet), 1)
        self.assertIn("child workflow", unmet[0])

        child.current_state = self.done_state
        child.save(update_fields=["current_state", "updated_at"])
        self.assertEqual(parent.unmet_transition_conditions(self.finish), [])


class DelegationReportUpdateTests(TestCase):
    """BR03 update history: typed rows that emit the ATC publication event."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="updates", email="updates@example.com", password="pw"
        )
        cls.wt = WorkflowType.objects.get(name="Delegation Report")
        cls.tabled_state = cls.wt.states.get(name="Tabled and referred to Committee")
        cls.close = cls.wt.transitions.get(name="Close – House approved")

    def _report(self):
        return DelegationReport.objects.create(
            workflow_type=self.wt,
            current_state=self.tabled_state,
            title="Report with updates",
            owner=self.user,
        )

    def test_record_update_with_atc_details_emits_event_and_unlocks_close(self):
        report = self._report()
        update = report.record_update(
            atc_reference="ATC 2026, No. 12",
            atc_publication_date=date(2026, 4, 2),
            atc_page_number="14",
            atc_document_url="https://sp.example/atc.pdf",
            notes="Tabled and referred",
            recorded_by=self.user,
        )
        self.assertTrue(update.has_atc_details)
        self.assertEqual(
            report.events().filter(event_type__slug="atc-update-published").count(), 1
        )

        # Editing an update must not duplicate the event (create-only emission).
        update.notes = "edited"
        update.save()
        self.assertEqual(
            report.events().filter(event_type__slug="atc-update-published").count(), 1
        )

        # The seeded close guard is satisfied by the recorded evidence.
        self.assertEqual(report.unmet_transition_conditions(self.close), [])
        report.perform_transition(self.close, actor=self.user, comment="ATC published")
        self.assertEqual(report.current_state.name, "Closed – House approved")

    def test_record_update_without_atc_details_does_not_emit_event(self):
        report = self._report()
        update = report.record_update(notes="Internal note only")

        self.assertFalse(update.has_atc_details)
        self.assertFalse(report.events().exists())
        self.assertEqual(len(report.unmet_transition_conditions(self.close)), 1)

    def test_latest_atc_properties_reflect_newest_update(self):
        report = self._report()
        report.record_update(atc_reference="OLD", atc_page_number="1")
        report.record_update(atc_reference="NEW", atc_page_number="9")

        self.assertEqual(report.updates.count(), 2)
        self.assertEqual(report.atc_reference, "NEW")
        self.assertEqual(report.atc_page_number, "9")
        self.assertEqual(report.latest_update.atc_page_number, "9")
        self.assertIsNone(report.atc_publication_date)


class WorkflowCrudViewTests(TestCase):
    """The site's workflow CRUD views (list/detail/create/update/delete)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="crud", email="crud@example.com", password="pw"
        )
        # Group-scoped RBAC: creation needs one of a type's create roles, held
        # in the type's own group.
        cls.group = Group.objects.create(
            name="Test Workflow Group", group_type="portfolio_committee"
        )
        cls.creator_role = Role.objects.create(name="Test Workflow Creator")
        cls.other_role = Role.objects.create(name="Test Workflow Bystander")
        GroupMembership.objects.create(
            user=cls.user, group=cls.group, role=cls.creator_role
        )
        cls.report_type = WorkflowType.objects.get(name="Delegation Report")
        cls.resolution_type = WorkflowType.objects.get(name="International Resolution")
        cls.agreement_type = WorkflowType.objects.get(name="International Agreement")
        cls.bill_type = WorkflowType.objects.get(name="Bill")
        for workflow_type in (
            cls.report_type,
            cls.resolution_type,
            cls.agreement_type,
            cls.bill_type,
        ):
            workflow_type.group = cls.group
            workflow_type.save(update_fields=["group"])
            workflow_type.create_roles.add(cls.creator_role)
        # Option-list lengths are asserted below, so the shared group table must
        # hold only this class's group: seeded groups (e.g. the Government of
        # RSA ministries branch) would flip the group field to a search picker.
        Group.objects.exclude(pk=cls.group.pk).delete()

    def setUp(self):
        self.client.force_login(self.user)

    def _add_extra_users(self, count=11):
        """Push the user list past the form's search-picker threshold."""
        User = get_user_model()
        for number in range(count):
            User.objects.create_user(
                username=f"extra{number}", first_name=f"Extra{number}", last_name="User"
            )

    def _add_extra_groups(self, count=11):
        """Push the group list past the form's search-picker threshold."""
        for number in range(count):
            Group.objects.create(name=f"Extra group {number}")

    def _report_payload(self, **overrides):
        payload = {
            "workflow_type": self.report_type.pk,
            "title": "CRUD report",
            "description": "",
            "owner": self.user.pk,
            "assigned_to": "",
            "deadline": "",
            "priority": "medium",
            "engagement_name": "CRUD engagement",
            "engagement_start_date": "",
            "engagement_end_date": "",
            "location_city": "",
            "location_country": "",
            "notes": "",
            "report_document_url": "",
            # The report form also carries the delegates and adopted-resolutions
            # formsets. This is the management data for one untouched blank row in
            # each; ``participant_type`` repeats the select's initial value, as a
            # browser would, so the row counts as empty and is ignored.
            "participants-TOTAL_FORMS": "1",
            "participants-INITIAL_FORMS": "0",
            "participants-MIN_NUM_FORMS": "0",
            "participants-MAX_NUM_FORMS": "1000",
            "participants-0-id": "",
            "participants-0-user": "",
            "participants-0-participant_type": DelegationParticipant.MEMBER,
            "participants-0-delegation_role": "",
            "participants-0-DELETE": "",
            "resolutions-TOTAL_FORMS": "1",
            "resolutions-INITIAL_FORMS": "0",
            "resolutions-MIN_NUM_FORMS": "0",
            "resolutions-MAX_NUM_FORMS": "1000",
            "resolutions-0-resolution_number": "",
            "resolutions-0-title": "",
            "resolutions-0-adoption_date": "",
        }
        payload.update(overrides)
        return payload

    def _resolution_payload(self, **overrides):
        payload = {
            "workflow_type": self.resolution_type.pk,
            "title": "CRUD resolution",
            "description": "",
            "owner": self.user.pk,
            "assigned_to": "",
            "deadline": "",
            "priority": "medium",
            "resolution_number": "IR-CRUD-1",
            "resolution_text": "",
            "adoption_date": "",
            "responsible_group": "",
            "implementation_progress": "",
        }
        payload.update(overrides)
        return payload

    def _agreement_payload(self, **overrides):
        payload = {
            "workflow_type": self.agreement_type.pk,
            "title": "CRUD agreement",
            "description": "",
            "owner": self.user.pk,
            "assigned_to": "",
            "deadline": "",
            "priority": "medium",
            "agreement_type": InternationalAgreement.SECTION_231_3,
            "submitting_department": "Department of Justice",
            "responsible_minister_name": "Minister of Justice",
            "atc_tabling_date": "",
            "atc_reference": "",
            "referral_committees": [],
            "notes": "",
            "agreement_document_url": "",
            "explanatory_memorandum_url": "",
        }
        payload.update(overrides)
        return payload

    def _bill_payload(self, **overrides):
        payload = {
            "workflow_type": self.bill_type.pk,
            "title": "CRUD bill",
            "description": "",
            "owner": self.user.pk,
            "assigned_to": "",
            "deadline": "",
            "priority": "medium",
            "bill_number": "B 99—2026",
            "short_title": "CRUD",
            "bill_type": Bill.SECTION_75,
            "house_of_origin": Bill.NA,
            "sponsor_name": "Minister of Justice",
            "introduced_date": "",
            "responsible_committee": "",
            "atc_reference": "",
            "order_paper_reference": "",
            "bill_document_url": "",
            "notes": "",
        }
        payload.update(overrides)
        return payload

    def test_pages_require_login(self):
        self.client.logout()
        for name in (
            "delegation_reports",
            "international_resolutions",
            "international_agreements",
            "bills",
        ):
            response = self.client.get(reverse(f"pwms:{name}"))
            self.assertEqual(response.status_code, 302)
            self.assertIn("/pwms/login/", response["Location"])

    def test_delegation_report_crud_cycle(self):
        # Create: the initial state is derived from the chosen workflow type.
        response = self.client.post(
            reverse("pwms:delegation_report_create"), self._report_payload()
        )
        report = DelegationReport.objects.get(title="CRUD report")
        self.assertRedirects(
            response,
            reverse("pwms:delegation_report_detail", args=[report.public_id]),
        )
        self.assertEqual(report.current_state, self.report_type.get_initial_state())
        self.assertTrue(report.reference_number.startswith("DR-"))

        # Detail page renders the report.
        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, report.reference_number)

        # Update: workflow_type is fixed, current_state is editable instead.
        edit = self.client.get(
            reverse("pwms:delegation_report_update", args=[report.public_id])
        )
        self.assertContains(edit, "current_state")
        self.assertNotContains(edit, 'name="workflow_type"')
        response = self.client.post(
            reverse("pwms:delegation_report_update", args=[report.public_id]),
            self._report_payload(
                title="CRUD report (edited)",
                current_state=report.current_state.pk,
                priority="high",
                engagement_name="",
            ),
        )
        self.assertRedirects(
            response,
            reverse("pwms:delegation_report_detail", args=[report.public_id]),
        )
        report.refresh_from_db()
        self.assertEqual(report.title, "CRUD report (edited)")
        self.assertEqual(report.priority, "high")

        # Delete: POST removes the row, GET only confirms.
        confirm = self.client.get(
            reverse("pwms:delegation_report_delete", args=[report.public_id])
        )
        self.assertEqual(confirm.status_code, 200)
        response = self.client.post(
            reverse("pwms:delegation_report_delete", args=[report.public_id])
        )
        self.assertRedirects(response, reverse("pwms:delegation_reports"))
        self.assertFalse(DelegationReport.objects.filter(pk=report.pk).exists())

    def test_report_form_records_delegates(self):
        """Delegates chosen through the user lookup are saved with their role."""
        delegate = get_user_model().objects.create_user(
            username="mokoena", first_name="Naledi", last_name="Mokoena"
        )
        payload = self._report_payload()
        payload["participants-0-user"] = delegate.pk
        payload["participants-0-delegation_role"] = "Leader of the Delegation"

        response = self.client.post(reverse("pwms:delegation_report_create"), payload)
        report = DelegationReport.objects.get(title="CRUD report")
        self.assertRedirects(
            response,
            reverse("pwms:delegation_report_detail", args=[report.public_id]),
        )

        participant = report.participants.get()
        self.assertEqual(participant.user, delegate)
        self.assertEqual(participant.delegation_role, "Leader of the Delegation")
        # The person's names are taken from the account the row picked.
        self.assertEqual(participant.first_name, "Naledi")
        self.assertEqual(participant.last_name, "Mokoena")

        # The edit form lists the delegate again as one of the report's cards.
        edit = self.client.get(
            reverse("pwms:delegation_report_update", args=[report.public_id])
        )
        self.assertContains(edit, delegate.display_name)
        self.assertContains(edit, "Leader of the Delegation")

    def test_report_form_rejects_the_same_delegate_twice(self):
        """One person cannot be listed twice on the same report."""
        delegate = get_user_model().objects.create_user(
            username="twice", first_name="Tanya", last_name="Twice"
        )
        payload = self._report_payload()
        payload.update(
            {
                "participants-TOTAL_FORMS": "2",
                "participants-0-user": delegate.pk,
                "participants-0-delegation_role": "Delegate",
                "participants-1-id": "",
                "participants-1-user": delegate.pk,
                "participants-1-participant_type": DelegationParticipant.MEMBER,
                "participants-1-delegation_role": "Head Delegate",
                "participants-1-DELETE": "",
            }
        )

        response = self.client.post(reverse("pwms:delegation_report_create"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DelegationReport.objects.exists())
        # The clash is reported against the row, not swallowed.
        self.assertContains(response, "is already on this delegation")

    def test_report_form_rejects_the_same_resolution_number_twice(self):
        """Two rows cannot claim the same resolution number."""
        payload = self._report_payload()
        payload.update(
            {
                "resolutions-TOTAL_FORMS": "2",
                "resolutions-0-resolution_number": "IR-TWICE-1",
                "resolutions-0-title": "First outcome",
                "resolutions-1-resolution_number": "IR-TWICE-1",
                "resolutions-1-title": "Second outcome",
                "resolutions-1-adoption_date": "",
                "resolutions-1-DELETE": "",
            }
        )

        response = self.client.post(reverse("pwms:delegation_report_create"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DelegationReport.objects.exists())
        self.assertContains(response, "is listed twice")

    def test_report_form_offers_one_line_adders(self):
        """Delegates and resolutions are added from a one-line row, not blank rows."""
        response = self.client.get(reverse("pwms:delegation_report_create"))

        self.assertEqual(response.status_code, 200)
        # Each adder posts its entry through the formset it belongs to.
        self.assertContains(response, "participants-TOTAL_FORMS")
        self.assertContains(response, "resolutions-TOTAL_FORMS")
        self.assertContains(response, 'id="id_delegate_adder-user_search"')
        self.assertContains(response, 'id="id_resolution_adder-title"')
        # Nothing is pre-rendered: the page builds each row when one is added.
        self.assertNotContains(response, 'name="participants-0-user"')
        self.assertNotContains(response, 'name="resolutions-0-resolution_number"')
        # The hidden <template> holds the empty form the page clones, and each
        # card slot names the adder control whose value fills it in.
        self.assertContains(response, "participants-__prefix__-user")
        self.assertContains(response, "resolutions-__prefix__-title")
        self.assertContains(response, 'data-adder-slot="delegation_role"')
        self.assertContains(response, "data-adder-remove")

    def test_report_form_creates_and_links_child_resolutions(self):
        """A resolution entered on the report form is created and nested under it."""
        payload = self._report_payload()
        payload["resolutions-0-resolution_number"] = "IR-CHILD-1"
        payload["resolutions-0-title"] = "Implement the outcome"

        response = self.client.post(reverse("pwms:delegation_report_create"), payload)
        report = DelegationReport.objects.get(title="CRUD report")
        self.assertRedirects(
            response,
            reverse("pwms:delegation_report_detail", args=[report.public_id]),
        )

        resolution = InternationalResolution.objects.get(resolution_number="IR-CHILD-1")
        self.assertEqual(resolution.title, "Implement the outcome")
        self.assertEqual(resolution.workflow_type, self.resolution_type)
        self.assertEqual(
            resolution.current_state, self.resolution_type.get_initial_state()
        )
        self.assertEqual(resolution.owner, self.user)
        self.assertEqual(report.get_all_descendants(), [resolution])

    def test_resolution_form_links_to_an_existing_report(self):
        """The resolution form nests the resolution under a chosen report."""
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Parent report"),
        )
        report = DelegationReport.objects.get(title="Parent report")

        response = self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(parent_report=report.pk),
        )
        resolution = InternationalResolution.objects.get(title="CRUD resolution")
        self.assertRedirects(
            response,
            reverse(
                "pwms:international_resolution_detail", args=[resolution.public_id]
            ),
        )
        self.assertEqual(resolution.parent_workflow, report)
        self.assertEqual(report.get_all_descendants(), [resolution])

    def test_resolution_update_can_detach_from_its_report(self):
        """Clearing the parent picker on the edit form detaches the resolution."""
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Parent report"),
        )
        report = DelegationReport.objects.get(title="Parent report")
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(parent_report=report.pk),
        )
        resolution = InternationalResolution.objects.get(title="CRUD resolution")
        self.assertEqual(resolution.parent_workflow, report)

        response = self.client.post(
            reverse(
                "pwms:international_resolution_update", args=[resolution.public_id]
            ),
            self._resolution_payload(
                current_state=resolution.current_state.pk, parent_report=""
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(
            InternationalResolution.objects.get(pk=resolution.pk).parent_workflow
        )

    def test_international_resolution_crud_cycle(self):
        response = self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(),
        )
        resolution = InternationalResolution.objects.get(resolution_number="IR-CRUD-1")
        self.assertRedirects(
            response,
            reverse(
                "pwms:international_resolution_detail", args=[resolution.public_id]
            ),
        )
        self.assertEqual(
            resolution.current_state, self.resolution_type.get_initial_state()
        )

        response = self.client.get(
            reverse("pwms:international_resolution_detail", args=[resolution.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "IR-CRUD-1")

        response = self.client.post(
            reverse("pwms:international_resolution_delete", args=[resolution.public_id])
        )
        self.assertRedirects(response, reverse("pwms:international_resolutions"))
        self.assertFalse(
            InternationalResolution.objects.filter(pk=resolution.pk).exists()
        )

    def test_international_agreement_crud_cycle(self):
        # Create: the initial state is derived from the chosen workflow type.
        committee = Group.objects.create(
            name="CRUD Committee", group_type="portfolio_committee"
        )
        response = self.client.post(
            reverse("pwms:international_agreement_create"),
            self._agreement_payload(referral_committees=[committee.pk]),
        )
        agreement = InternationalAgreement.objects.get(title="CRUD agreement")
        self.assertRedirects(
            response,
            reverse("pwms:international_agreement_detail", args=[agreement.public_id]),
        )
        self.assertEqual(
            agreement.current_state, self.agreement_type.get_initial_state()
        )
        self.assertTrue(agreement.reference_number.startswith("IA-"))
        self.assertEqual(list(agreement.referral_committees.all()), [committee])

        # The list page renders the new row.
        listing = self.client.get(reverse("pwms:international_agreements"))
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, agreement.reference_number)

        # Detail page renders the agreement and its BRS attributes.
        response = self.client.get(
            reverse("pwms:international_agreement_detail", args=[agreement.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, agreement.reference_number)
        self.assertContains(response, "Department of Justice")

        # Update: workflow_type is fixed, current_state is editable instead.
        edit = self.client.get(
            reverse("pwms:international_agreement_update", args=[agreement.public_id])
        )
        self.assertContains(edit, "current_state")
        self.assertNotContains(edit, 'name="workflow_type"')
        response = self.client.post(
            reverse("pwms:international_agreement_update", args=[agreement.public_id]),
            {
                "title": "CRUD agreement (edited)",
                "description": "",
                "current_state": agreement.current_state.pk,
                "owner": self.user.pk,
                "assigned_to": "",
                "deadline": "",
                "priority": "high",
                "agreement_type": InternationalAgreement.SECTION_231_2,
                "submitting_department": "Department of Justice",
                "responsible_minister_name": "Minister of Justice",
                "atc_tabling_date": "",
                "atc_reference": "",
                "referral_committees": [],
                "notes": "",
                "agreement_document_url": "",
                "explanatory_memorandum_url": "",
            },
        )
        self.assertRedirects(
            response,
            reverse("pwms:international_agreement_detail", args=[agreement.public_id]),
        )
        agreement.refresh_from_db()
        self.assertEqual(agreement.title, "CRUD agreement (edited)")
        self.assertEqual(agreement.priority, "high")

        # Delete: POST removes the row.
        response = self.client.post(
            reverse("pwms:international_agreement_delete", args=[agreement.public_id])
        )
        self.assertRedirects(response, reverse("pwms:international_agreements"))
        self.assertFalse(
            InternationalAgreement.objects.filter(pk=agreement.pk).exists()
        )

    def test_agreement_create_links_a_serving_minister(self):
        """The responsible-minister picker round-trips the linked account."""
        ministry = Group.objects.create(
            name="Ministry of Testing", group_type="ministry"
        )
        minister = get_user_model().objects.create_user(
            username="linked-minister", password="pw"
        )
        GroupMembership.objects.create(
            user=minister,
            group=ministry,
            role=Role.objects.get_or_create(name="Minister")[0],
        )

        response = self.client.post(
            reverse("pwms:international_agreement_create"),
            self._agreement_payload(responsible_minister=minister.pk),
        )

        agreement = InternationalAgreement.objects.get()
        self.assertRedirects(
            response,
            reverse("pwms:international_agreement_detail", args=[agreement.public_id]),
        )
        self.assertEqual(agreement.responsible_minister, minister)

    def test_bill_crud_cycle(self):
        # Create: the initial state is derived from the chosen workflow type.
        response = self.client.post(
            reverse("pwms:bill_create"),
            self._bill_payload(),
        )
        bill = Bill.objects.get(bill_number="B 99—2026")
        self.assertRedirects(
            response,
            reverse("pwms:bill_detail", args=[bill.public_id]),
        )
        self.assertEqual(bill.current_state, self.bill_type.get_initial_state())
        self.assertEqual(bill.public_status, "Introduced")

        # List and detail render the bill and its simplified public status.
        listing = self.client.get(reverse("pwms:bills"))
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "B 99—2026")

        detail = self.client.get(reverse("pwms:bill_detail", args=[bill.public_id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Public status")
        self.assertContains(detail, "Introduced")

        # Version history renders on the bill page (BRS §15A).
        BillVersion.objects.create(
            bill=bill,
            version_label="B 99—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            version_date=timezone.localdate(),
            is_current=True,
            recorded_by=self.user,
        )
        detail = self.client.get(reverse("pwms:bill_detail", args=[bill.public_id]))
        self.assertContains(detail, "Version history")
        self.assertContains(detail, "B 99—2026 (1st amendment)")

        # Update: workflow_type is fixed, current_state is editable instead.
        edit = self.client.get(reverse("pwms:bill_update", args=[bill.public_id]))
        self.assertContains(edit, "current_state")
        self.assertNotContains(edit, 'name="workflow_type"')
        response = self.client.post(
            reverse("pwms:bill_update", args=[bill.public_id]),
            {
                "title": "CRUD bill (edited)",
                "description": "",
                "current_state": bill.current_state.pk,
                "owner": self.user.pk,
                "assigned_to": "",
                "deadline": "",
                "priority": "high",
                "bill_number": "B 99—2026",
                "short_title": "CRUD",
                "bill_type": Bill.SECTION_75,
                "house_of_origin": Bill.NA,
                "sponsor_name": "Minister of Justice",
                "introduced_date": "",
                "responsible_committee": "",
                "atc_reference": "",
                "order_paper_reference": "",
                "bill_document_url": "",
                "notes": "",
            },
        )
        self.assertRedirects(
            response,
            reverse("pwms:bill_detail", args=[bill.public_id]),
        )
        bill.refresh_from_db()
        self.assertEqual(bill.title, "CRUD bill (edited)")
        self.assertEqual(bill.priority, "high")

        # Delete: POST removes the row.
        response = self.client.post(reverse("pwms:bill_delete", args=[bill.public_id]))
        self.assertRedirects(response, reverse("pwms:bills"))
        self.assertFalse(Bill.objects.filter(pk=bill.pk).exists())

    def test_bill_version_capture_from_the_web_ui(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")

        response = self.client.post(
            reverse("pwms:bill_version_create", args=[bill.public_id]),
            {
                "version_label": "B 99—2026 (1st amendment)",
                "version_type": BillVersion.AMENDED,
                "version_date": "2026-07-01",
                "is_current": "on",
                "document_url": "",
                "notes": "Amended clause 4",
            },
        )
        self.assertRedirects(
            response, reverse("pwms:bill_detail", args=[bill.public_id])
        )

        version = BillVersion.objects.get(bill=bill)
        self.assertEqual(version.recorded_by, self.user)
        self.assertTrue(version.is_current)
        self.assertEqual(bill.current_version, "B 99—2026 (1st amendment)")

        detail = self.client.get(reverse("pwms:bill_detail", args=[bill.public_id]))
        self.assertContains(detail, "Record version")
        self.assertContains(detail, "B 99—2026 (1st amendment)")
        self.assertContains(
            detail,
            reverse(
                "pwms:bill_version_update", args=[bill.public_id, version.public_id]
            ),
        )

    def test_bill_version_edit_from_the_web_ui(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")
        version = BillVersion.objects.create(
            bill=bill,
            version_label="B 99—2026",
            version_type=BillVersion.INTRODUCED,
            version_date=date(2026, 5, 1),
            recorded_by=self.user,
        )

        response = self.client.post(
            reverse(
                "pwms:bill_version_update", args=[bill.public_id, version.public_id]
            ),
            {
                "version_label": "B 99—2026 (corrected)",
                "version_type": BillVersion.AMENDED,
                "version_date": "2026-05-02",
                "is_current": "on",
                "document_url": "https://example.com/v1",
                "notes": "Corrected label",
            },
        )
        self.assertRedirects(
            response, reverse("pwms:bill_detail", args=[bill.public_id])
        )

        version.refresh_from_db()
        self.assertEqual(version.version_label, "B 99—2026 (corrected)")
        self.assertEqual(version.version_type, BillVersion.AMENDED)
        self.assertEqual(version.version_date, date(2026, 5, 2))
        self.assertTrue(version.is_current)
        self.assertEqual(version.notes, "Corrected label")
        # The correction is in place: identity and recorder are preserved.
        self.assertEqual(version.recorded_by, self.user)
        self.assertEqual(BillVersion.objects.filter(bill=bill).count(), 1)

    def test_bill_version_edit_can_move_the_current_flag(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")
        older = BillVersion.objects.create(
            bill=bill,
            version_label="B 99—2026",
            version_date=date(2026, 5, 1),
        )
        newer = BillVersion.objects.create(
            bill=bill,
            version_label="B 99—2026 (1st amendment)",
            version_date=date(2026, 6, 1),
            is_current=True,
        )

        response = self.client.post(
            reverse("pwms:bill_version_update", args=[bill.public_id, older.public_id]),
            {
                "version_label": older.version_label,
                "version_type": BillVersion.INTRODUCED,
                "version_date": "2026-05-01",
                "is_current": "on",
                "document_url": "",
                "notes": "",
            },
        )
        self.assertRedirects(
            response, reverse("pwms:bill_detail", args=[bill.public_id])
        )

        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertTrue(older.is_current)
        self.assertFalse(newer.is_current)
        self.assertEqual(bill.current_version, "B 99—2026")

    def test_bill_version_edit_requires_edit_permission(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")
        version = BillVersion.objects.create(
            bill=bill,
            version_label="B 99—2026",
            version_date=date(2026, 5, 1),
        )

        outsider = get_user_model().objects.create_user(
            username="version-editor-outsider", password="pw"
        )
        self.client.force_login(outsider)
        response = self.client.post(
            reverse(
                "pwms:bill_version_update", args=[bill.public_id, version.public_id]
            ),
            {
                "version_label": "Hijacked",
                "version_type": BillVersion.INTRODUCED,
                "version_date": "2026-05-01",
            },
        )
        self.assertEqual(response.status_code, 403)
        version.refresh_from_db()
        self.assertEqual(version.version_label, "B 99—2026")

    def test_bill_version_capture_requires_edit_permission(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")

        outsider = get_user_model().objects.create_user(
            username="version-outsider", password="pw"
        )
        self.client.force_login(outsider)
        response = self.client.post(
            reverse("pwms:bill_version_create", args=[bill.public_id]),
            {
                "version_label": "Sneaky version",
                "version_type": BillVersion.INTRODUCED,
                "version_date": "2026-07-01",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BillVersion.objects.filter(bill=bill).exists())

    def test_create_requires_a_type_with_states(self):
        empty_type = WorkflowType.objects.create(
            name="Test Empty Workflow Type", group=self.group
        )
        empty_type.create_roles.add(self.creator_role)
        response = self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(workflow_type=empty_type.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DelegationReport.objects.exists())

    def test_create_form_hides_the_type_and_owner(self):
        """The type is implicit and the owner is the acting user, so both are hidden."""
        response = self.client.get(reverse("pwms:delegation_report_create"))

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        # Submitted, but not controls the user can change.
        self.assertNotContains(response, '<select name="workflow_type"')
        self.assertContains(response, '<input type="hidden" name="workflow_type"')
        self.assertEqual(form["workflow_type"].value(), self.report_type.pk)
        self.assertNotContains(response, '<select name="owner"')
        self.assertContains(response, '<input type="hidden" name="owner"')
        self.assertEqual(form["owner"].value(), self.user.pk)

    def test_create_cannot_be_retargeted_at_another_workflow_type(self):
        """A forged workflow_type cannot create a mis-typed instance."""
        response = self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(workflow_type=self.agreement_type.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DelegationReport.objects.exists())

    def test_search_filters_the_list(self):
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Geneva delegation"),
        )
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="New York delegation"),
        )

        response = self.client.get(reverse("pwms:delegation_reports"), {"q": "Geneva"})
        self.assertContains(response, "Geneva delegation")
        self.assertNotContains(response, "New York delegation")

    def test_navbar_links_to_workflow_views(self):
        response = self.client.get(reverse("pwms:home"))
        for name in (
            "workflows",
            "delegation_reports",
            "international_resolutions",
            "international_agreements",
            "bills",
        ):
            self.assertContains(response, reverse(f"pwms:{name}"))

    def test_workflows_page_unifies_every_accessible_instance(self):
        """One table spans all four concrete workflow types."""
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Unified report"),
        )
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(title="Unified resolution"),
        )
        self.client.post(
            reverse("pwms:international_agreement_create"),
            self._agreement_payload(title="Unified agreement"),
        )
        self.client.post(
            reverse("pwms:bill_create"), self._bill_payload(title="Unified bill")
        )

        response = self.client.get(reverse("pwms:workflows"))
        self.assertEqual(response.status_code, 200)
        for title in (
            "Unified report",
            "Unified resolution",
            "Unified agreement",
            "Unified bill",
        ):
            self.assertContains(response, title)

        # The page's own template comment must not leak as literal text; a
        # multi-line ``{# #}`` comment (which Django does not accept) would.
        self.assertNotContains(response, "Data-driven create menu")

        # Every row links to its own detail page (not a placeholder).
        for model, view_name in (
            (DelegationReport, "pwms:delegation_report_detail"),
            (InternationalResolution, "pwms:international_resolution_detail"),
            (InternationalAgreement, "pwms:international_agreement_detail"),
            (Bill, "pwms:bill_detail"),
        ):
            instance = model.objects.get()
            self.assertContains(
                response, reverse(view_name, kwargs={"public_id": instance.public_id})
            )

    def test_workflows_filters_narrow_the_table(self):
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Geneva delegation", priority="high"),
        )
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(title="Geneva resolution"),
        )
        url = reverse("pwms:workflows")

        # Free text matches on the title.
        response = self.client.get(url, {"q": "Geneva"})
        self.assertContains(response, "Geneva delegation")
        self.assertContains(response, "Geneva resolution")

        # Type narrows to one register; the other type drops out.
        response = self.client.get(url, {"type": "Delegation Report"})
        self.assertContains(response, "Geneva delegation")
        self.assertNotContains(response, "Geneva resolution")

        # Priority filters on the stored value, not the display label.
        response = self.client.get(url, {"priority": "high"})
        self.assertContains(response, "Geneva delegation")
        self.assertNotContains(response, "Geneva resolution")

    def test_workflows_page_only_lists_instances_the_user_may_view(self):
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Private report"),
        )
        self.assertContains(
            self.client.get(reverse("pwms:workflows")), "Private report"
        )

        outsider = get_user_model().objects.create_user(
            username="workflow-outsider", password="pw"
        )
        self.client.force_login(outsider)

        response = self.client.get(reverse("pwms:workflows"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Private report")
        self.assertContains(response, "0 workflows")

    def test_create_requires_a_role_from_the_type_group(self):
        """No create role in the type's group -> no creation (403 on POST)."""
        User = get_user_model()
        outsider = User.objects.create_user(username="outsider", password="pw")
        self.client.force_login(outsider)

        response = self.client.get(reverse("pwms:delegation_report_create"))
        self.assertRedirects(response, reverse("pwms:delegation_reports"))

        response = self.client.post(
            reverse("pwms:delegation_report_create"), self._report_payload()
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DelegationReport.objects.exists())

    def test_create_page_requires_a_role_for_that_specific_type(self):
        """A create role for another type does not open this type's create form."""
        # The acting user keeps a create role for bills only, so the report form
        # (a different type) must not be offered. Rolled back with the test.
        self.report_type.create_roles.remove(self.creator_role)

        response = self.client.get(reverse("pwms:delegation_report_create"))
        self.assertRedirects(response, reverse("pwms:delegation_reports"))

        # The type whose role they do hold still opens.
        self.assertEqual(self.client.get(reverse("pwms:bill_create")).status_code, 200)

    def test_can_create_needs_the_role_in_the_types_own_group(self):
        User = get_user_model()
        other_group = Group.objects.create(
            name="Test Other Group", group_type="portfolio_committee"
        )

        # Right role, wrong group.
        wrong_group = User.objects.create_user(username="wrong_group", password="pw")
        GroupMembership.objects.create(
            user=wrong_group, group=other_group, role=self.creator_role
        )
        self.assertFalse(self.report_type.can_create(wrong_group))

        # Right group, role that is not a declared create role.
        wrong_role = User.objects.create_user(username="wrong_role", password="pw")
        GroupMembership.objects.create(
            user=wrong_role, group=self.group, role=self.other_role
        )
        self.assertFalse(self.report_type.can_create(wrong_role))

        self.assertTrue(self.report_type.can_create(self.user))
        self.assertFalse(self.report_type.can_create(None))

    def test_group_roles_come_from_the_types_group(self):
        User = get_user_model()
        colleague = User.objects.create_user(username="colleague", password="pw")
        GroupMembership.objects.create(
            user=colleague, group=self.group, role=self.other_role
        )

        self.assertEqual(
            set(self.report_type.group_roles()),
            {self.creator_role, self.other_role},
        )
        # other_role is held in the group but is not a declared create role.
        self.assertFalse(self.report_type.can_create(colleague))

    def test_superuser_can_create_any_enabled_type(self):
        User = get_user_model()
        root = User.objects.create_superuser(username="root", password="pw")

        self.assertTrue(self.report_type.can_create(root))
        self.assertIn(self.report_type, WorkflowType.creatable_by(root))

    def test_create_form_only_offers_creatable_types(self):
        ungrouped = WorkflowType.objects.create(name="Test Ungrouped Type")

        response = self.client.get(reverse("pwms:delegation_report_create"))
        self.assertEqual(response.status_code, 200)
        offered = response.context["form"].fields["workflow_type"].queryset
        self.assertIn(self.report_type, offered)
        self.assertNotIn(ungrouped, offered)

    def test_create_forms_use_flatpickr_date_pickers(self):
        """Date/time fields are flatpickr widgets and the page loads their media."""
        cases = (
            ("pwms:delegation_report_create", "deadline", "engagement_start_date"),
            (
                "pwms:international_resolution_create",
                "deadline",
                "adoption_date",
            ),
        )
        for url_name, datetime_field, date_field in cases:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                form = response.context["form"]
                self.assertIsInstance(
                    form.fields[datetime_field].widget, DateTimePickerInput
                )
                self.assertIsInstance(form.fields[date_field].widget, DatePickerInput)
                # Without the package's media on the page the inputs stay plain text.
                self.assertContains(response, "flatpickr.min.css")
                self.assertContains(response, "flatpickr.min.js")
                self.assertContains(response, "js/django-flatpickr.js")
                self.assertContains(response, "data-fpconfig")

    def test_group_search_returns_matching_groups_for_htmx(self):
        """The picker's endpoint returns the results fragment, not a full page."""
        response = self.client.get(
            reverse("pwms:group_search"), {"search": "Test Workflow"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/partials/group_search_results.html")
        self.assertContains(response, self.group.name)
        self.assertContains(response, f"selectGroup('{self.group.pk}'")

    def test_short_option_lists_stay_selects(self):
        """With few options the fields keep their plain <select>."""
        response = self.client.get(reverse("pwms:international_resolution_create"))

        self.assertEqual(response.context["form"].search_pickers, set())
        # owner is never chosen by hand, so it is hidden rather than a control.
        self.assertContains(response, '<input type="hidden" name="owner"')
        self.assertContains(response, '<select name="assigned_to"')
        self.assertContains(response, '<select name="responsible_group"')
        self.assertNotContains(response, 'id="id_assigned_to_search"')

    def test_long_option_lists_become_search_pickers(self):
        """More options than the threshold switches the fields to search fields."""
        self._add_extra_users()
        self._add_extra_groups()

        for url_name in (
            "pwms:delegation_report_create",
            "pwms:international_resolution_create",
        ):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                form = response.context["form"]

                self.assertEqual(response.status_code, 200)
                # owner is never a picker: it is hidden and fixed server-side.
                self.assertNotIn("owner", form.search_pickers)
                self.assertIn("assigned_to", form.search_pickers)
                # A <select> with every active user is not rendered.
                self.assertNotContains(response, '<select name="assigned_to"')
                self.assertContains(response, 'id="id_assigned_to_search"')
                self.assertContains(response, f'hx-get="{reverse("pwms:user_search")}"')
                # The hidden owner still carries the acting user.
                self.assertContains(response, '<input type="hidden" name="owner"')
                self.assertEqual(form["owner"].value(), self.user.pk)

    def test_group_field_becomes_a_picker_only_when_the_list_is_long(self):
        """The group picker follows the same threshold as the user fields."""
        url = reverse("pwms:international_resolution_create")
        short = self.client.get(url)
        self.assertNotIn("responsible_group", short.context["form"].search_pickers)

        self._add_extra_groups()
        long = self.client.get(url)
        self.assertIn("responsible_group", long.context["form"].search_pickers)
        self.assertNotContains(long, '<select name="responsible_group"')
        self.assertContains(long, 'id="id_responsible_group_search"')
        self.assertContains(long, f'hx-get="{reverse("pwms:group_search")}"')
        self.assertContains(long, 'hx-target="#id_responsible_group_results"')

    def test_picked_group_round_trips_through_the_form(self):
        """What the search stores is what the model keeps, and edit shows it."""
        self._add_extra_groups()
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(responsible_group=self.group.pk),
        )
        resolution = InternationalResolution.objects.get(title="CRUD resolution")
        self.assertEqual(resolution.responsible_group, self.group)

        response = self.client.get(
            reverse("pwms:international_resolution_update", args=[resolution.public_id])
        )
        # The hidden input carries the pk; the search box carries the name.
        self.assertContains(
            response, f'value="{self.group.pk}" id="id_responsible_group"'
        )
        self.assertContains(response, f'value="{self.group.name}"')

    def test_user_search_returns_matching_users_for_htmx(self):
        """The user picker's endpoint returns the results fragment, not a page."""
        response = self.client.get(
            reverse("pwms:user_search"), {"search": self.user.username}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/partials/user_search_results.html")
        self.assertContains(response, self.user.display_name)
        self.assertContains(response, f"selectUser('{self.user.pk}'")

    def test_picked_users_round_trip_through_the_form(self):
        """The assignee picked in the search is saved; the owner stays the actor."""
        self._add_extra_users()
        assignee = get_user_model().objects.create_user(
            username="assignee", first_name="Ada", last_name="Assignee"
        )
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(owner=self.user.pk, assigned_to=assignee.pk),
        )
        report = DelegationReport.objects.get(title="CRUD report")
        self.assertEqual(report.owner, self.user)
        self.assertEqual(report.assigned_to, assignee)

        response = self.client.get(
            reverse("pwms:delegation_report_update", args=[report.public_id])
        )
        self.assertContains(response, f'value="{assignee.pk}" id="id_assigned_to"')
        self.assertContains(response, f'value="{assignee.display_name}"')

    def test_location_fields_become_linked_pickers(self):
        """The location pair follows the threshold and is wired country -> city."""
        url = reverse("pwms:delegation_report_create")
        short = self.client.get(url)
        self.assertNotIn("location_country", short.context["form"].search_pickers)
        self.assertContains(short, '<select name="location_country"')

        for number in range(11):
            country = Country.objects.create(
                code=chr(65 + number), name=f"Test Country {number}"
            )
            City.objects.create(
                country=country,
                name=f"Test City {number}",
                latitude="1.5",
                longitude="2.5",
                population=1000,
            )

        response = self.client.get(url)
        form = response.context["form"]
        self.assertIn("location_country", form.search_pickers)
        self.assertIn("location_city", form.search_pickers)
        self.assertNotContains(response, '<select name="location_country"')
        self.assertNotContains(response, '<select name="location_city"')
        self.assertContains(response, 'id="id_location_country_search"')
        self.assertContains(response, 'id="id_location_city_search"')
        self.assertContains(response, f'hx-get="{reverse("pwms:country_search")}"')
        self.assertContains(response, f'hx-get="{reverse("pwms:city_search")}"')
        # The city search sends the selected country, and the country picker
        # resets the city when it changes.
        self.assertContains(
            response,
            'hx-vals=\'js:{"country": document.getElementById("id_location_country").value}\'',
        )
        self.assertContains(response, 'data-clears="#id_location_city_picker"')

    def test_location_round_trips_through_the_form(self):
        """A picked country/city is saved and shown again on the edit form."""
        country = Country.objects.create(code="ZZ", iso3="ZZZ", name="Testland")
        city = City.objects.create(
            country=country,
            name="Testville",
            latitude="1.5",
            longitude="2.5",
            population=42,
        )
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(location_country=country.pk, location_city=city.pk),
        )
        report = DelegationReport.objects.get(title="CRUD report")
        self.assertEqual(report.location_country, country)
        self.assertEqual(report.location_city, city)

        response = self.client.get(
            reverse("pwms:delegation_report_update", args=[report.public_id])
        )
        form = response.context["form"]
        self.assertEqual(str(form["location_country"].value()), str(country.pk))
        self.assertEqual(form.picker_labels["location_city"], "Testville")

    def test_update_and_delete_require_edit_and_delete_permission(self):
        """A non-owner without instance access gets 403 on edit/delete."""
        self.client.post(
            reverse("pwms:delegation_report_create"), self._report_payload()
        )
        report = DelegationReport.objects.get(title="CRUD report")

        User = get_user_model()
        stranger = User.objects.create_user(username="stranger", password="pw")
        self.client.force_login(stranger)

        edit_url = reverse("pwms:delegation_report_update", args=[report.public_id])
        delete_url = reverse("pwms:delegation_report_delete", args=[report.public_id])

        self.assertEqual(self.client.get(edit_url).status_code, 403)
        self.assertEqual(
            self.client.post(edit_url, {"title": "Hijacked"}).status_code, 403
        )
        self.assertEqual(self.client.get(delete_url).status_code, 403)
        self.assertEqual(self.client.post(delete_url).status_code, 403)
        self.assertTrue(DelegationReport.objects.filter(pk=report.pk).exists())

    def test_detail_requires_view_access(self):
        """A non-owner without instance access cannot open a detail page."""
        self.client.post(
            reverse("pwms:delegation_report_create"), self._report_payload()
        )
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(),
        )
        report = DelegationReport.objects.get(title="CRUD report")
        resolution = InternationalResolution.objects.get(resolution_number="IR-CRUD-1")

        User = get_user_model()
        stranger = User.objects.create_user(username="view_stranger", password="pw")
        self.client.force_login(stranger)

        report_url = reverse("pwms:delegation_report_detail", args=[report.public_id])
        resolution_url = reverse(
            "pwms:international_resolution_detail", args=[resolution.public_id]
        )
        self.assertEqual(self.client.get(report_url).status_code, 403)
        self.assertEqual(self.client.get(resolution_url).status_code, 403)

    def test_lists_hide_instances_without_view_access(self):
        """List pages only render rows the signed-in user may view."""
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Owner only report"),
        )
        report = DelegationReport.objects.get(title="Owner only report")

        User = get_user_model()
        stranger = User.objects.create_user(username="list_stranger", password="pw")
        self.client.force_login(stranger)

        response = self.client.get(reverse("pwms:delegation_reports"))
        self.assertNotContains(response, "Owner only report")

        # The owner still sees their own row.
        self.client.force_login(self.user)
        response = self.client.get(reverse("pwms:delegation_reports"))
        self.assertContains(response, "Owner only report")
        self.assertContains(response, report.reference_number)


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
        cls.state = State.objects.create(
            workflow_type=cls.wt, name="Open", is_initial=True
        )

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
        plain_state = State.objects.create(
            workflow_type=plain_type, name="Open", is_initial=True
        )
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
        state = State.objects.create(workflow_type=wt, name="Open", is_initial=True)
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
        state = State.objects.create(workflow_type=wt, name="Open", is_initial=True)
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
        state = State.objects.create(workflow_type=wt, name="Open", is_initial=True)
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
        state = State.objects.create(workflow_type=wt, name="Open", is_initial=True)
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
        cls.state = State.objects.create(
            workflow_type=cls.wt, name="Open", is_initial=True
        )
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
        state = State.objects.create(
            workflow_type=workflow_type, name="Open", is_initial=True
        )
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
        state = State.objects.create(
            workflow_type=workflow_type, name="Open", is_initial=True
        )
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


class PlaceDataTests(TestCase):
    """The bundled country/city reference data and the fragments it feeds."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="places", password="pw"
        )
        cls.south_africa = Country.objects.create(
            code="ZA", iso3="ZAF", name="South Africa", continent="AF"
        )
        cls.switzerland = Country.objects.create(
            code="CH", iso3="CHE", name="Switzerland", continent="EU"
        )
        # Contains "south" without starting with it, so the ranking rule has
        # something to place below South Africa.
        cls.french_southern = Country.objects.create(
            code="TF",
            iso3="ATF",
            name="French Southern Territories",
            continent="AN",
        )
        cls.cape_town = cls._city(cls.south_africa, "Cape Town", 4772846)
        cls.geneva = cls._city(cls.switzerland, "Geneva", 201818)
        cls.zurich = cls._city(cls.switzerland, "Zurich", 341730)

    @staticmethod
    def _city(country, name, population):
        return City.objects.create(
            country=country,
            name=name,
            ascii_name=name,
            latitude="1.5",
            longitude="2.5",
            population=population,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_country_search_fragment_matches_name_and_code(self):
        response = self.client.get(reverse("pwms:country_search"), {"search": "swit"})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/partials/country_search_results.html")
        self.assertContains(response, "Switzerland")
        self.assertContains(response, f"selectOption('{self.switzerland.pk}'")

    def test_country_search_ranks_prefix_matches_first(self):
        response = self.client.get(reverse("pwms:country_search"), {"search": "south"})

        names = [country.name for country in response.context["countries"]]
        self.assertEqual(names[0], "South Africa")
        self.assertIn("French Southern Territories", names)

    def test_city_search_is_scoped_to_the_country_it_is_sent(self):
        response = self.client.get(
            reverse("pwms:city_search"),
            {"country": self.switzerland.pk, "search": "z"},
        )

        self.assertContains(response, "Zurich")
        self.assertNotContains(response, "Cape Town")

    def test_city_search_ranks_biggest_places_first(self):
        response = self.client.get(
            reverse("pwms:city_search"), {"country": self.switzerland.pk}
        )

        names = [city.name for city in response.context["cities"]]
        self.assertEqual(names, ["Zurich", "Geneva"])

    def test_city_search_ignores_an_unusable_country_value(self):
        response = self.client.get(
            reverse("pwms:city_search"), {"country": "not-an-id", "search": "cap"}
        )

        self.assertContains(response, "Cape Town")

    def test_place_loader_reads_the_bundled_layout_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "countries.csv").write_text(
                "code,iso3,name,continent\nZZ,ZZZ,Zedland,AF\n", encoding="utf-8"
            )
            (data_dir / "cities.csv").write_text(
                "name,ascii_name,country_code,latitude,longitude,population\n"
                "Testville,Testville,ZZ,1.5,2.5,42\n",
                encoding="utf-8",
            )
            first = StringIO()
            call_command("load_places", data_dir=data_dir, stdout=first)
            second = StringIO()
            call_command("load_places", data_dir=data_dir, stdout=second)

        self.assertIn("cities: 1 created, 0 already present", first.getvalue())
        self.assertIn("cities: 0 created, 1 already present", second.getvalue())
        self.assertEqual(Country.objects.filter(code="ZZ").count(), 1)
        self.assertEqual(City.objects.get(name="Testville").country.code, "ZZ")


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
        cls.state = State.objects.create(
            workflow_type=cls.wt, name="Open", is_initial=True
        )
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


class DashboardViewTests(TestCase):
    """The dashboard summarises only the work the signed-in user can see."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.viewer = User.objects.create_user(
            username="dash-viewer",
            first_name="Dana",
            last_name="Viewer",
            password="pw",
        )
        cls.stranger = User.objects.create_user(username="dash-stranger", password="pw")
        cls.referrer = User.objects.create_user(
            username="dash-referrer",
            first_name="Rex",
            last_name="Referrer",
            password="pw",
        )
        cls.activity_actor = User.objects.create_user(
            username="dash-activity",
            first_name="Ada",
            last_name="Activity",
            password="pw",
        )
        cls.committee = Group.objects.create(
            name="Dash Referral Committee XZ", group_type="portfolio_committee"
        )
        cls.role = Role.objects.create(name="Dash Committee Member")
        GroupMembership.objects.create(
            user=cls.viewer, group=cls.committee, role=cls.role
        )

        report_type = WorkflowType.objects.get(name="Delegation Report")
        resolution_type = WorkflowType.objects.get(name="International Resolution")
        now = timezone.now()

        cls.overdue_report = DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Overdue delegation",
            owner=cls.viewer,
            deadline=now - timedelta(days=3),
        )
        cls.overdue_resolution = InternationalResolution.objects.create(
            workflow_type=resolution_type,
            current_state=resolution_type.get_initial_state(),
            title="Overdue resolution",
            resolution_number="IR-DASH-OVERDUE",
            owner=cls.viewer,
            deadline=now - timedelta(days=1),
        )
        cls.due_report = DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Due soon report",
            owner=cls.viewer,
            assigned_to=cls.viewer,
            deadline=now + timedelta(days=2),
        )
        cls.stranger_report = DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Stranger private report",
            owner=cls.stranger,
            deadline=now - timedelta(days=5),
        )
        cls.report_ct = ContentType.objects.get_for_model(DelegationReport)
        WorkflowReferral.objects.create(
            content_type=cls.report_ct,
            object_id=cls.due_report.pk,
            referred_to=cls.committee,
            referred_by=cls.referrer,
            due_date=now + timedelta(days=4),
        )
        TransitionLog.objects.create(
            content_type=cls.report_ct,
            object_id=cls.due_report.pk,
            action="STATE_TRANSITION",
            from_state=report_type.get_initial_state(),
            to_state=report_type.get_initial_state(),
            actor=cls.activity_actor,
        )

    def setUp(self):
        self.client.force_login(self.viewer)

    def _dashboard(self):
        return self.client.get(reverse("pwms:dashboard"))

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("pwms:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/pwms/login/", response["Location"])

    def test_counts_only_visible_work(self):
        """A stranger's work never inflates the viewer's metrics."""
        response = self._dashboard()
        self.assertContains(response, "Overdue delegation")
        self.assertContains(response, "Due soon report")
        self.assertNotContains(response, "Stranger private report")

    def test_overdue_work_surfaces_across_types(self):
        """Every concrete type honours the shared BR12 overdue rule."""
        response = self._dashboard()
        self.assertContains(response, "Overdue delegation")
        self.assertContains(response, "Overdue resolution")
        self.assertNotContains(response, "Nothing is past its deadline.")

    def test_due_soon_lists_upcoming_work(self):
        response = self._dashboard()
        self.assertContains(response, "Due soon report")
        self.assertNotContains(response, "Nothing falls due in the next 7 days.")

    def test_rows_show_how_far_each_workflow_has_travelled(self):
        """
        Each work-list row repeats the detail page's Progress measure, so where a
        record stands is visible without opening it.
        """
        resolution_type = WorkflowType.objects.get(name="International Resolution")
        self.overdue_resolution.current_state = resolution_type.states.get(
            name="In Progress"
        )
        self.overdue_resolution.save(update_fields=["current_state"])

        response = self._dashboard()

        # "In Progress" is half way to "Closed" on the resolution machine.
        self.assertContains(response, 'aria-valuenow="50"')
        self.assertContains(response, "In Progress · 50%")
        # The dashboard gets the thin bar, not the detail page's ring.
        self.assertContains(response, 'class="progress-bar-thin"')
        self.assertNotContains(response, "progress-ring")

    def test_state_graph_is_read_once_per_workflow_type(self):
        """
        One machine per workflow type is shared by every row, so a row's progress
        costs no graph read of its own.
        """
        with mock.patch("pwms.views.machine_for", wraps=machine_for) as build:
            self._dashboard()
        baseline = build.call_count

        report_type = WorkflowType.objects.get(name="Delegation Report")
        DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Another overdue report",
            owner=self.viewer,
            deadline=timezone.now() - timedelta(days=2),
        )
        with mock.patch("pwms.views.machine_for", wraps=machine_for) as build:
            self._dashboard()

        self.assertGreater(baseline, 0)
        self.assertEqual(build.call_count, baseline)

    def test_awaiting_referral_shows_for_my_committee(self):
        response = self._dashboard()
        self.assertContains(response, self.committee.name)

    def test_referral_to_another_committee_stays_hidden(self):
        other = Group.objects.create(name="Unrelated Board XY")
        WorkflowReferral.objects.create(
            content_type=self.report_ct,
            object_id=self.due_report.pk,
            referred_to=other,
            due_date=timezone.now() + timedelta(days=1),
        )
        response = self._dashboard()
        self.assertNotContains(response, other.name)

    def test_recent_activity_lists_visible_state_changes(self):
        response = self._dashboard()
        self.assertContains(response, self.activity_actor.display_name)

    def test_activity_on_invisible_work_stays_hidden(self):
        ghost = get_user_model().objects.create_user(
            username="ghost-actor",
            first_name="Ghost",
            last_name="Actor",
        )
        TransitionLog.objects.create(
            content_type=self.report_ct,
            object_id=self.stranger_report.pk,
            action="STATE_TRANSITION",
            actor=ghost,
        )
        response = self._dashboard()
        self.assertNotContains(response, "Ghost Actor")

    def test_empty_state_for_a_user_with_no_work(self):
        loner = get_user_model().objects.create_user(
            username="dash-loner", password="pw"
        )
        self.client.force_login(loner)
        response = self.client.get(reverse("pwms:dashboard"))
        self.assertContains(response, "Nothing on your plate yet")


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
            username="deadline-member", email="member@example.com", password="pw"
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

        # One message per recipient: each delivery is logged on its own
        # notification row, and the committee is not shown the owner's address.
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            sorted(message.to[0] for message in mail.outbox),
            ["member@example.com", "owner@example.com"],
        )
        self.assertTrue(
            all("Referred report" in message.subject for message in mail.outbox)
        )
        referral.refresh_from_db()
        self.assertIsNotNone(referral.deadline_notified_at)

    def test_reminder_is_not_repeated(self):
        self._referral(due_in=timedelta(hours=2))

        self.run_command()
        self.run_command()

        # One reminder run, one message per recipient; the second run is silent.
        self.assertEqual(len(mail.outbox), 2)

    def test_second_reminder_fires_inside_the_last_hour(self):
        self._referral(
            due_in=timedelta(minutes=30),
            notified_at=timezone.now() - timedelta(hours=2),
        )

        self.run_command()

        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(
            all("Referred report" in message.subject for message in mail.outbox)
        )

    def test_passed_deadline_is_marked_expired(self):
        referral = self._referral(due_in=timedelta(hours=-2))

        self.run_command()

        referral.refresh_from_db()
        self.assertEqual(referral.status, "expired")
        # mark_expired() raises the notice itself, so exactly one per recipient.
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(all("Expired" in message.subject for message in mail.outbox))

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


class InformationalPageTests(TestCase):
    """The About and Contact pages describe the system, not placeholder copy."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="pages-user", password="pw"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_pages_require_login(self):
        self.client.logout()
        for name in ("about", "contact"):
            response = self.client.get(reverse(f"pwms:{name}"))
            self.assertEqual(response.status_code, 302)
            self.assertIn("/pwms/login/", response["Location"])

    def test_about_describes_the_system(self):
        response = self.client.get(reverse("pwms:about"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Parliament Workflow Management System")
        # The instruments the system actually tracks.
        for label in (
            "Delegation reports",
            "International resolutions",
            "International agreements",
            "Bills",
        ):
            self.assertContains(response, label)

    def test_contact_renders_its_own_page(self):
        """The contact route must not fall back to the about page."""
        response = self.client.get(reverse("pwms:contact"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Before you report a problem")
        self.assertNotContains(response, "Why PWMS exists")


class HomePageTests(TestCase):
    """The home page is public, and the landing page after sign-in."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="home-user", password="pw"
        )

    def test_home_is_reachable_without_signing_in(self):
        """The one public page: nothing behind it, but nothing in front of it."""
        response = self.client.get(reverse("pwms:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Welcome to PWMS")
        self.assertContains(response, reverse("pwms:login"))

    def test_home_is_the_post_login_landing_page(self):
        """Sign-in redirects here, so the route cannot simply be dropped."""
        self.assertEqual(settings.LOGIN_REDIRECT_URL, reverse("pwms:home"))

    def test_signed_in_users_get_a_launchpad(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("pwms:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Welcome back")
        for name in ("dashboard", "workflows", "my_groups"):
            self.assertContains(response, reverse(f"pwms:{name}"))

    def test_page_titles_reach_the_document(self):
        """base.html's title block renders (it was hardcoded before)."""
        response = self.client.get(reverse("pwms:home"))
        self.assertContains(response, "<title>Home</title>")

        self.client.force_login(self.user)
        response = self.client.get(reverse("pwms:about"))
        self.assertContains(response, "<title>About</title>")


class GroupListFilterTests(TestCase):
    """The Name and Type filters above the all-groups list."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="group-filter", password="pw"
        )
        cls.committee = Group.objects.create(
            name="Zzz Filter Portfolio Committee", group_type="portfolio_committee"
        )
        cls.ministry = Group.objects.create(
            name="Zzz Filter Ministry", short_name="ZFM", group_type="ministry"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_name_filter_narrows_the_table(self):
        response = self.client.get(
            reverse("pwms:all_groups"), {"q": "Zzz Filter Ministry"}
        )
        self.assertContains(response, self.ministry.name)
        self.assertNotContains(response, self.committee.name)

    def test_name_filter_matches_the_short_name(self):
        response = self.client.get(reverse("pwms:all_groups"), {"q": "zfm"})
        self.assertContains(response, self.ministry.name)
        self.assertNotContains(response, self.committee.name)

    def test_type_filter_narrows_the_table(self):
        response = self.client.get(
            reverse("pwms:all_groups"), {"type": "portfolio_committee"}
        )
        self.assertContains(response, self.committee.name)
        self.assertNotContains(response, self.ministry.name)

    def test_filters_combine(self):
        response = self.client.get(
            reverse("pwms:all_groups"), {"q": "Zzz Filter", "type": "ministry"}
        )
        self.assertContains(response, self.ministry.name)
        self.assertNotContains(response, self.committee.name)

    def test_type_options_cover_exactly_the_types_in_use(self):
        response = self.client.get(reverse("pwms:all_groups"))
        offered = {value for value, _ in response.context["type_choices"]}
        in_use = set(Group.objects.values_list("group_type", flat=True))
        self.assertEqual(offered, in_use)

    def test_no_match_message_names_the_filters(self):
        response = self.client.get(reverse("pwms:all_groups"), {"q": "nothing matches"})
        self.assertContains(response, "No groups match your filters.")
        self.assertNotContains(response, "No groups found.")
