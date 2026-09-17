"""The site's flash messages, rendered as toast-like boxes by ``base.html``.

The boxes are the only place ``django.contrib.messages`` reaches the screen, so
these read a message back out of the page a redirect lands on — one success, one
error — which pins both the rendering and the level-to-Bootstrap mapping.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import DelegationReport, WorkflowType


class FlashMessageTests(TestCase):
    """Messages queued by a view appear on the page its redirect returns to."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="flash-owner", password="pw")
        cls.plain = User.objects.create_user(username="flash-plain", password="pw")
        workflow_type = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=workflow_type,
            current_state=workflow_type.get_initial_state(),
            title="Flash message report",
            owner=cls.owner,
        )

    def test_a_success_message_is_rendered_on_the_page_it_redirects_to(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("pwms:delegation_report_delete", args=[self.report.public_id]),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="site-message alert alert-success"')
        self.assertContains(response, "deleted.")

    def test_an_error_message_is_rendered_as_a_danger_box(self):
        # A signed-in user with no role that may create a report is sent back to
        # the register, carrying the refusal as a message.
        self.client.force_login(self.plain)

        response = self.client.get(
            reverse("pwms:delegation_report_create"), follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="site-message alert alert-danger"')
        self.assertContains(
            response, "You do not have a role that may create delegation reports."
        )

    def test_a_page_with_no_message_carries_no_message_stack(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[self.report.public_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "site-messages")
