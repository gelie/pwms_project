from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from ..models import (
    DelegationReport,
    EventType,
    Group,
    InternationalResolution,
    Transition,
    TransitionCondition,
    TransitionLog,
    WorkflowEvent,
    WorkflowType,
)
from .base import make_state


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
        state = make_state(
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
        cls.open_state = make_state(workflow_type=cls.wt, name="Open", is_initial=True)
        cls.done_state = make_state(workflow_type=cls.wt, name="Done", is_terminal=True)
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
