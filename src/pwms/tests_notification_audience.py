"""
Alert audiences: who is told, why, over which channel, and how they can opt out.

``tests_notifications`` covers what a dispatch *records and delivers*. This module
covers the decision before it — who is in the audience:

* **reasons** — every row says why the person was told (``next-actor``,
  ``assignee``, ``owner``, ``officer``, ``rule:<id>`` ...), and each reason has
  its own channels;
* **oversight** — the officers who run a type are told at the type's
  ``oversight_channel`` (email only by default), not through a bell count for
  every record;
* **scope** — an event is resolved in the groups it concerns (the custodian, a
  referral's committee, a meeting's host), never in "every group that can see
  the record" unless a rule opts into ``instance_groups``;
* **rules** — ``NotificationRule`` declares extra recipients per type/event/
  transition, with a scope, a role and a channel;
* **preferences** — a user can mute a kind per channel, but never an alert they
  owe an action on.
"""

from datetime import timedelta
from io import StringIO

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    DelegationReport,
    Group,
    GroupMembership,
    MeetingAttendance,
    Notification,
    NotificationPreference,
    NotificationRule,
    PublicStatus,
    Role,
    State,
    Transition,
    User,
    WorkflowGroupAccess,
    WorkflowReferral,
    WorkflowType,
)
from .models.notifications import EVENT_FOR_KIND, MANDATORY_KINDS, SHARE_KINDS
from .notifications import (
    Audience,
    notify_meeting_created,
    notify_referral_created,
    notify_workflow_created,
    preference_rows,
    referral_audience,
    save_preferences,
    stakeholders,
)
from .tests_meetings import MeetingFixture


def make_state(**kwargs):
    kwargs.setdefault("public_status", PublicStatus.objects.get(name="New"))
    return State.objects.create(**kwargs)


def make_user(username, **kwargs):
    kwargs.setdefault("is_staff_member", True)
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw", **kwargs
    )


class AudienceFixture(TestCase):
    """
    A type owned by ``office`` and a committee that is not the custodian.

    ``clerk_role`` is held in *both* groups — roles are shared across the
    organisation, which is exactly why a rule has to say where it applies.
    """

    @classmethod
    def setUpTestData(cls):
        cls.officer_role = Role.objects.create(name="Aud Officer")
        cls.clerk_role = Role.objects.create(name="Aud Clerk")
        cls.office = Group.objects.create(name="Aud Office", group_type="division")
        cls.committee = Group.objects.create(
            name="Aud Committee", group_type="portfolio_committee"
        )

        cls.officer = make_user("aud-officer")
        cls.clerk = make_user("aud-clerk")
        cls.mp = make_user(
            "aud-mp", is_mp=True, employee_type="member", is_staff_member=False
        )
        cls.committee_member = make_user("aud-committee")
        cls.author = make_user("aud-author")
        cls.actor = make_user("aud-actor")

        for user, group, role in (
            (cls.officer, cls.office, cls.officer_role),
            (cls.clerk, cls.office, cls.clerk_role),
            (cls.mp, cls.office, cls.clerk_role),
            (cls.committee_member, cls.committee, cls.clerk_role),
        ):
            GroupMembership.objects.create(user=user, group=group, role=role)

        cls.workflow_type = WorkflowType.objects.create(
            name="Aud Report", group=cls.office
        )
        cls.workflow_type.create_roles.add(cls.officer_role)
        cls.draft = make_state(
            workflow_type=cls.workflow_type, name="Drafting", is_initial=True
        )
        cls.review = make_state(workflow_type=cls.workflow_type, name="Review")
        cls.approved = make_state(workflow_type=cls.workflow_type, name="Approved")
        cls.submit = Transition.objects.create(
            workflow_type=cls.workflow_type,
            name="Submit",
            from_state=cls.draft,
            to_state=cls.review,
            requires_comment=False,
        )
        cls.approve = Transition.objects.create(
            workflow_type=cls.workflow_type,
            name="Approve",
            from_state=cls.review,
            to_state=cls.approved,
            requires_comment=False,
        )
        cls.approve.allowed_roles.add(cls.clerk_role)

    def setUp(self):
        self.report = DelegationReport.objects.create(
            workflow_type=self.workflow_type,
            current_state=self.draft,
            title="Audience report",
            owner=self.author,
        )
        Notification.objects.all().delete()

    # -- helpers ----------------------------------------------------------
    def rows(self, kind=None, channel=None, **extra):
        rows = Notification.objects.filter(**extra)
        if kind:
            rows = rows.filter(kind=kind)
        if channel:
            rows = rows.filter(channel=channel)
        return rows

    def told(self, kind, channel):
        """Usernames with a row of ``kind`` on ``channel``."""
        return set(
            self.rows(kind, channel).values_list("recipient__username", flat=True)
        )

    def referral(self, **kwargs):
        kwargs.setdefault("referred_by", self.officer)
        return WorkflowReferral.objects.create(
            content_type=ContentType.objects.get_for_model(DelegationReport),
            object_id=self.report.pk,
            referred_to=self.committee,
            **kwargs,
        )

    def rule(self, **kwargs):
        kwargs.setdefault("workflow_type", self.workflow_type)
        return NotificationRule.objects.create(**kwargs)

    def move_to_review(self, actor=None):
        self.report.perform_transition(
            self.submit, actor=actor or self.actor, comment="go"
        )


