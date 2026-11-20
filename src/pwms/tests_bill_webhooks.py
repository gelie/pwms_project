"""Tests for the Bill update webhook: queueing, delivery and the receiver.

The exchange has two sides — the sender (``pwms.services.bill_updates`` plus the
background task) and the testing receiver (``POST /api/v1/bill-updates/``). Each
is exercised here, including the failure path that makes the queue retry and the
idempotency key that stops a retry writing a second row.
"""

import json
from unittest import mock
from uuid import uuid4

import httpx
from background_task.models import Task
from background_task.tasks import tasks as bg_tasks
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Bill,
    BillUpdateLog,
    DelegationReport,
    PublicStatus,
    State,
    Transition,
    WorkflowType,
)
from .services.bill_updates import (
    EVENT_EDITS,
    EVENT_NEW,
    EVENT_REJECTION,
    EVENT_STATUS_CHANGE,
    BillUpdateDeliveryError,
    deliver_update,
    endpoint_url,
    queue_bill_update,
)

User = get_user_model()

WEBHOOK_URL = "/api/v1/bill-updates/"
REJECTION_URL = "/api/v1/bill-updates/rejections/"
TASK_NAME = "pwms.tasks.post_bill_update"
TOKEN = "test-webhook-token"


def _bill_and_user():
    """A bill in its seeded initial state, owned by a plain user."""
    user = User.objects.create_user(
        username="bill-poster", email="poster@example.org", password="pw"
    )
    workflow_type = WorkflowType.objects.get(name="Bill")
    bill = Bill.objects.create(
        workflow_type=workflow_type,
        current_state=workflow_type.get_initial_state(),
        bill_number="B 7-2026",
        title="Webhook bill",
        short_title="Webhook",
        bill_type=Bill.SECTION_75,
        house_of_origin=Bill.NA,
        owner=user,
    )
    return bill, user


@override_settings(
    BILL_UPDATE_WEBHOOK_ENABLED=True,
    BILL_UPDATE_ENDPOINT_URL="",
    BILL_UPDATE_WEBHOOK_TOKEN=TOKEN,
    PWMS_BASE_URL="http://testserver",
)
class BillUpdateQueueTests(TestCase):
    """The sender: building the payload, recording it and queueing the task."""

    @classmethod
    def setUpTestData(cls):
        cls.bill, cls.user = _bill_and_user()

    def test_endpoint_defaults_to_the_receiver_in_this_project(self):
        self.assertEqual(endpoint_url(), "http://testserver/api/v1/bill-updates/")

    def test_configured_url_wins_over_the_default(self):
        with override_settings(
            BILL_UPDATE_ENDPOINT_URL="https://partner.example.org/hook"
        ):
            self.assertEqual(endpoint_url(), "https://partner.example.org/hook")

    def test_no_base_url_means_no_target(self):
        with override_settings(PWMS_BASE_URL=""):
            self.assertEqual(endpoint_url(), "")

    def test_queue_records_a_pending_outbound_row_and_a_task(self):
        log = queue_bill_update(self.bill, EVENT_NEW, actor=self.user)

        self.assertEqual(log.direction, BillUpdateLog.DIRECTION_OUTBOUND)
        self.assertEqual(log.status, BillUpdateLog.STATUS_PENDING)
        self.assertEqual(log.event, EVENT_NEW)
        self.assertEqual(log.bill, self.bill)
        self.assertEqual(log.bill_number, "B 7-2026")
        self.assertEqual(log.actor, self.user)
        self.assertEqual(log.target_url, "http://testserver/api/v1/bill-updates/")
        self.assertIsNotNone(log.update_id)

        # The body carries the snapshot and who made the change.
        self.assertEqual(log.payload["update_id"], str(log.update_id))
        self.assertEqual(log.payload["event"], EVENT_NEW)
        self.assertEqual(log.payload["bill"]["bill_number"], "B 7-2026")
        self.assertEqual(
            log.payload["bill"]["url"],
            f"http://testserver{self.bill.get_absolute_url()}",
        )
        self.assertEqual(log.payload["actor"]["username"], "bill-poster")

        task = Task.objects.get(task_name=TASK_NAME)
        self.assertEqual(json.loads(task.task_params), [[log.pk], {}])

    def test_queue_skips_when_disabled(self):
        with override_settings(BILL_UPDATE_WEBHOOK_ENABLED=False):
            self.assertIsNone(queue_bill_update(self.bill, EVENT_NEW))
        self.assertFalse(BillUpdateLog.objects.exists())
        self.assertFalse(Task.objects.filter(task_name=TASK_NAME).exists())

    def test_queue_skips_when_there_is_no_target(self):
        with override_settings(PWMS_BASE_URL="", BILL_UPDATE_ENDPOINT_URL=""):
            self.assertIsNone(queue_bill_update(self.bill, EVENT_NEW))
        self.assertFalse(BillUpdateLog.objects.exists())

    def test_status_change_payload_carries_the_move(self):
        from_state = self.bill.current_state
        to_state = State.objects.create(
            workflow_type=self.bill.workflow_type,
            name="Referred to committee",
            public_status=PublicStatus.objects.first(),
        )
        transition = Transition.objects.create(
            workflow_type=self.bill.workflow_type,
            name="Refer",
            from_state=from_state,
            to_state=to_state,
        )

        log = queue_bill_update(
            self.bill,
            EVENT_STATUS_CHANGE,
            actor=self.user,
            transition=transition,
            comment="Sent for review.",
            ip_address="10.0.0.9",
        )

        move = log.payload["status_change"]
        self.assertEqual(move["from_state"], from_state.name)
        self.assertEqual(move["to_state"], to_state.name)
        self.assertEqual(move["transition"], "Refer")
        self.assertEqual(move["comment"], "Sent for review.")
        self.assertEqual(move["ip_address"], "10.0.0.9")

    def test_new_and_edit_payloads_have_no_status_change_block(self):
        self.assertNotIn(
            "status_change", queue_bill_update(self.bill, EVENT_NEW).payload
        )
        self.assertNotIn(
            "status_change", queue_bill_update(self.bill, EVENT_EDITS).payload
        )


