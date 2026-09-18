"""
Alerts: who is told, what is recorded, and what actually goes out.

The dispatch layer has three jobs, and each gets a group of tests here:

* **audience** — the officers who run the workflow type, anyone a transition
  names, and the people the record names, minus the actor and minus anyone who
  may not open the record (``stakeholders``);
* **record** — every dispatch writes an ``in_app`` row (the bell) and an
  ``email`` row (the delivery log), and the email row ends up ``sent`` or
  ``failed`` with the reason kept;
* **delivery** — sending is *queued*, not done inline: the task is written in
  the same transaction as the alert row, one message goes out per recipient, and
  a transient failure is retried before the row is finally marked ``failed``.

A ``TestCase`` never commits, and a queued task needs a worker, so the tests that
assert on ``mail.outbox`` call ``run_queued_emails()`` — the real background task
run synchronously, which is what ``manage.py process_tasks`` does in production.
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from background_task.models import Task
from background_task.tasks import tasks as bg_tasks
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Bill,
    DelegationReport,
    Group,
    GroupMembership,
    Notification,
    Role,
    State,
    Transition,
    User,
    WorkflowGroupAccess,
    WorkflowReferral,
    WorkflowRolePermission,
    WorkflowType,
)
from .notifications import (
    max_attempts,
    notify_referral_created,
    notify_workflow_created,
    referral_audience,
    stakeholders,
)

#: The name django-background-tasks registers for the alert-email job.
EMAIL_TASK_NAME = "pwms.tasks.deliver_notification_email"


def queued_email_tasks():
    """The alert-email tasks waiting in the queue."""
    return Task.objects.filter(task_name=EMAIL_TASK_NAME)


def run_queued_emails():
    """
    Run every queued alert-email task now.

    The real task, run synchronously (``BACKGROUND_TASK_RUN_ASYNC`` off) rather
    than through the thread pool, so a test can assert on what the worker did —
    retries and terminal failures included.
    """
    with override_settings(BACKGROUND_TASK_RUN_ASYNC=False):
        for task in list(queued_email_tasks()):
            bg_tasks.run_task(task)


class AlertFixture(TestCase):
    """A workflow type owned by a group, with two officers and one outsider."""

    @classmethod
    def setUpTestData(cls):
        cls.officer = User.objects.create_user(
            username="officer", email="officer@example.com", password="pw"
        )
        cls.second_officer = User.objects.create_user(
            username="second", email="second@example.com", password="pw"
        )
        cls.denied = User.objects.create_user(
            username="denied", email="denied@example.com", password="pw"
        )
        cls.author = User.objects.create_user(
            username="author", email="author@example.com", password="pw"
        )

        cls.officer_role = Role.objects.create(name="Alert Officer")
        cls.denied_role = Role.objects.create(name="Alert Denied")
        cls.office = Group.objects.create(name="Alert Office", group_type="division")
        GroupMembership.objects.create(
            user=cls.officer, group=cls.office, role=cls.officer_role
        )
        GroupMembership.objects.create(
            user=cls.second_officer, group=cls.office, role=cls.officer_role
        )
        # Holds a create role, but the group access below denies them view.
        GroupMembership.objects.create(
            user=cls.denied, group=cls.office, role=cls.denied_role
        )

        cls.workflow_type = WorkflowType.objects.create(
            name="Alert Test Report", group=cls.office
        )
        cls.workflow_type.create_roles.add(cls.officer_role, cls.denied_role)

        cls.draft = State.objects.create(
            workflow_type=cls.workflow_type, name="Drafting", is_initial=True
        )
        cls.review = State.objects.create(
            workflow_type=cls.workflow_type, name="Under review"
        )
        cls.submit = Transition.objects.create(
            workflow_type=cls.workflow_type,
            name="Submit",
            from_state=cls.draft,
            to_state=cls.review,
            requires_comment=False,
        )

    def setUp(self):
        self.report = DelegationReport.objects.create(
            workflow_type=self.workflow_type,
            current_state=self.draft,
            title="Alerted report",
            owner=self.author,
        )
        # materialize_group_access() ran on save; deny the denied_role view on it
        # so the RBAC filter has something real to reject.
        access = self.report.group_accesses().get(group=self.office)
        WorkflowRolePermission.objects.create(
            group_access=access, role=self.denied_role, can_view=False
        )
        Notification.objects.all().delete()


class StakeholderTests(AlertFixture):
    """``stakeholders()`` — the audience rules."""

    def test_officers_of_the_type_are_the_audience(self):
        audience = stakeholders(self.report)

        # The two officers holding a create role, plus the owner the record names.
        self.assertCountEqual(
            audience, [self.officer, self.second_officer, self.author]
        )

    def test_actor_is_not_told_about_their_own_action(self):
        """The author created it and owns it; nobody needs telling what they did."""
        rows = notify_workflow_created(self.report, actor=self.author)

        self.assertNotIn(self.author, [row.recipient for row in rows])

    def test_a_holder_who_may_not_view_the_record_is_skipped(self):
        """
        The denied role is a create role, so ``denied`` is a candidate, but the
        group access denies it view — an alert would link to a 403.
        """
        audience = stakeholders(self.report)

        self.assertNotIn(self.denied, audience)

    def test_inactive_memberships_do_not_receive_alerts(self):
        GroupMembership.objects.filter(
            user=self.second_officer, group=self.office
        ).update(is_active=False)

        self.assertCountEqual(stakeholders(self.report), [self.officer, self.author])

    def test_owner_is_told_without_holding_a_role(self):
        """The record names them, so they hear about it with no membership at all."""
        self.assertIn(self.author, stakeholders(self.report))


class DispatchRecordTests(AlertFixture):
    """What a dispatch writes down."""

    def test_each_recipient_gets_an_in_app_and_an_email_row(self):
        notify_workflow_created(self.report, actor=self.author)

        for recipient in (self.officer, self.second_officer):
            rows = Notification.objects.filter(recipient=recipient)
            self.assertEqual(
                sorted(rows.values_list("channel", flat=True)), ["email", "in_app"]
            )

    def test_records_the_target_subject_and_context(self):
        notify_workflow_created(self.report, actor=self.author)

        row = Notification.objects.get(recipient=self.officer, channel="in_app")
        self.assertIn("Alerted report", row.subject)
        self.assertEqual(row.kind, "workflow-created")
        self.assertEqual(row.actor, self.author)
        self.assertEqual(row.content_object, self.report)
        self.assertEqual(row.context["state"], "Drafting")
        self.assertEqual(row.url, self.report.get_absolute_url())
        self.assertTrue(row.is_unread)
        self.assertEqual(row.status, "sent")

    def test_recipient_without_an_email_still_gets_the_bell(self):
        User.objects.filter(pk=self.officer.pk).update(email="")

        notify_workflow_created(self.report, actor=self.author)

        self.assertTrue(
            Notification.objects.filter(
                recipient=self.officer, channel="in_app"
            ).exists()
        )
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.officer, channel="email"
            ).exists()
        )
        # No address, no delivery task: only the other officer is queued.
        self.assertEqual(queued_email_tasks().count(), 1)

    def test_transition_alert_quotes_both_states(self):
        self.report.perform_transition(
            self.submit, actor=self.officer, comment="ready for review"
        )

        row = Notification.objects.get(
            recipient=self.second_officer, kind="workflow-transition", channel="in_app"
        )
        self.assertIn("Drafting", row.subject)
        self.assertIn("Under review", row.subject)
        self.assertIn("ready for review", row.body)

    def test_transition_does_not_tell_the_actor(self):
        self.report.perform_transition(self.submit, actor=self.officer, comment="go")

        self.assertFalse(
            Notification.objects.filter(
                recipient=self.officer, kind="workflow-transition"
            ).exists()
        )


class EmailDeliveryTests(AlertFixture):
    """The email half: queued, one message each, retried, then recorded."""

    def test_addressed_from_the_configured_sender_to_the_recipient(self):
        """Sender comes from DEFAULT_FROM_EMAIL; each message names one recipient."""
        notify_workflow_created(self.report, actor=self.author)
        run_queued_emails()

        message = next(m for m in mail.outbox if m.to == [self.officer.email])
        self.assertEqual(message.from_email, settings.DEFAULT_FROM_EMAIL)
        self.assertEqual(message.to, [self.officer.email])

    def test_one_message_per_recipient(self):
        notify_workflow_created(self.report, actor=self.author)
        run_queued_emails()

        self.assertEqual(
            sorted(message.to[0] for message in mail.outbox),
            ["officer@example.com", "second@example.com"],
        )
        self.assertEqual(
            Notification.objects.filter(channel="email", status="sent").count(), 2
        )

    def test_mail_is_queued_rather_than_sent_inline(self):
        """
        Dispatching only queues; a worker is what actually sends.

        That is what makes a crash after COMMIT survivable: the task is a row in
        the database, not a callback that was about to run and then did not.
        """
        notify_workflow_created(self.report, actor=self.author)

        self.assertEqual(mail.outbox, [])
        self.assertEqual(
            Notification.objects.filter(channel="email", status="pending").count(), 2
        )
        self.assertEqual(queued_email_tasks().count(), 2)

        run_queued_emails()

        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            Notification.objects.filter(channel="email", status="sent").count(), 2
        )
        self.assertEqual(queued_email_tasks().count(), 0)

    def test_a_rolled_back_change_queues_nothing(self):
        """The task commits with the alert row, so a rollback leaves nothing behind."""
        with self.assertRaises(RuntimeError), transaction.atomic():
            notify_workflow_created(self.report, actor=self.author)
            raise RuntimeError("the workflow change failed")

        self.assertEqual(Notification.objects.filter(channel="email").count(), 0)
        self.assertEqual(queued_email_tasks().count(), 0)
        self.assertEqual(mail.outbox, [])

    def test_message_carries_the_recipient_the_link_and_the_log_line(self):
        notify_workflow_created(self.report, actor=self.author)
        run_queued_emails()

        message = next(m for m in mail.outbox if m.to == ["officer@example.com"])
        self.assertIn("officer", message.body)
        self.assertIn(self.report.get_absolute_url(), message.body)
        self.assertIn("Workflow created", message.body)

    def test_a_failed_send_is_retried_and_the_reason_kept(self):
        with mock.patch(
            "pwms.notifications.dispatch.send_mail",
            side_effect=OSError("smtp is down"),
        ):
            # Dispatching never raises, whatever the mail server does...
            notify_workflow_created(self.report, actor=self.author)
            run_queued_emails()  # ...and one failed attempt is not the end.

        rows = Notification.objects.filter(channel="email")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(set(rows.values_list("status", flat=True)), {"pending"})
        self.assertEqual(set(rows.values_list("attempts", flat=True)), {1})
        self.assertIn("smtp is down", rows.first().error)
        # Still queued for another go, and the in-app half landed regardless.
        self.assertEqual(queued_email_tasks().count(), 2)
        self.assertEqual(
            Notification.objects.filter(channel="in_app", status="sent").count(), 2
        )

    def test_the_row_is_failed_once_the_attempts_run_out(self):
        with mock.patch(
            "pwms.notifications.dispatch.send_mail",
            side_effect=OSError("smtp is down"),
        ):
            notify_workflow_created(self.report, actor=self.author)
            for _attempt in range(max_attempts()):
                run_queued_emails()

        rows = Notification.objects.filter(channel="email")
        self.assertEqual(set(rows.values_list("status", flat=True)), {"failed"})
        self.assertEqual(set(rows.values_list("attempts", flat=True)), {max_attempts()})
        self.assertIn("smtp is down", rows.first().error)
        # Nothing is left to retry, so the queue is empty.
        self.assertEqual(queued_email_tasks().count(), 0)

    def test_delivery_is_idempotent(self):
        from .notifications import deliver_email

        notify_workflow_created(self.report, actor=self.author)
        run_queued_emails()

        sent = Notification.objects.filter(channel="email").first()
        before = len(mail.outbox)
        deliver_email(sent.pk)  # already sent; must not send again

        self.assertEqual(len(mail.outbox), before)


class ReferralAlertTests(AlertFixture):
    """Referral alerts: the committee is told, and its own acts come back."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.committee = Group.objects.create(
            name="Alert Committee", group_type="portfolio_committee"
        )
        cls.committee_role = Role.objects.create(name="Alert Committee Member")
        cls.member = User.objects.create_user(
            username="member", email="member@example.com", password="pw"
        )
        GroupMembership.objects.create(
            user=cls.member, group=cls.committee, role=cls.committee_role
        )

    def test_referring_tells_the_committee_and_the_owner(self):
        notify_referral_created(self._referred(), actor=self.officer)

        recipients = Notification.objects.filter(kind="referral-created").values_list(
            "recipient__username", flat=True
        )
        self.assertCountEqual(set(recipients), {"member", "author"})

    def test_committee_member_is_told_without_an_access_row(self):
        """
        The referral is the entitlement: its audience is the committee's members,
        not whoever holds an access row on the record. The grant the referral
        materialises is not what puts them on this list, which is why this
        audience is not RBAC-filtered like the role-driven one.
        """
        referral = self._referred()
        # The referral does grant access (see tests_referral_access), so remove it
        # to show the audience does not depend on it.
        WorkflowGroupAccess.objects.filter(
            group=self.committee,
            content_type=ContentType.objects.get_for_model(DelegationReport),
            object_id=self.report.pk,
        ).delete()

        self.assertIn(self.member, referral_audience(referral))

    def test_assignee_hears_about_referral_activity(self):
        """
        Whoever owes the next action is told about a referral too - the same
        owner/assignee pair a creation or transition alert reaches.
        """
        assignee = User.objects.create_user(
            username="assignee", email="assignee@example.com", password="pw"
        )
        # No membership anywhere, so only the assigned_to half can reach them.
        self.report.assigned_to = assignee
        self.report.save(update_fields=["assigned_to"])
        referral = self._referred()

        referral.respond(responded_by=self.member, notes="Noted, no objection.")

        rows = Notification.objects.filter(
            recipient=assignee, kind="referral-responded"
        )
        self.assertEqual(
            sorted(rows.values_list("channel", flat=True)), ["email", "in_app"]
        )

    def test_answering_tells_the_parties(self):
        referral = self._referred()

        referral.respond(responded_by=self.member, notes="Noted, no objection.")

        row = Notification.objects.get(
            recipient=self.author, kind="referral-responded", channel="in_app"
        )
        self.assertIn("Answered", row.subject)
        self.assertIn("no objection", row.body)

    def test_expiring_tells_the_parties(self):
        referral = self._referred(due_date=timezone.now() - timedelta(hours=1))

        referral.mark_expired()

        row = Notification.objects.get(
            recipient=self.member, kind="referral-expired", channel="in_app"
        )
        self.assertIn("Expired", row.subject)

    def _referred(self, due_date=None):
        return WorkflowReferral.objects.create(
            content_type=ContentType.objects.get_for_model(DelegationReport),
            object_id=self.report.pk,
            referred_to=self.committee,
            referred_by=self.officer,
            due_date=due_date,
        )