class OversightTests(AudienceFixture):
    """The officers are the oversight tier: email by default, never a bell count."""

    def test_officers_are_emailed_but_get_no_bell_by_default(self):
        notify_workflow_created(self.report, actor=self.actor)

        self.assertEqual(self.told("workflow-created", "email"), {"aud-officer"})
        self.assertNotIn("aud-officer", self.told("workflow-created", "in_app"))

    def test_a_transition_does_not_ring_the_officers_bell_either(self):
        self.move_to_review()

        self.assertIn("aud-officer", self.told("workflow-transition", "email"))
        self.assertNotIn("aud-officer", self.told("workflow-transition", "in_app"))

    def test_bell_and_email(self):
        self.workflow_type.oversight_channel = WorkflowType.OVERSIGHT_BOTH
        self.workflow_type.save()

        notify_workflow_created(self.report, actor=self.actor)

        self.assertIn("aud-officer", self.told("workflow-created", "email"))
        self.assertIn("aud-officer", self.told("workflow-created", "in_app"))

    def test_bell_only(self):
        self.workflow_type.oversight_channel = WorkflowType.OVERSIGHT_IN_APP
        self.workflow_type.save()

        notify_workflow_created(self.report, actor=self.actor)

        self.assertNotIn("aud-officer", self.told("workflow-created", "email"))
        self.assertIn("aud-officer", self.told("workflow-created", "in_app"))

    def test_off_tells_the_officers_nothing(self):
        self.workflow_type.oversight_channel = WorkflowType.OVERSIGHT_OFF
        self.workflow_type.save()

        notify_workflow_created(self.report, actor=self.actor)

        self.assertFalse(self.rows(recipient=self.officer).exists())

    def test_officers_are_resolved_in_the_custodian_group_only(self):
        """A create-role holder in some other group is not an officer of this type."""
        GroupMembership.objects.create(
            user=self.committee_member, group=self.committee, role=self.officer_role
        )

        notify_workflow_created(self.report, actor=self.actor)

        self.assertNotIn("aud-committee", self.told("workflow-created", "email"))


class ReasonTests(AudienceFixture):
    """Every row records why the person was told."""

    def test_the_owner_is_told_as_the_owner(self):
        notify_workflow_created(self.report, actor=self.actor)

        row = self.rows(recipient=self.author, channel="in_app").get()
        self.assertEqual(row.reasons, ["owner"])
        # The owner is not a member of the custodian group, so: bell, no mail.
        self.assertFalse(self.rows(recipient=self.author, channel="email").exists())

    def test_the_assignee_is_told_as_the_assignee(self):
        self.report.assigned_to = self.clerk
        self.report.save(update_fields=["assigned_to"])

        notify_workflow_created(self.report, actor=self.actor)

        row = self.rows(recipient=self.clerk, channel="in_app").get()
        self.assertEqual(row.reasons, ["assignee"])
        # ...and, being staff in the custodian group, is mailed too.
        self.assertTrue(self.rows(recipient=self.clerk, channel="email").exists())

    def test_the_email_row_carries_the_same_reasons(self):
        notify_workflow_created(self.report, actor=self.actor)

        row = self.rows(recipient=self.officer, channel="email").get()
        self.assertEqual(row.reasons, ["officer"])

    def test_several_reasons_merge_into_one_row_per_channel(self):
        """An owner who is also an officer: both reasons, the union of channels."""
        self.report.owner = self.officer
        self.report.save(update_fields=["owner"])

        notify_workflow_created(self.report, actor=self.actor)

        bell = self.rows(recipient=self.officer, channel="in_app").get()
        mail_row = self.rows(recipient=self.officer, channel="email").get()
        self.assertCountEqual(bell.reasons, ["owner", "officer"])
        self.assertCountEqual(mail_row.reasons, ["owner", "officer"])

    def test_named_people_can_be_switched_off_for_a_type(self):
        self.workflow_type.notify_named_people = False
        self.workflow_type.save()

        notify_workflow_created(self.report, actor=self.actor)

        self.assertFalse(self.rows(recipient=self.author).exists())

    def test_the_actor_is_never_told(self):
        notify_workflow_created(self.report, actor=self.officer)

        self.assertFalse(self.rows(recipient=self.officer).exists())

    def test_every_alert_kind_maps_to_a_rule_event(self):
        alert_kinds = {kind for kind, _label in Notification.alert_kinds()}
        self.assertEqual(alert_kinds, set(EVENT_FOR_KIND))
        self.assertFalse(alert_kinds & SHARE_KINDS)
        self.assertEqual(
            set(EVENT_FOR_KIND.values()),
            {value for value, _label in NotificationRule.EVENT_CHOICES},
        )


class NextActorTests(AudienceFixture):
    """Whoever's turn it is hears by bell and email; being able to see is not enough."""

    def test_the_holder_of_an_allowed_role_who_can_transition_is_told(self):
        self.move_to_review()

        self.assertIn("aud-clerk", self.told("workflow-transition", "in_app"))
        self.assertIn("aud-clerk", self.told("workflow-transition", "email"))
        row = self.rows("workflow-transition", "in_app", recipient=self.clerk).get()
        self.assertEqual(row.reasons, ["next-actor"])

    def test_a_member_who_acts_next_is_told_in_the_bell_but_never_mailed(self):
        self.move_to_review()

        self.assertIn("aud-mp", self.told("workflow-transition", "in_app"))
        self.assertNotIn("aud-mp", self.told("workflow-transition", "email"))

    def test_a_referred_committee_is_not_next_even_with_the_same_role(self):
        """
        ``Aud Clerk`` is held in the committee too, and a referral gives the
        committee access to the record — but never ``transition``. It is not the
        committee's move, so it must not be told it is.
        """
        self.referral()
        self.move_to_review()

        self.assertNotIn("aud-committee", self.told("workflow-transition", "in_app"))
        self.assertNotIn("aud-committee", self.told("workflow-transition", "email"))

    def test_a_group_that_may_not_transition_is_not_next(self):
        self.report.group_accesses().update(can_transition=False)

        self.move_to_review()

        self.assertNotIn("aud-clerk", self.told("workflow-transition", "in_app"))

    def test_a_non_actor_role_is_not_next(self):
        """Officers hold no role the Approve transition allows."""
        self.move_to_review()

        self.assertNotIn("aud-officer", self.told("workflow-transition", "in_app"))

    def test_next_actors_can_be_switched_off_for_a_type(self):
        self.workflow_type.notify_next_actors = False
        self.workflow_type.save()

        self.move_to_review()

        self.assertNotIn("aud-clerk", self.told("workflow-transition", "in_app"))

    def test_creation_does_not_tell_the_next_actors(self):
        """Only a *move* hands the turn over; a creation is not one."""
        notify_workflow_created(self.report, actor=self.actor)

        self.assertNotIn("aud-clerk", self.told("workflow-created", "in_app"))