@override_settings(
    BILL_UPDATE_WEBHOOK_ENABLED=True,
    BILL_UPDATE_ENDPOINT_URL="",
    BILL_UPDATE_WEBHOOK_TOKEN=TOKEN,
    PWMS_BASE_URL="http://testserver",
)
class BillUpdateDeliveryTests(TestCase):
    """The worker: posting a row and recording the outcome."""

    @classmethod
    def setUpTestData(cls):
        cls.bill, cls.user = _bill_and_user()

    def _log(self):
        return queue_bill_update(self.bill, EVENT_NEW, actor=self.user)

    def test_success_marks_the_row_sent(self):
        log = self._log()
        response = httpx.Response(200, text='{"ok": true}')

        with mock_httpx(response):
            returned = deliver_update(log.pk)

        self.assertEqual(returned.pk, log.pk)
        log.refresh_from_db()
        self.assertEqual(log.status, BillUpdateLog.STATUS_SENT)
        self.assertEqual(log.http_status, 200)
        self.assertEqual(log.response_body, '{"ok": true}')
        self.assertEqual(log.attempts, 1)
        self.assertEqual(log.error, "")
        self.assertIsNotNone(log.completed_at)

    def test_success_is_not_posted_twice(self):
        log = self._log()

        with mock_httpx(httpx.Response(200, text="ok")) as post:
            deliver_update(log.pk)
            self.assertIsNone(deliver_update(log.pk))
            self.assertEqual(post.call_count, 1)

    def test_transport_failure_raises_for_retry_and_stays_pending(self):
        log = self._log()

        with (
            mock_httpx(httpx.ConnectError("connection refused")),
            override_settings(MAX_ATTEMPTS=3),
            self.assertRaises(BillUpdateDeliveryError),
        ):
            deliver_update(log.pk)

        log.refresh_from_db()
        self.assertEqual(log.status, BillUpdateLog.STATUS_PENDING)
        self.assertEqual(log.attempts, 1)
        self.assertIn("ConnectError", log.error)

    def test_http_error_is_recorded(self):
        log = self._log()

        with (
            mock_httpx(httpx.Response(500, text="boom")),
            override_settings(MAX_ATTEMPTS=3),
            self.assertRaises(BillUpdateDeliveryError),
        ):
            deliver_update(log.pk)

        log.refresh_from_db()
        self.assertEqual(log.status, BillUpdateLog.STATUS_PENDING)
        self.assertEqual(log.http_status, 500)
        self.assertIn("HTTP 500", log.error)

    def test_last_attempt_marks_the_row_failed_without_raising(self):
        log = self._log()

        with mock_httpx(httpx.ConnectError("down")), override_settings(MAX_ATTEMPTS=1):
            returned = deliver_update(log.pk)

        self.assertEqual(returned.pk, log.pk)
        log.refresh_from_db()
        self.assertEqual(log.status, BillUpdateLog.STATUS_FAILED)
        self.assertIsNotNone(log.completed_at)
        self.assertIn("ConnectError", log.error)

    def test_a_retried_task_delivers_the_queued_row(self):
        log = self._log()

        with (
            mock_httpx(httpx.Response(202, text="accepted")),
            override_settings(BACKGROUND_TASK_RUN_ASYNC=False),
        ):
            for task in Task.objects.filter(task_name=TASK_NAME):
                bg_tasks.run_task(task)

        log.refresh_from_db()
        self.assertEqual(log.status, BillUpdateLog.STATUS_SENT)
        self.assertEqual(log.http_status, 202)