class NotificationModelTests(TestCase):
    """The row's own state machine (it is the delivery log)."""

    def test_in_app_alert_read_state(self):
        user = User.objects.create_user(username="state", email="state@example.com")
        row = Notification.objects.create(
            recipient=user, channel="in_app", kind="workflow-created", subject="s"
        )

        self.assertTrue(row.is_unread)

        row.mark_read()
        self.assertIsNotNone(row.read_at)
        self.assertFalse(row.is_unread)
        read_at = row.read_at
        row.mark_read()  # idempotent
        self.assertEqual(row.read_at, read_at)

    def test_email_row_delivery_state(self):
        user = User.objects.create_user(username="state", email="state@example.com")
        row = Notification.objects.create(
            recipient=user, channel="email", kind="workflow-created", subject="s"
        )

        self.assertEqual(row.status, "pending")
        # A delivery record is never "unread", however long it sits there.
        self.assertFalse(row.is_unread)

        row.mark_sent()
        self.assertEqual(row.status, "sent")
        self.assertIsNotNone(row.sent_at)

    def test_failure_keeps_the_reason(self):
        user = User.objects.create_user(username="state2", email="state2@example.com")
        row = Notification.objects.create(
            recipient=user, channel="email", kind="workflow-created", subject="s"
        )

        row.mark_failed(ValueError("refused"))

        row.refresh_from_db()
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.error, "refused")