class RuleTests(AudienceFixture):
    """``NotificationRule``: explicit, scoped, per-event recipients."""

    def test_a_rule_tells_the_role_holders_in_the_custodian(self):
        rule = self.rule(event="created", role=self.clerk_role, channel="both")

        notify_workflow_created(self.report, actor=self.actor)

        self.assertIn("aud-clerk", self.told("workflow-created", "in_app"))
        self.assertIn("aud-clerk", self.told("workflow-created", "email"))
        row = self.rows("workflow-created", "in_app", recipient=self.clerk).get()
        self.assertEqual(row.reasons, [f"rule:{rule.pk}"])

    def test_a_rule_is_scoped_so_the_same_role_elsewhere_is_not_told(self):
        """The committee holds ``Aud Clerk`` too; the custodian scope excludes it."""
        self.rule(event="created", role=self.clerk_role)
        self.referral()  # gives the committee access to the record
        Notification.objects.all().delete()

        notify_workflow_created(self.report, actor=self.actor)

        self.assertNotIn("aud-committee", self.told("workflow-created", "in_app"))

    def test_instance_groups_scope_is_the_explicit_wide_option(self):
        self.rule(
            event="created",
            role=self.clerk_role,
            scope=NotificationRule.SCOPE_INSTANCE_GROUPS,
        )
        self.referral()
        Notification.objects.all().delete()

        notify_workflow_created(self.report, actor=self.actor)

        self.assertIn("aud-committee", self.told("workflow-created", "in_app"))
        self.assertIn("aud-committee", self.told("workflow-created", "email"))

    def test_an_empty_role_means_every_member_of_the_scope(self):
        self.rule(event="created", role=None, channel="in_app")

        notify_workflow_created(self.report, actor=self.actor)

        bell = self.told("workflow-created", "in_app")
        self.assertTrue({"aud-clerk", "aud-mp", "aud-officer"} <= bell)
        self.assertNotIn("aud-committee", bell)

    def test_a_channel_limits_what_is_written(self):
        self.rule(event="created", role=self.clerk_role, channel="email")

        notify_workflow_created(self.report, actor=self.actor)

        self.assertIn("aud-clerk", self.told("workflow-created", "email"))
        self.assertNotIn("aud-clerk", self.told("workflow-created", "in_app"))

    def test_in_app_only_rule_writes_no_email(self):
        self.rule(event="created", role=self.clerk_role, channel="in_app")

        notify_workflow_created(self.report, actor=self.actor)

        self.assertIn("aud-clerk", self.told("workflow-created", "in_app"))
        self.assertNotIn("aud-clerk", self.told("workflow-created", "email"))

    def test_a_rule_can_put_a_member_in_the_bell_but_never_their_inbox(self):
        self.rule(event="created", role=self.clerk_role, channel="both")

        notify_workflow_created(self.report, actor=self.actor)

        self.assertIn("aud-mp", self.told("workflow-created", "in_app"))
        self.assertNotIn("aud-mp", self.told("workflow-created", "email"))

    def test_an_email_only_rule_writes_nothing_for_a_member(self):
        self.rule(event="created", role=self.clerk_role, channel="email")

        notify_workflow_created(self.report, actor=self.actor)

        self.assertFalse(self.rows(recipient=self.mp).exists())

    def test_a_rule_still_respects_who_may_open_the_record(self):
        from .models import WorkflowRolePermission

        self.rule(event="created", role=self.clerk_role)
        access = self.report.group_accesses().get(group=self.office)
        WorkflowRolePermission.objects.create(
            group_access=access, role=self.clerk_role, can_view=False
        )

        notify_workflow_created(self.report, actor=self.actor)

        self.assertNotIn("aud-clerk", self.told("workflow-created", "in_app"))

    def test_an_inactive_rule_is_ignored(self):
        self.rule(event="created", role=self.clerk_role, is_active=False)

        notify_workflow_created(self.report, actor=self.actor)

        self.assertFalse(self.rows(recipient=self.clerk).exists())

    def test_a_rule_for_another_event_is_ignored(self):
        self.rule(event="transition", role=self.clerk_role)

        notify_workflow_created(self.report, actor=self.actor)

        self.assertFalse(self.rows(recipient=self.clerk).exists())

    def test_a_rule_of_another_type_is_ignored(self):
        other = WorkflowType.objects.create(name="Aud Other", group=self.office)
        NotificationRule.objects.create(
            workflow_type=other, event="created", role=self.clerk_role
        )

        notify_workflow_created(self.report, actor=self.actor)

        self.assertFalse(self.rows(recipient=self.clerk).exists())

    def test_a_transition_rule_fires_only_for_its_transition(self):
        self.rule(event="transition", transition=self.approve, role=self.officer_role)

        def rule_reasons():
            rows = self.rows(recipient=self.officer, kind="workflow-transition")
            return [r for row in rows for r in row.reasons if r.startswith("rule:")]

        self.move_to_review()  # Submit, not Approve
        self.assertEqual(rule_reasons(), [])

        self.report.perform_transition(self.approve, actor=self.actor, comment="ok")
        self.assertTrue(rule_reasons())

    def test_a_transition_rule_without_a_transition_fires_for_every_one(self):
        rule = self.rule(event="transition", role=self.officer_role, channel="in_app")

        self.move_to_review()

        row = self.rows("workflow-transition", "in_app", recipient=self.officer).get()
        self.assertIn(f"rule:{rule.pk}", row.reasons)

    def test_rules_apply_on_referral_events_through_the_referred_group(self):
        """A rule scoped to the referred group tells only that committee."""
        rule = self.rule(
            event="referral-created",
            role=None,
            scope=NotificationRule.SCOPE_REFERRED_GROUP,
            channel="in_app",
        )

        referral = self.referral()
        notify_referral_created(referral, actor=self.actor)

        row = self.rows(
            "referral-created", "in_app", recipient=self.committee_member
        ).get()
        self.assertIn(f"rule:{rule.pk}", row.reasons)
        self.assertNotIn("aud-clerk", self.told("referral-created", "in_app"))

    def test_the_custodian_can_opt_into_hearing_about_referrals(self):
        self.rule(event="referral-created", role=self.officer_role, channel="email")

        notify_referral_created(self.referral(), actor=self.actor)

        self.assertIn("aud-officer", self.told("referral-created", "email"))

    def test_str_names_the_event_scope_and_role(self):
        rule = self.rule(
            event="transition", transition=self.approve, role=self.clerk_role
        )

        text = str(rule)

        self.assertIn("Aud Report", text)
        self.assertIn("Approve", text)
        self.assertIn("Aud Clerk", text)
        self.assertIn("custodian", text)


