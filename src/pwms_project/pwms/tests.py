from auditlog.context import set_actor
from auditlog.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from .models import (
    InternationalResolution,
    State,
    Transition,
    TransitionLog,
    WorkflowType,
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
        cls.wt = WorkflowType.objects.create(name="International Resolution")
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

    def test_referral_group_m2m_change_is_audited(self):
        from .models import Group

        group = Group.objects.create(
            name="Portfolio Committee A", group_type="portfolio_committee"
        )
        res = self._make_resolution(number="IR-3", title="Third resolution")

        with set_actor(self.user):
            res.referred_to_groups.add(group)

        # auditlog records M2M adds under the field key, e.g.
        # {"referred_to_groups": {"type": "m2m", "objects": [...], "operation": "add"}}
        m2m = self._logs_for(res.pk).filter(
            action=LogEntry.Action.UPDATE, changes__has_key="referred_to_groups"
        )
        self.assertTrue(m2m.exists())