class NotificationViewTests(AlertFixture):
    """The bell, the alerts page and the read state."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.officer)
        notify_workflow_created(self.report, actor=self.author)

    def test_authentication_is_required(self):
        self.client.logout()

        response = self.client.get(reverse("pwms:notifications"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/pwms/login/", response["Location"])

    def test_bell_counts_unread_alerts_in_the_site_chrome(self):
        response = self.client.get(reverse("pwms:home"))

        self.assertEqual(response.context["unread_alert_count"], 1)
        self.assertEqual(len(response.context["recent_alerts"]), 1)
        self.assertContains(response, "alerts-badge")

    def test_alerts_page_lists_the_alert(self):
        response = self.client.get(reverse("pwms:notifications"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alerted report")
        self.assertContains(response, "Workflow created")

    def test_opening_an_alert_marks_it_read_and_follows_the_link(self):
        row = Notification.objects.get(recipient=self.officer, channel="in_app")

        response = self.client.get(
            reverse("pwms:notification_open", kwargs={"public_id": row.public_id})
        )

        self.assertRedirects(
            response, self.report.get_absolute_url(), fetch_redirect_response=False
        )
        row.refresh_from_db()
        self.assertIsNotNone(row.read_at)

    def test_alerts_are_not_visible_to_another_user(self):
        stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        row = Notification.objects.get(recipient=self.officer, channel="in_app")
        self.client.force_login(stranger)

        response = self.client.get(
            reverse("pwms:notification_open", kwargs={"public_id": row.public_id})
        )

        self.assertEqual(response.status_code, 404)

    def test_mark_all_read_clears_the_count(self):
        response = self.client.post(reverse("pwms:notifications_read_all"))

        self.assertRedirects(response, reverse("pwms:notifications"))
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.officer, channel="in_app", read_at__isnull=True
            ).exists()
        )

    def test_mark_all_read_rejects_get(self):
        response = self.client.get(reverse("pwms:notifications_read_all"))

        self.assertEqual(response.status_code, 405)


class CreatedNotificationFromViewTests(AlertFixture):
    """Creating a workflow through the web UI raises the alert.

    The form is mocked: the CRUD tests already cover it, and what matters here is
    the wiring — a successful create notifies the type's officers, handing the
    service the saved instance and the signed-in actor.
    """

    def test_creating_a_bill_notifies_the_office(self):
        # Passing the view's create-scope check needs one of the type's create
        # roles in the type's group.
        GroupMembership.objects.create(
            user=self.author, group=self.office, role=self.officer_role
        )
        self.client.force_login(self.author)
        bill = Bill.objects.create(
            workflow_type=self.workflow_type,
            current_state=self.draft,
            bill_number="B 1—2026",
            title="Alerted bill",
            owner=self.author,
        )

        with (
            mock.patch("pwms.views.BillForm") as form_class,
            mock.patch("pwms.views.notify_workflow_created") as notify,
        ):
            form_class.return_value.is_valid.return_value = True
            form_class.return_value.save.return_value = bill
            response = self.client.post(
                reverse("pwms:bill_create"),
                {"workflow_type": self.workflow_type.pk},
            )

        self.assertEqual(response.status_code, 302)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], bill)
        self.assertEqual(notify.call_args.kwargs["actor"], self.author)


class ReferralDeadlineLogTests(AlertFixture):
    """The scheduled command writes to the same log as every other alert."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.committee = Group.objects.create(
            name="Deadline Alert Committee", group_type="portfolio_committee"
        )
        cls.member = User.objects.create_user(
            username="deadline-alert-member", email="dl@example.com", password="pw"
        )
        GroupMembership.objects.create(
            user=cls.member,
            group=cls.committee,
            role=Role.objects.create(name="Deadline Alert Member"),
        )

    def test_reminder_is_recorded_for_each_recipient(self):
        self._referral(due_in=timedelta(hours=2))

        call_command("check_referral_deadlines", stdout=StringIO())
        run_queued_emails()

        rows = Notification.objects.filter(kind="referral-deadline", channel="email")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(set(rows.values_list("status", flat=True)), {"sent"})

    def test_dry_run_records_nothing(self):
        self._referral(due_in=timedelta(hours=2))

        call_command("check_referral_deadlines", "--dry-run", stdout=StringIO())

        self.assertEqual(Notification.objects.count(), 0)

    def test_expiry_is_alerted_once_by_the_model(self):
        """``mark_expired()`` raises the notice, so the command must not also send."""
        self._referral(due_in=timedelta(hours=-2))

        call_command("check_referral_deadlines", stdout=StringIO())
        run_queued_emails()

        rows = Notification.objects.filter(kind="referral-expired", channel="email")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(len(mail.outbox), 2)

    def _referral(self, *, due_in):
        return WorkflowReferral.objects.create(
            content_type=ContentType.objects.get_for_model(DelegationReport),
            object_id=self.report.pk,
            referred_to=self.committee,
            referred_by=self.author,
            due_date=timezone.now() + due_in,
        )