@override_settings(
    BILL_UPDATE_WEBHOOK_ENABLED=True,
    BILL_UPDATE_ENDPOINT_URL="",
    BILL_UPDATE_WEBHOOK_TOKEN=TOKEN,
    PWMS_BASE_URL="http://testserver",
)
class BillUpdateViewHookTests(TestCase):
    """Creating, editing and moving a Bill through the site queues the update."""

    @classmethod
    def setUpTestData(cls):
        # Superuser: the hook is what is under test, not the RBAC chain.
        cls.user = User.objects.create_superuser(
            username="hook-admin", email="hook@example.org", password="pw"
        )
        cls.workflow_type = WorkflowType.objects.get(name="Bill")

    def setUp(self):
        self.client.force_login(self.user)
        # SharePoint filing is not what these tests are about, and the test
        # environment may have a real drive configured.
        patcher = mock.patch("pwms.views._file_bill_folder")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _bill_payload(self, **overrides):
        payload = {
            "workflow_type": self.workflow_type.pk,
            "title": "Hooked bill",
            "description": "",
            "owner": self.user.pk,
            "assigned_to": "",
            "deadline": "",
            "priority": "medium",
            "bill_number": "B 501-2026",
            "short_title": "Hooked",
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

    def _create_bill(self):
        response = self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        self.assertEqual(response.status_code, 302)
        return Bill.objects.get(bill_number="B 501-2026")

    def test_create_queues_a_new_update(self):
        bill = self._create_bill()

        log = BillUpdateLog.objects.get(direction=BillUpdateLog.DIRECTION_OUTBOUND)
        self.assertEqual(log.event, EVENT_NEW)
        self.assertEqual(log.bill_id, bill.pk)
        self.assertEqual(log.payload["bill"]["bill_number"], "B 501-2026")
        self.assertEqual(log.payload["actor"]["username"], "hook-admin")
        self.assertTrue(Task.objects.filter(task_name=TASK_NAME).exists())

    def test_edit_queues_an_edits_update(self):
        bill = self._create_bill()
        BillUpdateLog.objects.all().delete()

        response = self.client.post(
            reverse("pwms:bill_update", args=[bill.public_id]),
            self._bill_payload(
                title="Hooked bill (edited)",
                current_state=bill.current_state.pk,
                priority="high",
            ),
        )

        self.assertEqual(response.status_code, 302)
        log = BillUpdateLog.objects.get(direction=BillUpdateLog.DIRECTION_OUTBOUND)
        self.assertEqual(log.event, EVENT_EDITS)
        self.assertEqual(log.payload["bill"]["title"], "Hooked bill (edited)")
        self.assertEqual(log.payload["bill"]["priority"], "high")

    def test_status_change_queues_a_status_change_update(self):
        bill = self._create_bill()
        BillUpdateLog.objects.all().delete()
        new_state = State.objects.create(
            workflow_type=self.workflow_type,
            name="Referred by the hook test",
            public_status=PublicStatus.objects.first(),
        )
        transition = Transition.objects.create(
            workflow_type=self.workflow_type,
            name="Refer from the hook test",
            from_state=bill.current_state,
            to_state=new_state,
        )

        response = self.client.post(
            reverse("pwms:workflow_transition", args=[bill.public_id]),
            {"transition": transition.pk, "comment": "Please review."},
        )

        self.assertEqual(response.status_code, 302)
        log = BillUpdateLog.objects.get(direction=BillUpdateLog.DIRECTION_OUTBOUND)
        self.assertEqual(log.event, EVENT_STATUS_CHANGE)
        move = log.payload["status_change"]
        self.assertEqual(move["to_state"], "Referred by the hook test")
        self.assertEqual(move["comment"], "Please review.")


@override_settings(
    BILL_UPDATE_WEBHOOK_ENABLED=True,
    BILL_UPDATE_ENDPOINT_URL="",
    BILL_UPDATE_WEBHOOK_TOKEN=TOKEN,
    PWMS_BASE_URL="http://testserver",
)
class NonBillTransitionTests(TestCase):
    """The hook is for Bills only; other instruments queue nothing."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="other-admin", email="other@example.org", password="pw"
        )
        cls.workflow_type = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=cls.workflow_type,
            current_state=cls.workflow_type.get_initial_state(),
            title="Not a bill",
            owner=cls.user,
        )
        cls.transition = Transition.objects.create(
            workflow_type=cls.workflow_type,
            name="Move the report",
            from_state=cls.report.current_state,
            to_state=State.objects.create(
                workflow_type=cls.workflow_type,
                name="Moved by the hook test",
                public_status=PublicStatus.objects.first(),
            ),
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_a_report_transition_queues_no_bill_update(self):
        response = self.client.post(
            reverse("pwms:workflow_transition", args=[self.report.public_id]),
            {"transition": self.transition.pk, "comment": "Moving."},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(BillUpdateLog.objects.exists())
        self.assertFalse(Task.objects.filter(task_name=TASK_NAME).exists())


@override_settings(BILL_UPDATE_WEBHOOK_TOKEN=TOKEN)
class BillUpdateEndpointTests(TestCase):
    """The testing receiver: authentication, receipts and idempotency."""

    @classmethod
    def setUpTestData(cls):
        cls.bill, cls.user = _bill_and_user()

    def _payload(self, **overrides):
        payload = {
            "update_id": str(uuid4()),
            "event": "NEW",
            "occurred_at": timezone.now().isoformat(),
            "bill": {
                "public_id": str(self.bill.public_id),
                "bill_number": self.bill.bill_number,
                "title": self.bill.title,
                "public_status": self.bill.public_status,
            },
            "actor": {
                "public_id": str(self.user.public_id),
                "username": self.user.username,
                "display_name": self.user.display_name,
            },
        }
        payload.update(overrides)
        return payload

    def _post(self, payload, token=TOKEN):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(
            WEBHOOK_URL,
            data=json.dumps(payload),
            content_type="application/json",
            **headers,
        )

    def test_authentication_is_required(self):
        response = self._post(self._payload(), token=None)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(BillUpdateLog.objects.exists())

    def test_a_wrong_token_is_rejected(self):
        response = self._post(self._payload(), token="not-the-token")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(BillUpdateLog.objects.exists())

    def test_an_unset_token_closes_the_receiver(self):
        with override_settings(BILL_UPDATE_WEBHOOK_TOKEN=""):
            response = self._post(self._payload())
        self.assertEqual(response.status_code, 401)

    def test_a_valid_post_is_recorded_and_receipted(self):
        payload = self._payload()

        response = self._post(payload)

        self.assertEqual(response.status_code, 200, response.content)
        receipt = response.json()
        self.assertEqual(receipt["update_id"], payload["update_id"])
        self.assertEqual(receipt["event"], "NEW")
        self.assertFalse(receipt["duplicate"])
        self.assertEqual(receipt["public_status"], self.bill.public_status)

        log = BillUpdateLog.objects.get(direction=BillUpdateLog.DIRECTION_INBOUND)
        self.assertEqual(log.status, BillUpdateLog.STATUS_RECEIVED)
        self.assertEqual(log.bill, self.bill)
        self.assertEqual(log.bill_public_id, self.bill.public_id)
        self.assertEqual(log.event, EVENT_NEW)
        self.assertEqual(log.payload["update_id"], payload["update_id"])
        self.assertEqual(log.payload["bill"]["title"], self.bill.title)
        self.assertEqual(log.target_url, WEBHOOK_URL)

    def test_the_receiver_never_mutates_the_bill(self):
        payload = self._payload()
        payload["bill"]["title"] = "Injected by the webhook"

        response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.title, "Webhook bill")

    def test_a_retried_update_id_returns_the_same_receipt(self):
        payload = self._payload()

        first = self._post(payload)
        second = self._post(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(second.json()["update_id"], first.json()["update_id"])
        self.assertEqual(second.json()["public_status"], first.json()["public_status"])
        self.assertEqual(
            BillUpdateLog.objects.filter(
                direction=BillUpdateLog.DIRECTION_INBOUND
            ).count(),
            1,
        )

    def test_an_unknown_bill_is_rejected_and_recorded(self):
        payload = self._payload()
        payload["bill"]["public_id"] = str(uuid4())

        response = self._post(payload)

        self.assertEqual(response.status_code, 404)
        log = BillUpdateLog.objects.get(direction=BillUpdateLog.DIRECTION_INBOUND)
        self.assertEqual(log.status, BillUpdateLog.STATUS_REJECTED)
        self.assertIsNone(log.bill)
        self.assertIn("Unknown bill", log.error)

    def test_an_unknown_event_is_a_validation_error(self):
        response = self._post(self._payload(event="DELETED"))
        self.assertEqual(response.status_code, 422)
        self.assertFalse(BillUpdateLog.objects.exists())


@override_settings(
    BILL_UPDATE_WEBHOOK_ENABLED=True,
    BILL_UPDATE_ENDPOINT_URL="",
    BILL_UPDATE_WEBHOOK_TOKEN=TOKEN,
    PWMS_BASE_URL="http://testserver",
)
class BillUpdateRejectionEndpointTests(TestCase):
    """The sender's API: logging a rejection posted back by the receiver."""

    @classmethod
    def setUpTestData(cls):
        cls.bill, cls.user = _bill_and_user()
        cls.original = queue_bill_update(cls.bill, EVENT_NEW, actor=cls.user)

    def _payload(self, **overrides):
        payload = {
            "rejection_id": str(uuid4()),
            "update_id": str(self.original.update_id),
            "event": EVENT_NEW,
            "occurred_at": timezone.now().isoformat(),
            "bill": {
                "public_id": str(self.bill.public_id),
                "bill_number": self.bill.bill_number,
                "title": self.bill.title,
            },
            "reason": "The committee stage is missing.",
            "rejected_at": timezone.now().isoformat(),
            "rejected_by": {
                "username": "reviewer",
                "display_name": "Reviewer",
            },
        }
        payload.update(overrides)
        return payload

    def _post(self, payload, token=TOKEN):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(
            REJECTION_URL,
            data=json.dumps(payload),
            content_type="application/json",
            **headers,
        )

    def test_authentication_is_required(self):
        response = self._post(self._payload(), token=None)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(BillUpdateLog.objects.filter(event=EVENT_REJECTION).exists())

    def test_a_rejection_is_logged_and_stamps_the_original_message(self):
        payload = self._payload()

        response = self._post(payload)

        self.assertEqual(response.status_code, 200, response.content)
        receipt = response.json()
        self.assertEqual(receipt["rejection_id"], payload["rejection_id"])
        self.assertEqual(receipt["update_id"], payload["update_id"])
        self.assertFalse(receipt["duplicate"])

        logged = BillUpdateLog.objects.get(
            update_id=payload["rejection_id"],
            direction=BillUpdateLog.DIRECTION_INBOUND,
        )
        self.assertEqual(logged.event, EVENT_REJECTION)
        self.assertEqual(logged.status, BillUpdateLog.STATUS_RECEIVED)
        self.assertEqual(logged.bill, self.bill)
        self.assertEqual(logged.rejection_reason, "The committee stage is missing.")
        self.assertEqual(logged.rejected_by_name, "Reviewer")
        self.assertEqual(logged.target_url, REJECTION_URL)

        original = BillUpdateLog.objects.get(pk=self.original.pk)
        self.assertEqual(original.status, BillUpdateLog.STATUS_PENDING)
        self.assertEqual(original.rejection_reason, "The committee stage is missing.")
        self.assertEqual(original.rejected_by_name, "Reviewer")
        self.assertIsNotNone(original.rejected_at)

    def test_a_retried_rejection_is_idempotent(self):
        payload = self._payload()

        first = self._post(payload)
        second = self._post(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(
            BillUpdateLog.objects.filter(
                direction=BillUpdateLog.DIRECTION_INBOUND,
                event=EVENT_REJECTION,
            ).count(),
            1,
        )

    def test_a_rejection_for_an_unknown_update_is_still_logged(self):
        payload = self._payload(update_id=str(uuid4()))

        response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            BillUpdateLog.objects.filter(
                update_id=payload["rejection_id"],
                direction=BillUpdateLog.DIRECTION_INBOUND,
            ).exists()
        )

    def test_an_unknown_event_is_a_validation_error(self):
        response = self._post(self._payload(event=EVENT_REJECTION))
        self.assertEqual(response.status_code, 422)
        self.assertFalse(BillUpdateLog.objects.filter(event=EVENT_REJECTION).exists())


@override_settings(
    BILL_UPDATE_WEBHOOK_ENABLED=True,
    BILL_UPDATE_ENDPOINT_URL="",
    BILL_UPDATE_WEBHOOK_TOKEN=TOKEN,
    PWMS_BASE_URL="http://testserver",
)
class BillTrackerPageTests(TestCase):
    """The tracker page: the list, the stepper and the webhook activity feed."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="tracker-admin", email="tracker@example.org", password="pw"
        )
        cls.bill, _owner = _bill_and_user()
        cls.next_state = State.objects.create(
            workflow_type=cls.bill.workflow_type,
            name="Referred by tracker test",
            public_status=PublicStatus.objects.get(
                name="Under Parliamentary Consideration"
            ),
            order=15,
        )
        cls.transition = Transition.objects.create(
            workflow_type=cls.bill.workflow_type,
            name="Refer from tracker test",
            from_state=cls.bill.current_state,
            to_state=cls.next_state,
        )

    def setUp(self):
        # Class-level model instances are shared between tests; a transition or
        # an edit in one test must not leak into the next.
        self.bill.refresh_from_db()
        self.client.force_login(self.user)

    def _row(self, response, bill_number="B 7-2026"):
        for row in response.context["rows"]:
            if row["bill"].bill_number == bill_number:
                return row
        raise AssertionError(f"No tracker row for {bill_number}")

    def _stepper_statuses(self, response, bill_number="B 7-2026"):
        row = self._row(response, bill_number)
        return {step["name"]: step["status"] for step in row["steps"]}

    def _post_to_receiver(self, payload, url=WEBHOOK_URL):
        return self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )

    def _receive_and_approve(self, outbound):
        received = self._post_to_receiver(outbound.payload)
        self.assertEqual(received.status_code, 200, received.content)
        response = self.client.post(
            reverse("pwms:bill_update_approve", args=[outbound.update_id])
        )
        self.assertEqual(response.status_code, 302)

    def test_page_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("pwms:bill_tracker"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/pwms/login/", response["Location"])

    def test_review_and_approve_require_login(self):
        outbound = queue_bill_update(self.bill, EVENT_NEW, actor=self.user)
        self._post_to_receiver(outbound.payload)
        self.client.logout()

        review = self.client.get(
            reverse("pwms:bill_update_review", args=[outbound.update_id])
        )
        self.assertEqual(review.status_code, 302)
        approve = self.client.post(
            reverse("pwms:bill_update_approve", args=[outbound.update_id])
        )
        self.assertEqual(approve.status_code, 302)
        reject = self.client.post(
            reverse("pwms:bill_update_reject", args=[outbound.update_id])
        )
        self.assertEqual(reject.status_code, 302)
        inbound = BillUpdateLog.objects.get(direction=BillUpdateLog.DIRECTION_INBOUND)
        self.assertEqual(inbound.status, BillUpdateLog.STATUS_RECEIVED)

    def test_page_lists_the_bill_and_marks_its_current_step(self):
        response = self.client.get(reverse("pwms:bill_tracker"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.bill.bill_number)
        self.assertContains(response, "bill-stepper__step--current")
        statuses = self._stepper_statuses(response)
        self.assertEqual(statuses["Introduced"], "current")
        # The stepper is the public vocabulary, not the internal state names.
        self.assertIn("Under Parliamentary Consideration", statuses)
        self.assertNotIn("Referred to Committee", statuses)

    def test_htmx_request_returns_only_the_live_fragment(self):
        response = self.client.get(reverse("pwms:bill_tracker"), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.bill.bill_number)
        self.assertContains(response, "bill-stepper")
        # The fragment is what HTMX swaps in, not another whole document.
        self.assertNotContains(response, "<html")

    def test_a_status_change_waits_for_review_then_advances_the_stepper(self):
        # A new record must be accepted first; then it has an accepted baseline
        # for the next change to be measured against.
        new_outbound = queue_bill_update(self.bill, EVENT_NEW, actor=self.user)
        self._receive_and_approve(new_outbound)

        response = self.client.post(
            reverse("pwms:workflow_transition", args=[self.bill.public_id]),
            {"transition": self.transition.pk, "comment": "Please review."},
        )
        self.assertEqual(response.status_code, 302)

        # The hook queued the outbound row; post its payload as the worker would.
        outbound = BillUpdateLog.objects.get(
            direction=BillUpdateLog.DIRECTION_OUTBOUND, event=EVENT_STATUS_CHANGE
        )
        received = self._post_to_receiver(outbound.payload)
        self.assertEqual(received.status_code, 200, received.content)

        # The accepted row is untouched until the update is approved.
        page = self.client.get(reverse("pwms:bill_tracker"))
        statuses = self._stepper_statuses(page)
        self.assertEqual(statuses["Introduced"], "current")
        self.assertEqual(statuses["Under Parliamentary Consideration"], "todo")
        self.assertContains(page, "Incoming status changed")
        self.assertContains(page, "Review Status changed")

        # The popup highlights the move and the comment.
        review = self.client.get(
            reverse("pwms:bill_update_review", args=[outbound.update_id])
        )
        self.assertContains(review, "Status change")
        self.assertContains(review, "Introduced")
        self.assertContains(review, "Under Parliamentary Consideration")
        self.assertContains(review, "Referred by tracker test")
        self.assertContains(review, "Please review.")
        self.assertContains(review, "Approve and apply")

        self.client.post(reverse("pwms:bill_update_approve", args=[outbound.update_id]))
        inbound = BillUpdateLog.objects.get(
            direction=BillUpdateLog.DIRECTION_INBOUND, update_id=outbound.update_id
        )
        self.assertEqual(inbound.status, BillUpdateLog.STATUS_APPROVED)
        self.assertEqual(inbound.reviewed_by, self.user)
        self.assertIsNotNone(inbound.reviewed_at)

        page = self.client.get(reverse("pwms:bill_tracker"))
        statuses = self._stepper_statuses(page)
        self.assertEqual(statuses["Introduced"], "done")
        self.assertEqual(statuses["Under Parliamentary Consideration"], "current")
        self.assertContains(page, "Internal stage: Referred by tracker test")
        self.assertContains(page, "Accepted status changed")
        self.assertContains(page, "Approved")
        self.assertNotContains(page, "Review Status changed")

        # An approved update cannot be reviewed again.
        replay = self.client.get(
            reverse("pwms:bill_update_review", args=[outbound.update_id])
        )
        self.assertContains(replay, "already approved")

    def test_a_new_bill_waits_for_approval_then_is_accepted(self):
        new_bill = Bill.objects.create(
            workflow_type=self.bill.workflow_type,
            current_state=self.bill.workflow_type.get_initial_state(),
            bill_number="B 8-2026",
            title="Brand new bill",
            owner=self.user,
        )
        outbound = queue_bill_update(new_bill, EVENT_NEW, actor=self.user)
        received = self._post_to_receiver(outbound.payload)
        self.assertEqual(received.status_code, 200, received.content)

        page = self.client.get(reverse("pwms:bill_tracker"))
        self.assertContains(page, "B 8-2026")
        self.assertContains(page, "Brand new bill")
        self.assertContains(page, "Review New bill")
        self.assertIsNotNone(self._row(page, "B 8-2026")["pending"])

        # No earlier accepted snapshot: the popup shows the whole record.
        review = self.client.get(
            reverse("pwms:bill_update_review", args=[outbound.update_id])
        )
        self.assertContains(review, "No earlier accepted snapshot")
        self.assertContains(review, "Brand new bill")
        self.assertContains(review, "Approve and apply")

        self.client.post(reverse("pwms:bill_update_approve", args=[outbound.update_id]))
        page = self.client.get(reverse("pwms:bill_tracker"))
        row = self._row(page, "B 8-2026")
        self.assertIsNone(row["pending"])
        self.assertEqual(row["accepted_event"].event, EVENT_NEW)
        self.assertContains(page, "Accepted new bill")

    def test_an_edit_is_reviewed_before_it_replaces_the_row(self):
        # Establish an accepted snapshot with an approved NEW post.
        new_outbound = queue_bill_update(self.bill, EVENT_NEW, actor=self.user)
        self._receive_and_approve(new_outbound)

        # The source moves on, and the hook posts the edit.
        self.bill.title = "Edited title"
        self.bill.save(update_fields=["title", "updated_at"])
        edit_outbound = queue_bill_update(self.bill, EVENT_EDITS, actor=self.user)
        received = self._post_to_receiver(edit_outbound.payload)
        self.assertEqual(received.status_code, 200, received.content)

        # The row still shows the accepted title; the edit waits for review.
        page = self.client.get(reverse("pwms:bill_tracker"))
        row = self._row(page)
        self.assertEqual(row["title"], "Webhook bill")
        self.assertEqual(row["pending"].event, EVENT_EDITS)
        self.assertContains(page, "Review Bill edited")

        review = self.client.get(
            reverse("pwms:bill_update_review", args=[edit_outbound.update_id])
        )
        self.assertContains(review, "Title")
        self.assertContains(review, "Webhook bill")
        self.assertContains(review, "Edited title")

        self.client.post(
            reverse("pwms:bill_update_approve", args=[edit_outbound.update_id])
        )
        page = self.client.get(reverse("pwms:bill_tracker"))
        self.assertEqual(self._row(page)["title"], "Edited title")
        self.assertIsNone(self._row(page)["pending"])

    def test_rejecting_an_update_records_it_and_queues_the_callback(self):
        outbound = queue_bill_update(self.bill, EVENT_NEW, actor=self.user)
        self._post_to_receiver(outbound.payload)
        inbound = BillUpdateLog.objects.get(
            direction=BillUpdateLog.DIRECTION_INBOUND, update_id=outbound.update_id
        )

        response = self.client.post(
            reverse("pwms:bill_update_reject", args=[outbound.update_id]),
            {"reason": "Wrong B-number."},
        )
        self.assertEqual(response.status_code, 302)

        inbound.refresh_from_db()
        self.assertEqual(inbound.status, BillUpdateLog.STATUS_REJECTED)
        self.assertEqual(inbound.rejection_reason, "Wrong B-number.")
        self.assertEqual(inbound.reviewed_by, self.user)
        self.assertIsNotNone(inbound.rejected_at)

        # The decision leaves as its own message, queued like any outbound post.
        callback = BillUpdateLog.objects.get(
            direction=BillUpdateLog.DIRECTION_OUTBOUND, event=EVENT_REJECTION
        )
        self.assertEqual(callback.status, BillUpdateLog.STATUS_PENDING)
        self.assertEqual(callback.payload["update_id"], str(outbound.update_id))
        self.assertEqual(callback.payload["reason"], "Wrong B-number.")
        self.assertEqual(callback.payload["rejection_id"], str(callback.update_id))
        self.assertIn("/bill-updates/rejections/", callback.target_url)
        self.assertTrue(Task.objects.filter(task_name=TASK_NAME).exists())

        page = self.client.get(reverse("pwms:bill_tracker"))
        self.assertContains(page, "Rejected new bill")
        self.assertContains(page, "Wrong B-number.")
        self.assertNotContains(page, "Review New bill")

    def test_the_rejection_callback_is_logged_by_the_api(self):
        outbound = queue_bill_update(self.bill, EVENT_NEW, actor=self.user)
        self._post_to_receiver(outbound.payload)
        self.client.post(
            reverse("pwms:bill_update_reject", args=[outbound.update_id]),
            {"reason": "Duplicate of B 6-2026."},
        )

        callback = BillUpdateLog.objects.get(
            direction=BillUpdateLog.DIRECTION_OUTBOUND, event=EVENT_REJECTION
        )
        received = self._post_to_receiver(callback.payload, url=REJECTION_URL)
        self.assertEqual(received.status_code, 200, received.content)
        self.assertFalse(received.json()["duplicate"])

        # The sender's original message now carries the decision.
        outbound.refresh_from_db()
        self.assertEqual(outbound.rejection_reason, "Duplicate of B 6-2026.")
        self.assertEqual(outbound.rejected_by_name, "tracker-admin")
        self.assertIsNotNone(outbound.rejected_at)

        # And the callback itself is a logged inbound message.
        logged = BillUpdateLog.objects.get(
            direction=BillUpdateLog.DIRECTION_INBOUND,
            event=EVENT_REJECTION,
        )
        self.assertEqual(logged.update_id, callback.update_id)
        self.assertEqual(logged.bill, self.bill)
        self.assertEqual(logged.rejection_reason, "Duplicate of B 6-2026.")

        page = self.client.get(reverse("pwms:bill_tracker"))
        self.assertContains(page, "Logged")

    def test_approving_out_of_order_is_refused(self):
        first = queue_bill_update(self.bill, EVENT_NEW, actor=self.user)
        self._post_to_receiver(first.payload)

        # A second change arrives before the first is reviewed.
        self.bill.title = "Second state"
        self.bill.save(update_fields=["title", "updated_at"])
        second = queue_bill_update(self.bill, EVENT_EDITS, actor=self.user)
        self._post_to_receiver(second.payload)

        response = self.client.post(
            reverse("pwms:bill_update_approve", args=[second.update_id])
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            BillUpdateLog.objects.get(
                direction=BillUpdateLog.DIRECTION_INBOUND,
                update_id=second.update_id,
            ).status,
            BillUpdateLog.STATUS_RECEIVED,
        )

        # The earlier post still approves, and the later one follows it.
        self.client.post(reverse("pwms:bill_update_approve", args=[first.update_id]))
        self.client.post(reverse("pwms:bill_update_approve", args=[second.update_id]))
        self.assertEqual(
            BillUpdateLog.objects.get(
                direction=BillUpdateLog.DIRECTION_INBOUND,
                update_id=second.update_id,
            ).status,
            BillUpdateLog.STATUS_APPROVED,
        )


def mock_httpx(result):
    """Patch ``httpx.post`` in the delivery service with a canned outcome.

    An exception instance is raised instead, which is how transport failures
    reach the retry path.
    """
    if isinstance(result, Exception):
        return mock.patch("pwms.services.bill_updates.httpx.post", side_effect=result)
    return mock.patch("pwms.services.bill_updates.httpx.post", return_value=result)
