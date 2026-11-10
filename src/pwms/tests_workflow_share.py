"""Actions -> Share (link, email) and Print on a workflow detail page."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from .models import DelegationReport, WorkflowType


class WorkflowShareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(
            username="share-owner",
            password="pw",
            email="owner@parliament.gov.za",
            first_name="Olive",
            last_name="Owner",
        )
        cls.colleague = User.objects.create_user(
            username="colleague",
            password="pw",
            email="colleague@parliament.gov.za",
            first_name="Colin",
            last_name="Colleague",
        )
        cls.outsider = User.objects.create_user(
            username="share-outsider", password="pw", email="out@parliament.gov.za"
        )
        # An owner may view their record but is not thereby allowed to share it:
        # that right comes from a group grant, so the sender here is a superuser.
        cls.admin = User.objects.create_superuser(
            username="share-admin", password="pw", email="admin@parliament.gov.za"
        )
        wt = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=wt,
            current_state=wt.get_initial_state(),
            title="Share test report",
            owner=cls.owner,
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def _url(self, name="workflow_share"):
        return reverse(f"pwms:{name}", kwargs={"public_id": self.report.public_id})

    def _detail(self):
        return reverse(
            "pwms:delegation_report_detail",
            kwargs={"public_id": self.report.public_id},
        )

    # -- the page ------------------------------------------------------------
    def test_actions_menu_offers_share_and_print(self):
        response = self.client.get(self._detail())
        self.assertContains(response, 'id="modal-share"')
        self.assertContains(response, "data-workflow-print")
        self.assertContains(response, self._url())

    # -- email ---------------------------------------------------------------
    def test_emails_every_recipient_internal_and_external(self):
        response = self.client.post(
            self._url(),
            {
                "recipients": "colleague@parliament.gov.za, ext@example.org\nother@example.org",
                "message": "Please review.",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sent"], 3)
        self.assertEqual(len(mail.outbox), 3)
        # One message each, so recipients are not disclosed to one another.
        self.assertTrue(all(len(message.to) == 1 for message in mail.outbox))
        self.assertIn(self._detail(), mail.outbox[0].body)
        self.assertIn("Please review.", mail.outbox[0].body)

    def test_duplicate_addresses_are_sent_once(self):
        response = self.client.post(
            self._url(),
            {"recipients": "a@example.org, A@example.org; a@example.org"},
        )
        self.assertEqual(response.json()["sent"], 1)

    def test_a_bad_address_is_named_and_nothing_is_sent(self):
        response = self.client.post(
            self._url(), {"recipients": "good@example.org, not-an-address"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not-an-address", str(response.json()["errors"]))
        self.assertEqual(len(mail.outbox), 0)

    def test_at_least_one_recipient_is_required(self):
        response = self.client.post(self._url(), {"recipients": " "})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_recipients_without_access_are_warned_about(self):
        response = self.client.post(
            self._url(),
            {"recipients": "colleague@parliament.gov.za, ext@example.org"},
        )
        warnings = " ".join(response.json()["warnings"])
        self.assertIn("ext@example.org", warnings)

    def test_attaching_a_copy_adds_the_document_and_drops_the_warning(self):
        document = ("report.html", b"<html></html>", "text/html")
        with mock.patch(
            "pwms.reporting.instrument_sharing.instrument_content",
            return_value=document,
        ):
            response = self.client.post(
                self._url(),
                {"recipients": "ext@example.org", "attach_format": "html"},
            )
        self.assertEqual(response.json()["warnings"], [])
        self.assertEqual(mail.outbox[0].attachments[0][0], "report.html")

    def test_sharing_needs_the_share_right(self):
        # The owner can view the record but holds no share grant on it.
        for user in (self.owner, self.outsider):
            self.client.force_login(user)
            response = self.client.post(self._url(), {"recipients": "x@example.org"})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_page_hides_share_from_someone_who_cannot_share(self):
        self.client.force_login(self.owner)
        response = self.client.get(self._detail())
        self.assertNotContains(response, 'id="modal-share"')
        self.assertContains(response, "data-workflow-print")

    def test_sharing_is_post_only(self):
        self.assertEqual(self.client.get(self._url()).status_code, 405)

    # -- internal recipient search ------------------------------------------------
    def test_search_finds_internal_users_by_name_with_an_address(self):
        response = self.client.get(
            self._url("workflow_share_recipients"), {"search": "Coli"}
        )
        self.assertContains(response, "colleague@parliament.gov.za")

    def test_search_ignores_one_character_queries(self):
        response = self.client.get(
            self._url("workflow_share_recipients"), {"search": "c"}
        )
        self.assertNotContains(response, "data-share-recipient")

    def test_search_needs_the_share_right(self):
        self.client.force_login(self.outsider)
        response = self.client.get(
            self._url("workflow_share_recipients"), {"search": "Coli"}
        )
        self.assertEqual(response.status_code, 403)
