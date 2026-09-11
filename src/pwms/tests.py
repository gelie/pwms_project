from datetime import date, timedelta

from auditlog.context import set_actor
from auditlog.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    DelegationParticipant,
    DelegationReport,
    EventType,
    Group,
    GroupMembership,
    InternationalResolution,
    Role,
    State,
    Transition,
    TransitionCondition,
    TransitionLog,
    WorkflowEvent,
    WorkflowGroupAccess,
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
    VIEW,
    permissions_for,
    require,
    resolve,
)


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
            "location_city": "Geneva",
            "location_country": "Switzerland",
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

    def test_assigned_official_email_is_exposed(self):
        report = self._report()
        self.assertEqual(report.assigned_to_email, "irpd@example.com")


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
        for workflow_type in (cls.report_type, cls.resolution_type):
            workflow_type.group = cls.group
            workflow_type.save(update_fields=["group"])
            workflow_type.create_roles.add(cls.creator_role)

    def setUp(self):
        self.client.force_login(self.user)

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

    def test_pages_require_login(self):
        self.client.logout()
        for name in ("delegation_reports", "international_resolutions"):
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
            {
                "title": "CRUD report (edited)",
                "description": "",
                "current_state": report.current_state.pk,
                "owner": self.user.pk,
                "assigned_to": "",
                "deadline": "",
                "priority": "high",
                "engagement_name": "",
                "engagement_start_date": "",
                "engagement_end_date": "",
                "location_city": "",
                "location_country": "",
                "notes": "",
                "report_document_url": "",
            },
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
        for name in ("workflows", "delegation_reports", "international_resolutions"):
            self.assertContains(response, reverse(f"pwms:{name}"))

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