class RuleValidationTests(AudienceFixture):
    def build(self, **kwargs):
        kwargs.setdefault("workflow_type", self.workflow_type)
        kwargs.setdefault("event", "created")
        return NotificationRule(**kwargs)

    def test_a_transition_needs_a_transition_event(self):
        rule = self.build(event="created", transition=self.approve)

        with self.assertRaises(ValidationError) as caught:
            rule.full_clean()

        self.assertIn("transition", caught.exception.message_dict)

    def test_a_transition_must_belong_to_the_type(self):
        other = WorkflowType.objects.create(name="Aud Elsewhere", group=self.office)
        a = make_state(workflow_type=other, name="A", is_initial=True)
        b = make_state(workflow_type=other, name="B")
        foreign = Transition.objects.create(
            workflow_type=other, name="Go", from_state=a, to_state=b
        )
        rule = self.build(event="transition", transition=foreign)

        with self.assertRaises(ValidationError) as caught:
            rule.full_clean()

        self.assertIn("transition", caught.exception.message_dict)

    def test_the_referred_group_needs_a_referral_event(self):
        rule = self.build(event="created", scope="referred_group")

        with self.assertRaises(ValidationError) as caught:
            rule.full_clean()

        self.assertIn("scope", caught.exception.message_dict)

    def test_the_host_group_does_not_apply_to_referrals(self):
        rule = self.build(event="referral-created", scope="host_group")

        with self.assertRaises(ValidationError) as caught:
            rule.full_clean()

        self.assertIn("scope", caught.exception.message_dict)

    def test_valid_rules_pass(self):
        self.build(event="transition", transition=self.approve).full_clean()
        self.build(event="referral-closed", scope="referred_group").full_clean()
        self.build(event="created", role=self.clerk_role).full_clean()


class ScopeTests(AudienceFixture):
    """An event is about specific groups; viewer and referral groups are not it."""

    def test_the_owner_staff_in_a_viewer_group_is_not_emailed(self):
        """
        The owner hears in the bell. Email needs staff membership of a group the
        event concerns — a viewer group attached to the record is not one.
        """
        viewer = Group.objects.create(name="Aud Viewers", group_type="division")
        GroupMembership.objects.create(
            user=self.author, group=viewer, role=self.clerk_role
        )
        self.workflow_type.viewer_groups.add(viewer)
        self.report = DelegationReport.objects.create(
            workflow_type=self.workflow_type,
            current_state=self.draft,
            title="With viewers",
            owner=self.author,
        )
        Notification.objects.all().delete()

        notify_workflow_created(self.report, actor=self.actor)

        self.assertIn("aud-author", self.told("workflow-created", "in_app"))
        self.assertNotIn("aud-author", self.told("workflow-created", "email"))

    def test_a_type_without_a_custodian_falls_back_to_the_record_groups(self):
        """Seeded before its group existed: the audience must not vanish."""
        self.workflow_type.group = None
        self.workflow_type.save()
        report = DelegationReport.objects.create(
            workflow_type=self.workflow_type,
            current_state=self.draft,
            title="No custodian",
            owner=self.author,
        )
        # No custodian, so no access rows were materialised; give the committee one.
        WorkflowGroupAccess.objects.create(
            content_type=ContentType.objects.get_for_model(report),
            object_id=report.pk,
            group=self.committee,
            can_view=True,
        )
        self.rule(event="created", role=self.clerk_role)

        notify_workflow_created(report, actor=self.actor)

        self.assertIn("aud-committee", self.told("workflow-created", "in_app"))

    def test_a_transition_does_not_reach_a_group_that_only_has_view_access(self):
        self.report.group_accesses().create(
            group=self.committee,
            content_type=ContentType.objects.get_for_model(self.report),
            object_id=self.report.pk,
            can_view=True,
        )

        self.move_to_review()

        self.assertFalse(self.rows(recipient=self.committee_member).exists())


