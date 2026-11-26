from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import (
    DelegationReport,
    Group,
    GroupMembership,
    InternationalResolution,
    Role,
    TransitionLog,
    WorkflowReferral,
    WorkflowType,
)
from ..services.progress import machine_for


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
        with mock.patch("pwms.views.dashboard.machine_for", wraps=machine_for) as build:
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
        with mock.patch("pwms.views.dashboard.machine_for", wraps=machine_for) as build:
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
