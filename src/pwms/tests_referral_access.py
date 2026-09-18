"""Access a referral grants the referred group: contribute while open, read after.

A referral is the record of another group being asked to consider a workflow, and
it is also what gives that group a foothold on the record — see
``AbstractLegislativeWorkflow.sync_referral_access``. These tests pin what that
foothold is: **view and edit** while a referral to it is open, **view** alone once
every referral has closed (for audit and reporting) and no ``transition`` — moving
a record on stays with the owning group. They also pin what the grant does *not*
buy: authority over the referral registers, so the group a matter was referred to
cannot withdraw the referral it is answering or raise further ones.
"""

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
    WorkflowGroupAccess,
    WorkflowType,
)
from .services.permissions import (
    DELETE,
    EDIT,
    TRANSITION,
    VIEW,
    resolve,
    visible_instances,
)


class ReferralAccessTests(TestCase):
    """What raising, closing, moving and deleting a referral does to access."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="ra-owner", password="pw")
        cls.outsider = User.objects.create_user(username="ra-outsider", password="pw")
        cls.wt = WorkflowType.objects.get(name="International Resolution")
        cls.committee = Group.objects.create(
            name="RA Committee", group_type="portfolio_committee"
        )
        cls.other_committee = Group.objects.create(
            name="RA Other Committee", group_type="portfolio_committee"
        )
        cls.role = Role.objects.create(name="RA Member")
        cls.member = User.objects.create_user(username="ra-member", password="pw")
        GroupMembership.objects.create(
            user=cls.member, group=cls.committee, role=cls.role
        )
        cls.resolution = InternationalResolution.objects.create(
            workflow_type=cls.wt,
            current_state=cls.wt.get_initial_state(),
            resolution_number="IR-RA-1",
            title="Referral access",
            owner=cls.owner,
        )

    # -- helpers ------------------------------------------------------------
    def _access(self, group=None, resolution=None):
        return WorkflowGroupAccess.objects.filter(
            content_type=ContentType.objects.get_for_model(InternationalResolution),
            object_id=(resolution or self.resolution).pk,
            group=group or self.committee,
        ).first()

    def _refer(self, group=None, resolution=None):
        return (resolution or self.resolution).refer(
            group or self.committee, referred_by=self.owner
        )

    def _detail_url(self, resolution=None):
        resolution = resolution or self.resolution
        return reverse(
            "pwms:international_resolution_detail",
            kwargs={"public_id": resolution.public_id},
        )

    def _office_resolution(self):
        """
        A record of the office's own type, plus an officer who edits it.

        The officer's edit is organic — their group owns the type — while the
        referrals raised on this record go to ``self.committee``, so the officer is
        an editor of the record and nothing else.
        """
        office = Group.objects.create(name="RA Office", group_type="division")
        officer_role = Role.objects.create(name="RA Officer")
        officer = get_user_model().objects.create_user(
            username="ra-officer", password="pw"
        )
        GroupMembership.objects.create(user=officer, group=office, role=officer_role)
        office_type = WorkflowType.objects.create(name="RA Office Type", group=office)
        state = State.objects.create(
            workflow_type=office_type, name="Open", is_initial=True
        )
        resolution = InternationalResolution.objects.create(
            workflow_type=office_type,
            current_state=state,
            resolution_number="IR-RA-OFFICE",
            title="Referred by a colleague",
            owner=self.owner,
        )
        return resolution, officer

    # -- the grant ----------------------------------------------------------
    def test_referring_grants_the_group_view_and_edit_but_not_transition(self):
        self._refer()

        access = self._access()
        self.assertTrue(access.can_view)
        self.assertTrue(access.can_edit)
        # The group is asked to consider the record and reply to the referral; it is
        # not handed the state machine. `transition` offers *every* move out of the
        # current state — for a Bill referred to committee, even its withdrawal — so
        # it stays with the owning group (see Functional Design §4).
        self.assertFalse(access.can_transition)
        self.assertFalse(access.can_delete)
        self.assertFalse(access.is_primary)
        self.assertTrue(access.via_referral)
        self.assertTrue(access.referral_raised_edit)

        self.assertTrue(resolve(self.member, self.resolution, VIEW))
        self.assertTrue(resolve(self.member, self.resolution, EDIT))
        self.assertFalse(resolve(self.member, self.resolution, TRANSITION))
        self.assertFalse(resolve(self.member, self.resolution, DELETE))

    def test_the_referred_group_may_open_the_record(self):
        # The point of the grant: the committee can reach the page it answers from.
        self._refer()
        self.client.force_login(self.member)

        response = self.client.get(self._detail_url())

        self.assertEqual(response.status_code, 200)

    def test_the_grant_also_lists_the_record_for_the_group(self):
        self._refer()

        visible = visible_instances(self.member, InternationalResolution.objects.all())

        self.assertIn(self.resolution, visible)

    def test_a_stranger_gains_nothing(self):
        self._refer()

        for action in (VIEW, EDIT, TRANSITION, DELETE):
            self.assertFalse(resolve(self.outsider, self.resolution, action))

    # -- closing the referral ----------------------------------------------
    def test_answering_keeps_view_and_gives_back_edit(self):
        referral = self._refer()

        referral.respond(responded_by=self.member)

        access = self._access()
        self.assertTrue(access.can_view)
        self.assertFalse(access.can_edit)
        self.assertFalse(access.can_transition)
        # The row is kept, still marked as the referral's, so the group can see
        # what it was asked about.
        self.assertTrue(access.via_referral)
        self.assertTrue(resolve(self.member, self.resolution, VIEW))
        self.assertFalse(resolve(self.member, self.resolution, EDIT))

    def test_the_record_stays_reachable_after_the_referral_closes(self):
        referral = self._refer()
        referral.respond(responded_by=self.member)
        self.client.force_login(self.member)

        response = self.client.get(self._detail_url())

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            self.resolution,
            visible_instances(self.member, InternationalResolution.objects.all()),
        )

    def test_recalling_narrows_the_grant_too(self):
        referral = self._refer()

        referral.recall(recalled_by=self.owner, reason="Superseded")

        access = self._access()
        self.assertTrue(access.can_view)
        self.assertFalse(access.can_edit)

    def test_expiring_narrows_the_grant_too(self):
        referral = self._refer()

        referral.mark_expired()

        access = self._access()
        self.assertTrue(access.can_view)
        self.assertFalse(access.can_edit)

    def test_another_open_referral_keeps_the_group_working(self):
        first = self._refer()
        self._refer()

        first.respond(responded_by=self.member)

        access = self._access()
        self.assertTrue(access.can_edit)
        self.assertFalse(access.can_transition)

        # Only the last referral closing hands the capability back.
        self.resolution.referrals().filter(status="open").get().recall(
            recalled_by=self.owner
        )
        access.refresh_from_db()
        self.assertTrue(access.can_view)
        self.assertFalse(access.can_edit)

    # -- grants the group already held -------------------------------------
    def test_an_organic_viewer_grant_is_widened_then_restored(self):
        self.wt.viewer_groups.add(self.committee)
        organic = InternationalResolution.objects.create(
            workflow_type=self.wt,
            current_state=self.wt.get_initial_state(),
            resolution_number="IR-RA-VIEWER",
            title="Viewer group referred",
            owner=self.owner,
        )
        self.assertFalse(self._access(resolution=organic).can_edit)

        referral = self._refer(resolution=organic)
        widened = self._access(resolution=organic)
        self.assertTrue(widened.can_edit)
        self.assertTrue(widened.referral_raised_edit)
        # The row is the viewer grant, not a referral grant — and it never gains
        # the state machine, because no referral grants that.
        self.assertFalse(widened.via_referral)
        self.assertFalse(widened.can_transition)

        referral.respond(responded_by=self.member)
        widened.refresh_from_db()
        self.assertTrue(widened.can_view)
        self.assertFalse(widened.can_edit)
        self.assertIsNotNone(self._access(resolution=organic))

    def test_the_owning_groups_edit_and_transition_rights_are_never_withdrawn(self):
        # A type owned by the committee itself: materialisation gave the group edit
        # and transition, so a referral closing must not take either away.
        owned_type = WorkflowType.objects.create(
            name="RA Owned Type", group=self.committee
        )
        state = State.objects.create(
            workflow_type=owned_type, name="Open", is_initial=True
        )
        owned = InternationalResolution.objects.create(
            workflow_type=owned_type,
            current_state=state,
            resolution_number="IR-RA-OWNED",
            title="Owned and referred",
            owner=self.owner,
        )
        referral = self._refer(resolution=owned)

        referral.respond(responded_by=self.member)

        access = self._access(resolution=owned)
        self.assertTrue(access.can_edit)
        self.assertTrue(access.can_transition)
        self.assertFalse(access.referral_raised_edit)

    # -- moving and removing the referral ----------------------------------
    def test_reassigning_the_referral_moves_the_grant(self):
        referral = self._refer()

        referral.referred_to = self.other_committee
        referral.save()

        # The committee it left held only the referral grant, so that goes with it.
        self.assertIsNone(self._access())
        moved = self._access(group=self.other_committee)
        self.assertTrue(moved.can_view)
        self.assertTrue(moved.can_edit)
        self.assertFalse(moved.can_transition)

    def test_deleting_the_referral_drops_a_referral_only_grant(self):
        referral = self._refer()

        referral.delete()

        self.assertIsNone(self._access())

    def test_deleting_the_referral_keeps_an_organic_grant(self):
        self.wt.viewer_groups.add(self.committee)
        organic = InternationalResolution.objects.create(
            workflow_type=self.wt,
            current_state=self.wt.get_initial_state(),
            resolution_number="IR-RA-VIEWER-DEL",
            title="Viewer group referred and dropped",
            owner=self.owner,
        )
        referral = self._refer(resolution=organic)

        referral.delete()

        access = self._access(resolution=organic)
        self.assertIsNotNone(access)
        self.assertTrue(access.can_view)
        self.assertFalse(access.can_edit)

    # -- what the grant does not buy ---------------------------------------
    def test_ignoring_the_referral_grant_answers_without_it(self):
        self._refer()

        self.assertTrue(resolve(self.member, self.resolution, EDIT))
        # The same question with the referral's contribution left out: nothing else
        # grants the member anything, so edit and view both fall away.
        self.assertFalse(
            resolve(self.member, self.resolution, EDIT, ignore_referral_grants=True)
        )
        self.assertFalse(
            resolve(self.member, self.resolution, VIEW, ignore_referral_grants=True)
        )

    def test_a_referred_member_may_not_answer_another_groups_referral(self):
        # Their own referral being open means the grant carries edit — but edit
        # borrowed from a referral is not "an editor of the record", or one
        # committee's secretary could answer a neighbour's referral (which they
        # could: the borrowed edit satisfied the on-behalf path).
        self._refer()
        theirs = self._refer(group=self.other_committee)
        self.client.force_login(self.member)

        response = self.client.post(
            reverse("pwms:referral_respond", kwargs={"public_id": theirs.public_id}),
            {"notes": "Not ours to answer"},
        )

        self.assertEqual(response.status_code, 403)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, "open")

    def test_a_referred_member_may_still_answer_their_own(self):
        referral = self._refer()
        self.client.force_login(self.member)

        response = self.client.post(
            reverse("pwms:referral_respond", kwargs={"public_id": referral.public_id}),
            {"notes": "Ours to answer"},
        )

        self.assertEqual(response.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.status, "responded")
        self.assertEqual(referral.responded_by, self.member)

    def test_the_respond_form_is_offered_only_for_their_own_referral(self):
        mine = self._refer()
        theirs = self._refer(group=self.other_committee)
        self.client.force_login(self.member)

        response = self.client.get(self._detail_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("pwms:referral_respond", kwargs={"public_id": mine.public_id}),
        )
        self.assertNotContains(
            response,
            reverse("pwms:referral_respond", kwargs={"public_id": theirs.public_id}),
        )

    def test_a_referred_member_may_not_withdraw_the_referral(self):
        referral = self._refer()
        self.client.force_login(self.member)

        response = self.client.post(
            reverse("pwms:referral_recall", kwargs={"public_id": referral.public_id}),
            {"reason": "Not our concern"},
        )

        self.assertEqual(response.status_code, 403)
        referral.refresh_from_db()
        self.assertEqual(referral.status, "open")

    def test_a_referred_member_may_not_raise_another_referral(self):
        referral = self._refer()
        self.client.force_login(self.member)

        response = self.client.post(
            reverse("pwms:referral_create"),
            {
                "content_type": f"pwms.{InternationalResolution._meta.model_name}",
                "object_id": self.resolution.pk,
                "referred_to": self.other_committee.pk,
                "notes": "Over to you",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.resolution.referrals().count(), 1)
        referral.refresh_from_db()
        self.assertEqual(referral.status, "open")

    def test_the_add_referral_button_is_hidden_from_the_referred_group(self):
        self._refer()
        self.client.force_login(self.member)

        response = self.client.get(self._detail_url())

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Add Referral")

    def test_an_editor_of_the_record_may_still_withdraw_a_referral(self):
        # Only what a referral conferred is left out of the check, not everything
        # the reader holds: the record's own editors keep the referral registers.
        resolution, officer = self._office_resolution()
        referral = resolution.refer(self.committee, referred_by=self.owner)
        self.client.force_login(officer)

        response = self.client.post(
            reverse("pwms:referral_recall", kwargs={"public_id": referral.public_id}),
            {"reason": "Superseded"},
        )

        self.assertEqual(response.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.status, "recalled")

    def test_an_editor_of_the_record_may_answer_on_a_committees_behalf(self):
        # Answering is not restricted to the committee: the office that runs the
        # record may record the response for it, as it always could.
        resolution, officer = self._office_resolution()
        referral = resolution.refer(self.committee, referred_by=self.owner)
        self.client.force_login(officer)

        response = self.client.post(
            reverse("pwms:referral_respond", kwargs={"public_id": referral.public_id}),
            {"notes": "Recorded for the committee"},
        )

        self.assertEqual(response.status_code, 200)
        referral.refresh_from_db()
        self.assertEqual(referral.status, "responded")
        self.assertEqual(referral.responded_by, officer)