class ReferralAudienceTests(AudienceFixture):
    """A referral is the committee's business, not a blast to the custodian."""

    def test_the_committee_and_owner_are_told_but_not_the_officers(self):
        notify_referral_created(self.referral(), actor=self.officer)

        bell = self.told("referral-created", "in_app")
        self.assertIn("aud-committee", bell)
        self.assertIn("aud-author", bell)
        self.assertNotIn("aud-clerk", bell)
        self.assertNotIn("aud-officer", bell)

    def test_the_committee_is_told_because_it_was_referred(self):
        notify_referral_created(self.referral(), actor=self.officer)

        row = self.rows(
            "referral-created", "in_app", recipient=self.committee_member
        ).get()
        self.assertEqual(row.reasons, ["referred-group"])
        self.assertTrue(
            self.rows(
                "referral-created", "email", recipient=self.committee_member
            ).exists()
        )

    def test_the_referrer_hears_when_it_is_answered(self):
        referral = self.referral(referred_by=self.officer)

        referral.respond(responded_by=self.committee_member, notes="fine")

        row = self.rows("referral-responded", "in_app", recipient=self.officer).get()
        self.assertEqual(row.reasons, ["referrer"])
        # Staff in the custodian, which the event concerns, so mailed as well.
        self.assertTrue(
            self.rows("referral-responded", "email", recipient=self.officer).exists()
        )

    def test_the_referrer_hears_when_it_is_recalled_or_expires(self):
        recalled = self.referral(referred_by=self.officer)
        recalled.recall(recalled_by=self.actor, reason="withdrawn")
        self.assertTrue(self.rows("referral-recalled", recipient=self.officer).exists())

        expiring = self.referral(
            referred_by=self.officer, due_date=timezone.now() - timedelta(hours=1)
        )
        expiring.mark_expired()
        self.assertTrue(self.rows("referral-expired", recipient=self.officer).exists())

    def test_the_referrer_is_not_told_on_creation(self):
        """They raised it; and in any case only the closing notice names them."""
        notify_referral_created(self.referral(referred_by=self.clerk), actor=None)

        self.assertFalse(self.rows("referral-created", recipient=self.clerk).exists())

    def test_the_acting_referrer_is_not_told_about_their_own_recall(self):
        referral = self.referral(referred_by=self.officer)

        referral.recall(recalled_by=self.officer, reason="mine to withdraw")

        self.assertFalse(
            self.rows("referral-recalled", recipient=self.officer).exists()
        )

    def test_referral_audience_is_an_audience_with_reasons(self):
        audience = referral_audience(self.referral(), event="referral-created")

        self.assertIsInstance(audience, Audience)
        self.assertIn(self.committee_member, audience.users)
        self.assertEqual(
            audience.reasons_for(self.committee_member), ["referred-group"]
        )
        self.assertTrue(audience)

    def test_the_deadline_audience_is_the_committee(self):
        audience = referral_audience(self.referral(), event="referral-deadline")

        self.assertIn(self.committee_member, audience.users)
        self.assertNotIn(self.officer, audience.users)

    def test_an_unreachable_referral_has_no_audience(self):
        committee = Group.objects.create(
            name="Empty Committee", group_type="portfolio_committee"
        )
        referral = WorkflowReferral.objects.create(
            content_type=ContentType.objects.get_for_model(DelegationReport),
            object_id=self.report.pk,
            referred_to=committee,
            referred_by=self.officer,
        )
        self.workflow_type.notify_named_people = False
        self.workflow_type.save()

        self.assertFalse(referral_audience(referral, event="referral-deadline"))


