"""The Referrals tab of a workflow detail page (raise / answer / withdraw)."""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from .models import (
    Group,
    GroupMembership,
    InternationalResolution,
    Role,
    State,
    WorkflowType,
)


class ReferralViewTests(TestCase):
    """Raising, answering and withdrawing referrals from the record's page."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="ref-owner", password="pw")
        cls.outsider = User.objects.create_user(username="ref-outsider", password="pw")
        cls.wt = WorkflowType.objects.get(name="International Resolution")
        cls.committee = Group.objects.create(
            name="UI Referrals Committee", group_type="portfolio_committee"
        )
        cls.resolution = InternationalResolution.objects.create(
            workflow_type=cls.wt,
            current_state=cls.wt.get_initial_state(),
            resolution_number="IR-UI-1",
            title="Referral UI test",
            owner=cls.owner,
        )
        cls.content_type = ContentType.objects.get_for_model(InternationalResolution)

    def setUp(self):
        self.client.force_login(self.owner)

    # -- helpers ------------------------------------------------------------
    def _post_create(self, resolution=None):
        resolution = resolution or self.resolution
        return self.client.post(
            reverse("pwms:referral_create"),
            {
                "content_type": f"pwms.{InternationalResolution._meta.model_name}",
                "object_id": resolution.pk,
                "referred_to": self.committee.pk,
                "notes": "Please consider",
            },
        )

    def _referral(self):
        return self.resolution.refer(self.committee, referred_by=self.owner)

    def _locked_resolution(self):
        """A resolution in a state that forbids referrals, owned by the editor."""
        wt = WorkflowType.objects.create(name="UI Locked Referral Type")
        state = State.objects.create(
            workflow_type=wt, name="Locked", is_initial=True, allows_referrals=False
        )
        return InternationalResolution.objects.create(
            workflow_type=wt,
            current_state=state,
            resolution_number="IR-UI-LOCKED",
            title="Locked",
            owner=self.owner,
        )

    def _detail_url(self, resolution=None):
        resolution = resolution or self.resolution
        return reverse(
            "pwms:international_resolution_detail",
            kwargs={"public_id": resolution.public_id},
        )

    # -- raising ------------------------------------------------------------
    def test_the_tab_offers_the_add_form_to_an_editor(self):
        response = self.client.get(self._detail_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add Referral")
        # The group field is a search picker, not a <select> of every group.
        self.assertContains(response, reverse("pwms:group_search"))

    def test_creating_a_referral_from_the_ui(self):
        response = self._post_create()
        self.assertEqual(response.status_code, 200)
        referral = self.resolution.referrals().get()
        self.assertEqual(referral.referred_to, self.committee)
        self.assertEqual(referral.referred_by, self.owner)
        self.assertEqual(referral.notes, "Please consider")
        # The refreshed panel shows the new row.
        self.assertContains(response, self.committee.name)

    def test_creating_a_referral_needs_the_edit_right(self):
        self.client.force_login(self.outsider)
        response = self._post_create()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.resolution.referrals().exists())

    def test_creating_a_referral_is_refused_in_a_locked_state(self):
        resolution = self._locked_resolution()
        response = self._post_create(resolution=resolution)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not allowed")
        self.assertFalse(resolution.referrals().exists())

    def test_the_add_button_is_hidden_when_the_state_forbids_referrals(self):
        resolution = self._locked_resolution()
        response = self.client.get(self._detail_url(resolution))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Add Referral")

    # -- answering and withdrawing ------------------------------------------
    def test_responding_from_the_ui(self):
        referral = self._referral()
        response = self.client.post(
            reverse("pwms:referral_respond", kwargs={"public_id": referral.public_id}),
            {"document_url": "https://sp.example/answer.pdf", "notes": "Agreed"},
        )
        self.assertEqual(response.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.status, "responded")
        self.assertEqual(referral.responded_by, self.owner)
        self.assertEqual(referral.response_notes, "Agreed")
        self.assertEqual(
            referral.response_document_url, "https://sp.example/answer.pdf"
        )

    def test_a_closed_referral_cannot_be_answered(self):
        referral = self._referral()
        referral.recall(recalled_by=self.owner)
        response = self.client.post(
            reverse("pwms:referral_respond", kwargs={"public_id": referral.public_id})
        )
        self.assertEqual(response.status_code, 403)
        referral.refresh_from_db()
        self.assertEqual(referral.status, "recalled")

    def test_withdrawing_from_the_ui(self):
        referral = self._referral()
        response = self.client.post(
            reverse("pwms:referral_recall", kwargs={"public_id": referral.public_id}),
            {"reason": "No longer needed"},
        )
        self.assertEqual(response.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.status, "recalled")
        self.assertEqual(referral.recalled_by, self.owner)
        self.assertEqual(referral.recall_reason, "No longer needed")

    def test_a_member_of_the_referred_group_may_answer(self):
        member = get_user_model().objects.create_user(
            username="ref-member", password="pw"
        )
        GroupMembership.objects.create(
            user=member,
            group=self.committee,
            role=Role.objects.create(name="UI Answerer"),
        )
        referral = self._referral()
        self.client.force_login(member)

        response = self.client.post(
            reverse("pwms:referral_respond", kwargs={"public_id": referral.public_id}),
            {"notes": "Committee answer"},
        )

        self.assertEqual(response.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.status, "responded")
        self.assertEqual(referral.responded_by, member)

    def test_an_outsider_may_not_answer_or_withdraw(self):
        referral = self._referral()
        self.client.force_login(self.outsider)
        for name in ("referral_respond", "referral_recall"):
            response = self.client.post(
                reverse(f"pwms:{name}", kwargs={"public_id": referral.public_id})
            )
            self.assertEqual(response.status_code, 403)
        referral.refresh_from_db()
        self.assertEqual(referral.status, "open")

    # -- reading ------------------------------------------------------------
    def test_the_tab_lists_answers_and_withdrawals(self):
        answered = self._referral()
        answered.respond(
            responded_by=self.owner,
            notes="Committee supported it",
            document_url="https://sp.example/response.pdf",
        )
        withdrawn = self.resolution.refer(self.committee, referred_by=self.owner)
        withdrawn.recall(recalled_by=self.owner, reason="Superseded")

        response = self.client.get(self._detail_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Committee supported it")
        self.assertContains(response, "https://sp.example/response.pdf")
        self.assertContains(response, "Superseded")
        self.assertNotContains(response, "This record has not been referred.")