class MeetingAudienceTests(MeetingFixture):
    """Meetings: the register is *involved*; the host group joins the custodian."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.host = Group.objects.create(
            name="Aud Host Committee", group_type="portfolio_committee"
        )
        cls.host_role = Role.objects.create(name="Aud Host Member")
        cls.host_staff = make_user("aud-host-staff")
        GroupMembership.objects.create(
            user=cls.host_staff, group=cls.host, role=cls.host_role
        )
        cls.floating_staff = make_user("aud-floating")

    def invite(self, meeting, *users):
        for user in users:
            MeetingAttendance.objects.create(meeting=meeting, user=user)

    def told(self, kind, channel):
        return set(
            Notification.objects.filter(kind=kind, channel=channel).values_list(
                "recipient__username", flat=True
            )
        )

    def test_attendees_are_participants(self):
        meeting = self._meeting(host_group=self.host)
        self.invite(meeting, self.host_staff, self.floating_staff)

        notify_meeting_created(meeting, actor=self.organiser)

        row = Notification.objects.get(
            kind="meeting-created", channel="in_app", recipient=self.floating_staff
        )
        self.assertEqual(row.reasons, ["participant"])

    def test_staff_of_the_host_group_are_emailed_other_attendees_are_not(self):
        meeting = self._meeting(host_group=self.host)
        self.invite(meeting, self.host_staff, self.floating_staff)

        notify_meeting_created(meeting, actor=self.organiser)

        self.assertIn("aud-host-staff", self.told("meeting-created", "email"))
        self.assertNotIn("aud-floating", self.told("meeting-created", "email"))
        self.assertIn("aud-floating", self.told("meeting-created", "in_app"))

    def grant_host_access(self, meeting):
        WorkflowGroupAccess.objects.create(
            content_type=ContentType.objects.get_for_model(meeting),
            object_id=meeting.pk,
            group=self.host,
            can_view=True,
        )

    def test_a_host_group_rule_tells_the_convening_group(self):
        meeting = self._meeting(host_group=self.host)
        self.grant_host_access(meeting)
        NotificationRule.objects.create(
            workflow_type=self.meeting_type,
            event="created",
            scope=NotificationRule.SCOPE_HOST_GROUP,
            role=self.host_role,
            channel="both",
        )

        notify_meeting_created(meeting, actor=self.organiser)

        self.assertIn("aud-host-staff", self.told("meeting-created", "in_app"))
        self.assertIn("aud-host-staff", self.told("meeting-created", "email"))

    def test_a_host_group_rule_never_reaches_someone_who_cannot_open_it(self):
        """Rules narrow by scope; they never widen who may see the record."""
        meeting = self._meeting(host_group=self.host)  # no access row for the host
        NotificationRule.objects.create(
            workflow_type=self.meeting_type,
            event="created",
            scope=NotificationRule.SCOPE_HOST_GROUP,
            role=self.host_role,
        )

        notify_meeting_created(meeting, actor=self.organiser)

        self.assertNotIn("aud-host-staff", self.told("meeting-created", "in_app"))

    def test_a_host_group_rule_resolves_nothing_without_a_host(self):
        meeting = self._meeting()
        NotificationRule.objects.create(
            workflow_type=self.meeting_type,
            event="created",
            scope=NotificationRule.SCOPE_HOST_GROUP,
            role=self.host_role,
        )

        notify_meeting_created(meeting, actor=self.organiser)

        self.assertNotIn("aud-host-staff", self.told("meeting-created", "in_app"))

    def test_the_meeting_status_change_uses_transition_rules(self):
        meeting = self._meeting()
        rule = NotificationRule.objects.create(
            workflow_type=self.meeting_type,
            event="transition",
            transition=self.start,
            role=self.ecm_role,
            channel="in_app",
        )
        other = make_user("aud-ecm-colleague")
        GroupMembership.objects.create(
            user=other, group=self.ict_group, role=self.ecm_role
        )

        meeting.perform_transition(self.start, actor=self.organiser, comment="")

        row = Notification.objects.get(
            kind="meeting-status", channel="in_app", recipient=other
        )
        self.assertIn(f"rule:{rule.pk}", row.reasons)

    def test_stakeholders_for_a_meeting_include_the_register(self):
        meeting = self._meeting(host_group=self.host)
        self.invite(meeting, self.floating_staff)

        audience = stakeholders(meeting, event="meeting-document")

        self.assertIn(self.floating_staff, audience.users)


class PreferenceTests(AudienceFixture):
    """A user can narrow what they receive, but never what they owe."""

    def mute(self, user, kind, *, in_app=True, email=True):
        NotificationPreference.objects.update_or_create(
            user=user,
            kind=kind,
            defaults={"in_app_enabled": in_app, "email_enabled": email},
        )

    def setUp(self):
        super().setUp()
        self.workflow_type.oversight_channel = WorkflowType.OVERSIGHT_BOTH
        self.workflow_type.save()

    def test_muting_email_leaves_the_bell(self):
        self.mute(self.officer, "workflow-created", email=False)

        notify_workflow_created(self.report, actor=self.actor)

        self.assertIn("aud-officer", self.told("workflow-created", "in_app"))
        self.assertNotIn("aud-officer", self.told("workflow-created", "email"))

    def test_muting_the_bell_leaves_email(self):
        self.mute(self.officer, "workflow-created", in_app=False)

        notify_workflow_created(self.report, actor=self.actor)

        self.assertNotIn("aud-officer", self.told("workflow-created", "in_app"))
        self.assertIn("aud-officer", self.told("workflow-created", "email"))

    def test_muting_both_writes_nothing(self):
        self.mute(self.officer, "workflow-created", in_app=False, email=False)

        notify_workflow_created(self.report, actor=self.actor)

        self.assertFalse(self.rows(recipient=self.officer).exists())

    def test_a_preference_is_per_kind(self):
        self.mute(self.officer, "workflow-created", in_app=False, email=False)

        self.move_to_review()

        self.assertTrue(
            self.rows("workflow-transition", recipient=self.officer).exists()
        )

    def test_a_preference_is_per_user(self):
        self.mute(self.officer, "workflow-created", in_app=False, email=False)
        self.report.owner = self.author
        self.report.save()

        notify_workflow_created(self.report, actor=self.actor)

        self.assertIn("aud-author", self.told("workflow-created", "in_app"))

    def test_it_never_silences_whose_turn_it_is(self):
        self.mute(self.clerk, "workflow-transition", in_app=False, email=False)

        self.move_to_review()

        self.assertIn("aud-clerk", self.told("workflow-transition", "in_app"))
        self.assertIn("aud-clerk", self.told("workflow-transition", "email"))

    def test_it_never_silences_the_assignee(self):
        self.report.assigned_to = self.clerk
        self.report.save(update_fields=["assigned_to"])
        self.mute(self.clerk, "workflow-created", in_app=False, email=False)

        notify_workflow_created(self.report, actor=self.actor)

        self.assertIn("aud-clerk", self.told("workflow-created", "in_app"))

    def test_it_never_silences_a_referral_to_your_committee(self):
        self.mute(self.committee_member, "referral-created", in_app=False, email=False)

        notify_referral_created(self.referral(), actor=self.officer)

        self.assertIn("aud-committee", self.told("referral-created", "in_app"))
        self.assertIn("aud-committee", self.told("referral-created", "email"))

    def test_a_deadline_reminder_is_mandatory_for_everyone(self):
        """Even the owner (who is merely informed) cannot mute a deadline."""
        self.mute(self.author, "referral-deadline", in_app=False, email=False)
        self.mute(self.committee_member, "referral-deadline", in_app=False, email=False)
        self.referral(due_date=timezone.now() + timedelta(hours=2))

        call_command("check_referral_deadlines", stdout=StringIO())

        bell = self.told("referral-deadline", "in_app")
        self.assertIn("aud-author", bell)
        self.assertIn("aud-committee", bell)

    def test_a_preference_cannot_add_a_recipient(self):
        """Switching everything *on* tells nobody who the rules did not."""
        self.mute(self.outsider_user(), "workflow-created")

        notify_workflow_created(self.report, actor=self.actor)

        self.assertFalse(self.rows(recipient=self.outsider_user()).exists())

    def test_informed_reasons_can_be_muted_but_action_reasons_survive_alongside(self):
        """Owner (muted) and next actor (not) in one person: the turn wins."""
        self.report.owner = self.clerk
        self.report.save(update_fields=["owner"])
        self.mute(self.clerk, "workflow-transition", in_app=False, email=False)

        self.move_to_review()

        row = self.rows("workflow-transition", "in_app", recipient=self.clerk).get()
        self.assertIn("next-actor", row.reasons)

    def outsider_user(self):
        if not hasattr(self, "_outsider"):
            self._outsider = make_user("aud-outsider")
        return self._outsider

    def test_the_unique_constraint(self):
        self.mute(self.officer, "workflow-created", email=False)

        with self.assertRaises(IntegrityError), transaction.atomic():
            NotificationPreference.objects.create(
                user=self.officer, kind="workflow-created"
            )

    def test_str(self):
        preference = NotificationPreference.objects.create(
            user=self.officer, kind="workflow-created", email_enabled=False
        )

        self.assertIn("email off", str(preference))
        self.assertIn("bell on", str(preference))


class SavePreferencesTests(AudienceFixture):
    def kinds(self):
        return [kind for kind, _label in Notification.alert_kinds()]

    def all_on(self):
        return {
            f"{prefix}__{kind}"
            for kind in self.kinds()
            for prefix in ("in_app", "email")
        }

    def test_unticked_boxes_become_mutes(self):
        submitted = self.all_on() - {"email__workflow-created"}

        save_preferences(self.officer, submitted)

        preference = NotificationPreference.objects.get(
            user=self.officer, kind="workflow-created"
        )
        self.assertTrue(preference.in_app_enabled)
        self.assertFalse(preference.email_enabled)
        # Everything left on has no row: the table only holds exceptions.
        self.assertEqual(
            NotificationPreference.objects.filter(user=self.officer).count(), 1
        )

    def test_turning_everything_back_on_removes_the_row(self):
        save_preferences(self.officer, self.all_on() - {"in_app__workflow-created"})

        save_preferences(self.officer, self.all_on())

        self.assertFalse(
            NotificationPreference.objects.filter(user=self.officer).exists()
        )

    def test_saving_twice_is_idempotent(self):
        submitted = self.all_on() - {"email__workflow-created"}

        save_preferences(self.officer, submitted)
        save_preferences(self.officer, submitted)

        self.assertEqual(
            NotificationPreference.objects.filter(user=self.officer).count(), 1
        )

    def test_locked_kinds_are_never_stored(self):
        save_preferences(self.officer, set())

        stored = set(
            NotificationPreference.objects.filter(user=self.officer).values_list(
                "kind", flat=True
            )
        )
        self.assertFalse(stored & MANDATORY_KINDS)
        self.assertEqual(stored, set(self.kinds()) - MANDATORY_KINDS)

    def test_unknown_names_are_ignored(self):
        save_preferences(
            self.officer, self.all_on() | {"email__not-a-kind", "csrfmiddlewaretoken"}
        )

        self.assertFalse(
            NotificationPreference.objects.filter(user=self.officer).exists()
        )

    def test_it_only_touches_that_user(self):
        save_preferences(self.officer, set())

        self.assertFalse(
            NotificationPreference.objects.filter(user=self.clerk).exists()
        )

    def test_preference_rows_reflect_the_saved_state(self):
        save_preferences(self.officer, self.all_on() - {"email__workflow-created"})

        rows = {row["kind"]: row for row in preference_rows(self.officer)}

        self.assertTrue(rows["workflow-created"]["in_app"])
        self.assertFalse(rows["workflow-created"]["email"])
        self.assertTrue(rows["workflow-transition"]["email"])
        self.assertTrue(rows["referral-deadline"]["locked"])
        self.assertFalse(rows["workflow-created"]["locked"])
        self.assertNotIn("report-share", rows)


class PreferencePageTests(AudienceFixture):
    url = None

    def setUp(self):
        super().setUp()
        self.url = reverse("pwms:notification_preferences")

    def test_authentication_is_required(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/pwms/login/", response["Location"])

    def test_the_page_lists_every_alert_kind(self):
        self.client.force_login(self.officer)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Workflow created")
        self.assertContains(response, 'name="email__workflow-created"')
        self.assertNotContains(response, "Report shared")
        self.assertContains(response, "Always sent")

    def test_the_locked_kind_is_disabled(self):
        self.client.force_login(self.officer)

        response = self.client.get(self.url)

        self.assertContains(response, 'name="email__referral-deadline"')
        html = response.content.decode()
        start = html.index('name="email__referral-deadline"')
        self.assertIn("disabled", html[start : start + 200])

    def test_saving_stores_the_unticked_boxes(self):
        self.client.force_login(self.officer)
        data = {
            f"{prefix}__{kind}": "on"
            for kind, _label in Notification.alert_kinds()
            for prefix in ("in_app", "email")
        }
        del data["email__workflow-transition"]

        response = self.client.post(self.url, data)

        self.assertRedirects(response, self.url)
        preference = NotificationPreference.objects.get(
            user=self.officer, kind="workflow-transition"
        )
        self.assertFalse(preference.email_enabled)
        self.assertTrue(preference.in_app_enabled)

    def test_the_page_shows_the_saved_state(self):
        NotificationPreference.objects.create(
            user=self.officer, kind="workflow-created", email_enabled=False
        )
        self.client.force_login(self.officer)

        response = self.client.get(self.url)

        rows = {row["kind"]: row for row in response.context["rows"]}
        self.assertFalse(rows["workflow-created"]["email"])

    def test_one_user_cannot_change_anothers_preferences(self):
        self.client.force_login(self.officer)

        self.client.post(self.url, {})

        self.assertFalse(
            NotificationPreference.objects.filter(user=self.clerk).exists()
        )
        self.assertTrue(
            NotificationPreference.objects.filter(user=self.officer).exists()
        )

    def test_the_alerts_page_links_to_it(self):
        self.client.force_login(self.officer)

        response = self.client.get(reverse("pwms:notifications"))

        self.assertContains(response, self.url)


class NotifyRolesMigrationTests(AudienceFixture):
    """
    Migration 0067 turns ``Transition.notify_roles`` into rules and back.

    The column is gone by the time tests run, so the migration's functions are
    exercised with small stand-ins for the historical ``Transition`` model;
    ``NotificationRule`` is the real table.
    """

    class Roles:
        def __init__(self, roles):
            self.roles = roles
            self.added = []

        def all(self):
            return self.roles

        def add(self, role_id):
            self.added.append(role_id)

    class FakeTransition:
        def __init__(self, pk, workflow_type_id, roles):
            self.pk = pk
            self.workflow_type_id = workflow_type_id
            self.notify_roles = NotifyRolesMigrationTests.Roles(roles)

    @staticmethod
    def apps_with(transitions):
        class Manager:
            def prefetch_related(self, *_args):
                return transitions

        class TransitionModel:
            objects = Manager()

        class Apps:
            def get_model(self, _app, name):
                return (
                    NotificationRule if name == "NotificationRule" else TransitionModel
                )

        return Apps()

    @staticmethod
    def migration():
        import importlib

        return importlib.import_module(
            "pwms.migrations.0067_notification_rules_and_preferences"
        )

    def test_each_notify_role_becomes_a_rule_keeping_its_old_reach(self):
        fake = self.FakeTransition(
            self.approve.pk, self.workflow_type.pk, [self.clerk_role, self.officer_role]
        )

        self.migration().notify_roles_to_rules(self.apps_with([fake]), None)

        rules = NotificationRule.objects.filter(transition=self.approve)
        self.assertCountEqual(
            rules.values_list("role__name", flat=True), ["Aud Clerk", "Aud Officer"]
        )
        for rule in rules:
            self.assertEqual(rule.event, "transition")
            self.assertEqual(rule.workflow_type, self.workflow_type)
            # notify_roles looked in every group with access, so the rule does too.
            self.assertEqual(rule.scope, NotificationRule.SCOPE_INSTANCE_GROUPS)
            self.assertEqual(rule.channel, NotificationRule.CHANNEL_BOTH)

    def test_running_it_twice_does_not_duplicate_rules(self):
        fake = self.FakeTransition(
            self.approve.pk, self.workflow_type.pk, [self.clerk_role]
        )
        apps = self.apps_with([fake])

        self.migration().notify_roles_to_rules(apps, None)
        self.migration().notify_roles_to_rules(apps, None)

        self.assertEqual(NotificationRule.objects.count(), 1)

    def test_a_transition_with_no_notify_roles_makes_no_rules(self):
        fake = self.FakeTransition(self.approve.pk, self.workflow_type.pk, [])

        self.migration().notify_roles_to_rules(self.apps_with([fake]), None)

        self.assertEqual(NotificationRule.objects.count(), 0)

    def test_reversing_restores_only_what_notify_roles_could_express(self):
        """Per-transition role rules go back; everything else has no old home."""
        self.rule(event="transition", transition=self.approve, role=self.clerk_role)
        self.rule(event="transition", role=self.officer_role)
        self.rule(event="created", role=self.officer_role)
        self.rule(event="transition", transition=self.approve, role=None)
        restored = self.Roles([])

        class Transition:
            notify_roles = restored

        class Rules:
            def filter(self, **kwargs):
                from types import SimpleNamespace

                rows = [
                    SimpleNamespace(transition=Transition(), role_id=row.role_id)
                    for row in NotificationRule.objects.filter(**kwargs)
                ]
                return SimpleNamespace(select_related=lambda *_args: rows)

        class Model:
            objects = Rules()

        class Apps:
            def get_model(self, _app, _name):
                return Model

        self.migration().rules_to_notify_roles(Apps(), None)

        self.assertEqual(restored.added, [self.clerk_role.pk])


class AdminTests(AudienceFixture):
    """The rules are meant to be managed in the admin, so those pages must work."""

    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser(
            username="aud-root", email="root@example.com", password="pw"
        )
        self.client.force_login(self.superuser)

    def test_the_type_page_renders_with_its_rules_and_oversight(self):
        self.rule(event="created", role=self.clerk_role)

        response = self.client.get(
            reverse("admin:pwms_workflowtype_change", args=[self.workflow_type.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "oversight_channel")
        self.assertContains(response, "notification_rules-0-scope")

    def test_the_type_page_only_offers_that_types_transitions_on_a_rule(self):
        other = WorkflowType.objects.create(name="Aud Elsewhere", group=self.office)
        a = make_state(workflow_type=other, name="A", is_initial=True)
        b = make_state(workflow_type=other, name="B")
        Transition.objects.create(
            workflow_type=other, name="Foreign go", from_state=a, to_state=b
        )
        self.rule(event="transition", role=self.clerk_role)

        response = self.client.get(
            reverse("admin:pwms_workflowtype_change", args=[self.workflow_type.pk])
        )

        self.assertContains(response, "Approve")
        self.assertNotContains(response, "Foreign go")

    def test_the_transition_page_renders_without_notify_roles(self):
        response = self.client.get(
            reverse("admin:pwms_transition_change", args=[self.approve.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "notify_roles")
        self.assertContains(response, "Alert rules")

    def test_a_rule_added_on_a_transition_page_is_a_rule_about_that_transition(self):
        url = reverse("admin:pwms_transition_change", args=[self.approve.pk])
        page = self.client.get(url)
        form = page.context["adminform"].form
        data = {
            name: ("" if form[name].value() is None else form[name].value())
            for name in form.fields
        }
        data = {
            key: value
            for key, value in data.items()
            if not isinstance(value, (list, tuple, set))
        }
        data["allowed_roles"] = [self.clerk_role.pk]
        data["required_event_types"] = []
        for formset in page.context["inline_admin_formsets"]:
            prefix = formset.formset.prefix
            # Only the alert-rule inline gets a new row; the others stay empty.
            data[f"{prefix}-TOTAL_FORMS"] = (
                "1" if prefix == "notification_rules" else "0"
            )
            data[f"{prefix}-INITIAL_FORMS"] = "0"
            data[f"{prefix}-MIN_NUM_FORMS"] = "0"
            data[f"{prefix}-MAX_NUM_FORMS"] = "1000"
        data.update(
            {
                "notification_rules-0-scope": "custodian",
                "notification_rules-0-role": self.officer_role.pk,
                "notification_rules-0-channel": "in_app",
                "notification_rules-0-is_active": "on",
            }
        )

        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        rule = NotificationRule.objects.get(role=self.officer_role)
        self.assertEqual(rule.transition, self.approve)
        self.assertEqual(rule.event, "transition")
        self.assertEqual(rule.workflow_type, self.workflow_type)

    def test_the_rule_list_and_preference_list_render(self):
        self.rule(event="created", role=self.clerk_role)
        NotificationPreference.objects.create(
            user=self.officer, kind="workflow-created", email_enabled=False
        )

        for name in (
            "admin:pwms_notificationrule_changelist",
            "admin:pwms_notificationpreference_changelist",
            "admin:pwms_notification_changelist",
        ):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_the_log_lists_why_each_person_was_told(self):
        notify_workflow_created(self.report, actor=self.actor)

        response = self.client.get(reverse("admin:pwms_notification_changelist"))

        self.assertContains(response, "officer")
        self.assertContains(response, "Why")


class AuditTests(AudienceFixture):
    def test_the_admin_shows_why(self):
        from django.contrib import admin as django_admin

        from .admin import NotificationAdmin

        notify_workflow_created(self.report, actor=self.actor)
        row = self.rows(recipient=self.officer, channel="email").get()

        model_admin = NotificationAdmin(Notification, django_admin.site)

        self.assertEqual(model_admin.why(row), "officer")

    def test_rows_without_reasons_are_still_valid(self):
        """Rows written before reasons existed (and shares) carry an empty list."""
        row = Notification.objects.create(
            recipient=self.officer,
            channel="email",
            kind="workflow-created",
            subject="s",
        )

        self.assertEqual(row.reasons, [])
