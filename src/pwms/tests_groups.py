"""Committee self-administration and the delegation of a member's duties.

Two rules are pinned here:

* a **committee secretary** may edit their own committee's details and manage
  its membership, and nobody else's committee;
* the **committee secretary** records a member's delegation of their duties to
  another active PWMS user — who may belong to another group — until a chosen
  date. While that delegation is current the delegate carries the delegator's
  roles in the group (including the secretary's management powers, if those are
  what was delegated).
"""

from datetime import timedelta

from auditlog.models import LogEntry
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import DutyDelegation, Group, GroupMembership, Role
from .services.permissions import EDIT, MANAGE, VIEW, resolve


class CommitteeAccessTests(TestCase):
    """Who may administer a committee, and the delegation that lends it."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.committee = Group.objects.create(
            name="Portfolio Committee on Health", group_type=Group.Type.COMMITTEE
        )
        cls.other_committee = Group.objects.create(
            name="Portfolio Committee on Trade", group_type=Group.Type.COMMITTEE
        )
        cls.secretary_role = Role.objects.create(name=Group.COMMITTEE_SECRETARY_ROLE)
        cls.member_role = Role.objects.create(name="Committee Member")

        cls.secretary = User.objects.create_user(username="secretary", password="pw")
        cls.member = User.objects.create_user(username="member", password="pw")
        cls.other_secretary = User.objects.create_user(
            username="other-secretary", password="pw"
        )
        # An active user outside every group: a valid stand-in for a member.
        cls.outsider = User.objects.create_user(username="outsider", password="pw")

        cls.secretary_membership = GroupMembership.objects.create(
            user=cls.secretary, group=cls.committee, role=cls.secretary_role
        )
        cls.member_membership = GroupMembership.objects.create(
            user=cls.member, group=cls.committee, role=cls.member_role
        )
        GroupMembership.objects.create(
            user=cls.other_secretary,
            group=cls.other_committee,
            role=cls.secretary_role,
        )

    # -- the secretary rule --------------------------------------------------
    def test_secretary_may_edit_and_manage_own_committee(self):
        self.assertTrue(resolve(self.secretary, self.committee, EDIT))
        self.assertTrue(resolve(self.secretary, self.committee, MANAGE))
        # Everyone may still read it.
        self.assertTrue(resolve(self.member, self.committee, VIEW))

    def test_ordinary_member_cannot_edit_or_manage(self):
        self.assertFalse(resolve(self.member, self.committee, EDIT))
        self.assertFalse(resolve(self.member, self.committee, MANAGE))

    def test_secretary_cannot_administer_another_committee(self):
        self.assertFalse(resolve(self.other_secretary, self.committee, EDIT))
        self.assertFalse(resolve(self.other_secretary, self.committee, MANAGE))

    def test_membership_is_administered_with_its_group(self):
        self.assertTrue(resolve(self.secretary, self.member_membership, MANAGE))
        self.assertFalse(resolve(self.member, self.member_membership, MANAGE))

    def test_secretary_can_open_the_edit_page_and_save(self):
        self.client.force_login(self.secretary)
        url = reverse("pwms:group_edit", kwargs={"pk": self.committee.pk})

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            url,
            {
                "name": "Portfolio Committee on Health (renamed)",
                "short_name": "PC Health",
                "description": "Oversight of health.",
                "is_active": "on",
            },
        )
        self.assertRedirects(
            response, reverse("pwms:group_detail", kwargs={"pk": self.committee.pk})
        )
        self.committee.refresh_from_db()
        self.assertEqual(self.committee.name, "Portfolio Committee on Health (renamed)")

    def test_group_edits_are_audited(self):
        self.client.force_login(self.secretary)
        self.client.post(
            reverse("pwms:group_edit", kwargs={"pk": self.committee.pk}),
            {
                "name": "Portfolio Committee on Health (audited)",
                "is_active": "on",
            },
        )
        self.assertTrue(
            LogEntry.objects.get_for_object(self.committee)
            .filter(action=LogEntry.Action.UPDATE)
            .exists()
        )

    def test_ordinary_member_is_denied_the_edit_page(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("pwms:group_edit", kwargs={"pk": self.committee.pk})
        )
        self.assertEqual(response.status_code, 403)

    def test_group_details_card_only_shows_populated_fields(self):
        self.committee.short_name = "PC Health"
        self.committee.location = "Room 3"
        self.committee.contact_email = "pc.health@parliament.gov.za"
        self.committee.description = "Oversight of health."
        self.committee.save()

        self.client.force_login(self.member)
        response = self.client.get(
            reverse("pwms:group_detail", kwargs={"pk": self.committee.pk})
        )
        self.assertContains(response, "PC Health")
        self.assertContains(response, "Room 3")
        self.assertContains(response, "pc.health@parliament.gov.za")
        self.assertContains(response, "Oversight of health.")
        # Optional rows with no value are omitted entirely.
        self.assertNotContains(response, "Prefix")
        self.assertNotContains(response, "Term")

    def test_group_page_offers_controls_only_where_allowed(self):
        detail_url = reverse("pwms:group_detail", kwargs={"pk": self.committee.pk})
        self.client.force_login(self.secretary)
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edit group")
        self.assertContains(response, "Add member")
        self.assertContains(response, "Delegate duties")

        self.client.force_login(self.member)
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Edit group")
        self.assertNotContains(response, "Add member")
        self.assertNotContains(response, "Delegate duties")

    def test_delegation_page_is_management_only(self):
        url = reverse(
            "pwms:duty_delegation_create",
            kwargs={"pk": self.committee.pk},
        )
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)

        self.client.force_login(self.secretary)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # The two people come from two scoped type-ahead lookups.
        self.assertContains(response, "data-lookup")
        self.assertContains(
            response,
            reverse("pwms:group_member_search", kwargs={"pk": self.committee.pk}),
        )
        self.assertContains(
            response,
            reverse("pwms:duty_delegate_search", kwargs={"pk": self.committee.pk}),
        )

    def test_delegator_type_ahead_lists_only_group_members(self):
        self.client.force_login(self.secretary)
        search_url = reverse(
            "pwms:group_member_search", kwargs={"pk": self.committee.pk}
        )

        # The form points the delegator picker at the scoped search, not the
        # site-wide user picker.
        response = self.client.get(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            )
        )
        self.assertContains(response, search_url)

        # And that search only suggests people the form will accept.
        results = self.client.get(search_url)
        self.assertContains(results, self.member.username)
        self.assertContains(results, self.secretary.username)
        self.assertNotContains(results, self.outsider.username)

    def test_secretary_can_add_and_end_a_membership(self):
        self.client.force_login(self.secretary)
        new_person = get_user_model().objects.create_user(
            username="new-member", password="pw"
        )
        response = self.client.post(
            reverse(
                "pwms:group_membership_create",
                kwargs={"pk": self.committee.pk},
            ),
            {
                "user": new_person.pk,
                "role": self.member_role.pk,
                "start_date": timezone.localdate().isoformat(),
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        membership = GroupMembership.objects.get(group=self.committee, user=new_person)
        self.assertEqual(membership.updated_by, self.secretary)

        response = self.client.post(
            reverse(
                "pwms:group_membership_end",
                kwargs={"pk": self.committee.pk, "membership_pk": membership.pk},
            )
        )
        self.assertEqual(response.status_code, 302)
        membership.refresh_from_db()
        self.assertFalse(membership.is_active)

    def test_membership_changes_are_audited(self):
        self.client.force_login(self.secretary)
        new_person = get_user_model().objects.create_user(
            username="audited-member", password="pw"
        )
        self.client.post(
            reverse(
                "pwms:group_membership_create",
                kwargs={"pk": self.committee.pk},
            ),
            {
                "user": new_person.pk,
                "role": self.member_role.pk,
                "start_date": timezone.localdate().isoformat(),
                "is_active": "on",
            },
        )
        membership = GroupMembership.objects.get(group=self.committee, user=new_person)
        entries = LogEntry.objects.get_for_object(membership)
        self.assertTrue(entries.filter(action=LogEntry.Action.CREATE).exists())

        self.client.post(
            reverse(
                "pwms:group_membership_end",
                kwargs={"pk": self.committee.pk, "membership_pk": membership.pk},
            )
        )
        self.assertTrue(entries.filter(action=LogEntry.Action.UPDATE).exists())
        self.assertIn("is_active", entries.order_by("-timestamp").first().changes)

    def test_membership_person_and_role_fields_are_type_ahead_pickers(self):
        self.client.force_login(self.secretary)
        response = self.client.get(
            reverse(
                "pwms:group_membership_create",
                kwargs={"pk": self.committee.pk},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-lookup")
        self.assertContains(response, reverse("pwms:user_search"))
        self.assertContains(response, reverse("pwms:role_search"))

    def test_role_search_finds_assignable_roles_only(self):
        global_manager = Role.objects.create(
            name="Global Manager", can_manage_permissions=True
        )
        self.client.force_login(self.secretary)

        response = self.client.get(reverse("pwms:role_search"), {"search": "Committee"})
        self.assertContains(response, self.member_role.name)
        self.assertContains(response, self.secretary_role.name)
        self.assertNotContains(response, global_manager.name)

    # -- soft deletion -------------------------------------------------------
    def test_membership_delete_is_soft(self):
        self.member_membership.delete()
        self.member_membership.refresh_from_db()
        self.assertFalse(self.member_membership.is_active)
        self.assertTrue(
            GroupMembership.objects.filter(pk=self.member_membership.pk).exists()
        )

    def test_soft_deleting_membership_ends_its_delegations(self):
        delegation = DutyDelegation.objects.create(
            group=self.committee,
            delegator=self.member,
            delegate=self.outsider,
            end_date=timezone.localdate() + timedelta(days=7),
        )
        self.member_membership.delete()
        delegation.refresh_from_db()
        self.assertFalse(delegation.is_active)

    def test_membership_queryset_delete_is_soft(self):
        deleted, detail = GroupMembership.objects.filter(
            pk=self.member_membership.pk
        ).delete()
        self.assertEqual(deleted, 1)
        self.assertEqual(detail["pwms.GroupMembership"], 1)
        self.member_membership.refresh_from_db()
        self.assertFalse(self.member_membership.is_active)
        self.assertTrue(
            GroupMembership.objects.filter(pk=self.member_membership.pk).exists()
        )

    def test_delegation_delete_is_soft(self):
        delegation = DutyDelegation.objects.create(
            group=self.committee,
            delegator=self.secretary,
            delegate=self.outsider,
            end_date=timezone.localdate() + timedelta(days=7),
        )
        delegation.delete()
        delegation.refresh_from_db()
        self.assertFalse(delegation.is_active)
        self.assertTrue(DutyDelegation.objects.filter(pk=delegation.pk).exists())

    def test_delegation_queryset_delete_is_soft(self):
        delegation = DutyDelegation.objects.create(
            group=self.committee,
            delegator=self.secretary,
            delegate=self.outsider,
            end_date=timezone.localdate() + timedelta(days=7),
        )
        deleted, _ = DutyDelegation.objects.filter(pk=delegation.pk).delete()
        self.assertEqual(deleted, 1)
        delegation.refresh_from_db()
        self.assertFalse(delegation.is_active)
        self.assertTrue(DutyDelegation.objects.filter(pk=delegation.pk).exists())

    def test_user_deletion_is_blocked_by_membership(self):
        with self.assertRaises(ProtectedError):
            self.member.delete()

    def test_group_deletion_is_blocked_by_membership(self):
        with self.assertRaises(ProtectedError):
            self.committee.delete()

    def test_group_deletion_is_blocked_by_delegation(self):
        DutyDelegation.objects.create(
            group=self.committee,
            delegator=self.secretary,
            delegate=self.outsider,
            end_date=timezone.localdate() + timedelta(days=7),
        )
        with self.assertRaises(ProtectedError):
            self.committee.delete()

    def test_admin_delete_is_disabled_for_both_models(self):
        self.assertFalse(
            admin.site._registry[GroupMembership].has_delete_permission(None)
        )
        self.assertFalse(
            admin.site._registry[DutyDelegation].has_delete_permission(None)
        )

    def _member(self, username, **flags):
        """An active member of this committee, for the delegator slot."""
        user = get_user_model().objects.create_user(
            username=username, password="pw", **flags
        )
        GroupMembership.objects.create(
            user=user, group=self.committee, role=self.member_role
        )
        return user

    def test_mp_delegator_cannot_delegate_to_staff(self):
        mp_delegator = self._member("mp-delegator", is_mp=True)
        staff_outsider = get_user_model().objects.create_user(
            username="staff-outsider", password="pw", is_staff_member=True
        )
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(mp_delegator, staff_outsider),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            DutyDelegation.objects.filter(
                group=self.committee, delegator=mp_delegator
            ).exists()
        )

    def test_mp_delegator_can_delegate_to_another_mp(self):
        mp_delegator = self._member("mp-delegator", is_mp=True)
        mp_outsider = get_user_model().objects.create_user(
            username="mp-outsider", password="pw", is_mp=True
        )
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(mp_delegator, mp_outsider),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            DutyDelegation.objects.filter(
                group=self.committee, delegator=mp_delegator, delegate=mp_outsider
            ).exists()
        )

    def test_staff_delegator_cannot_delegate_to_an_mp(self):
        staff_delegator = self._member("staff-delegator", is_staff_member=True)
        mp_outsider = get_user_model().objects.create_user(
            username="mp-outsider", password="pw", is_mp=True
        )
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(staff_delegator, mp_outsider),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            DutyDelegation.objects.filter(
                group=self.committee, delegator=staff_delegator
            ).exists()
        )

    def test_erp_employee_type_alone_identifies_staff(self):
        # The Oracle sync sets employee_type but may leave is_staff_member false.
        staff_delegator = self._member("staff-delegator")
        staff_delegator.employee_type = "staff"
        staff_delegator.save(update_fields=["employee_type"])
        staff_outsider = get_user_model().objects.create_user(
            username="staff-outsider", password="pw", employee_type="staff"
        )
        mp_outsider = get_user_model().objects.create_user(
            username="mp-outsider", password="pw", is_mp=True
        )
        self.client.force_login(self.secretary)
        url = reverse("pwms:duty_delegation_create", kwargs={"pk": self.committee.pk})

        rejected = self.client.post(
            url, self._delegation_payload(staff_delegator, mp_outsider)
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(
            DutyDelegation.objects.filter(
                group=self.committee, delegator=staff_delegator
            ).exists()
        )

        accepted = self.client.post(
            url, self._delegation_payload(staff_delegator, staff_outsider)
        )
        self.assertEqual(accepted.status_code, 302)
        self.assertTrue(
            DutyDelegation.objects.filter(
                group=self.committee,
                delegator=staff_delegator,
                delegate=staff_outsider,
            ).exists()
        )

    def test_delegate_must_not_be_a_current_group_member(self):
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(self.secretary, self.member),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            DutyDelegation.objects.filter(
                group=self.committee, delegate=self.member
            ).exists()
        )

    def test_delegate_must_not_already_be_a_current_delegate(self):
        DutyDelegation.objects.create(
            group=self.committee,
            delegator=self.member,
            delegate=self.outsider,
            end_date=timezone.localdate() + timedelta(days=7),
        )
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(self.secretary, self.outsider),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            DutyDelegation.objects.filter(
                group=self.committee, delegate=self.outsider
            ).count(),
            1,
        )

    def test_delegate_search_applies_the_peer_and_exclusion_rules(self):
        mp_delegator = self._member("mp-delegator", is_mp=True)
        mp_candidate = get_user_model().objects.create_user(
            username="mp-candidate", password="pw", is_mp=True
        )
        mp_busy = get_user_model().objects.create_user(
            username="mp-busy", password="pw", is_mp=True
        )
        staff_candidate = get_user_model().objects.create_user(
            username="staff-candidate", password="pw", is_staff_member=True
        )
        DutyDelegation.objects.create(
            group=self.committee,
            delegator=mp_delegator,
            delegate=mp_busy,
            end_date=timezone.localdate() + timedelta(days=7),
        )

        self.client.force_login(self.secretary)
        response = self.client.get(
            reverse("pwms:duty_delegate_search", kwargs={"pk": self.committee.pk}),
            {"delegator": mp_delegator.pk},
        )
        self.assertContains(response, mp_candidate.username)
        self.assertNotContains(response, mp_busy.username)
        self.assertNotContains(response, staff_candidate.username)
        self.assertNotContains(response, self.member.username)

    def test_delegate_search_is_empty_without_a_group_member_delegator(self):
        self.client.force_login(self.secretary)
        response = self.client.get(
            reverse("pwms:duty_delegate_search", kwargs={"pk": self.committee.pk}),
            {"delegator": self.outsider.pk},
        )
        self.assertContains(response, "No users found")

    def test_delegation_is_reflected_on_the_member_row(self):
        delegation = DutyDelegation.objects.create(
            group=self.committee,
            delegator=self.member,
            delegate=self.outsider,
            end_date=timezone.localdate() + timedelta(days=7),
        )
        self.client.force_login(self.secretary)
        response = self.client.get(
            reverse("pwms:group_detail", kwargs={"pk": self.committee.pk})
        )
        self.assertContains(response, f"Delegated to {self.outsider.display_name}")
        # Delegation is reflected, not a suspension: the member still belongs.
        self.member_membership.refresh_from_db()
        self.assertTrue(self.member_membership.is_active)
        self.assertEqual(self.member_membership.current_delegation.pk, delegation.pk)

        delegation.is_active = False
        delegation.save(update_fields=["is_active", "updated_at"])
        response = self.client.get(
            reverse("pwms:group_detail", kwargs={"pk": self.committee.pk})
        )
        self.assertNotContains(response, "Delegated to")
        self.assertIsNone(self.member_membership.current_delegation)

    # -- delegation ----------------------------------------------------------
    def _delegation_payload(self, delegator, delegate, days=7):
        return {
            "delegator": delegator.pk,
            "delegate": delegate.pk,
            "end_date": (timezone.localdate() + timedelta(days=days)).isoformat(),
            "notes": "Covering a period of leave.",
        }

    def test_secretary_records_a_members_delegation(self):
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(self.member, self.outsider),
        )
        self.assertEqual(response.status_code, 302)

        delegation = DutyDelegation.objects.get(
            group=self.committee, delegator=self.member
        )
        self.assertEqual(delegation.delegate, self.outsider)
        self.assertEqual(delegation.created_by, self.secretary)
        self.assertTrue(delegation.is_current)
        # The stand-in carries the member's role even though they are not in
        # the group themselves.
        self.assertTrue(
            self.outsider.has_role_in_group("Committee Member", self.committee)
        )
        self.assertTrue(
            LogEntry.objects.get_for_object(delegation)
            .filter(action=LogEntry.Action.CREATE)
            .exists()
        )

    def test_delegation_changes_are_audited(self):
        self.client.force_login(self.secretary)
        self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(self.member, self.outsider),
        )
        delegation = DutyDelegation.objects.get(
            group=self.committee, delegator=self.member
        )
        entries = LogEntry.objects.get_for_object(delegation)
        self.assertTrue(entries.filter(action=LogEntry.Action.CREATE).exists())

        self.client.post(
            reverse(
                "pwms:duty_delegation_end",
                kwargs={
                    "pk": self.committee.pk,
                    "delegation_pk": delegation.pk,
                },
            )
        )
        self.assertTrue(entries.filter(action=LogEntry.Action.UPDATE).exists())

    def test_delegate_carries_the_delegators_committee_duties(self):
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(self.secretary, self.outsider),
        )
        self.assertEqual(response.status_code, 302)
        # An outside stand-in takes over the secretary's duties, including
        # administering this committee, for the period of the delegation.
        self.assertTrue(resolve(self.outsider, self.committee, EDIT))
        self.assertTrue(resolve(self.outsider, self.committee, MANAGE))

    def test_delegation_lapses_on_its_end_date(self):
        delegation = DutyDelegation.objects.create(
            group=self.committee,
            delegator=self.secretary,
            delegate=self.member,
            start_date=timezone.localdate() - timedelta(days=1),
            end_date=timezone.localdate() - timedelta(days=1),
        )
        self.assertFalse(delegation.is_current)
        self.assertFalse(resolve(self.member, self.committee, MANAGE))

    def test_ending_a_delegation_removes_the_lent_authority(self):
        DutyDelegation.objects.create(
            group=self.committee,
            delegator=self.secretary,
            delegate=self.member,
            end_date=timezone.localdate() + timedelta(days=7),
        )
        self.assertTrue(resolve(self.member, self.committee, MANAGE))

        self.client.force_login(self.secretary)
        delegation = DutyDelegation.objects.get(
            group=self.committee, delegator=self.secretary
        )
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_end",
                kwargs={
                    "pk": self.committee.pk,
                    "delegation_pk": delegation.pk,
                },
            )
        )
        self.assertEqual(response.status_code, 302)
        delegation.refresh_from_db()
        self.assertFalse(delegation.is_active)
        self.assertFalse(resolve(self.member, self.committee, MANAGE))

    def test_ending_the_delegators_membership_also_ends_the_delegation(self):
        delegation = DutyDelegation.objects.create(
            group=self.committee,
            delegator=self.member,
            delegate=self.outsider,
            end_date=timezone.localdate() + timedelta(days=7),
        )
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:group_membership_end",
                kwargs={
                    "pk": self.committee.pk,
                    "membership_pk": self.member_membership.pk,
                },
            )
        )
        self.assertEqual(response.status_code, 302)
        delegation.refresh_from_db()
        self.assertFalse(delegation.is_active)
        self.assertFalse(
            self.outsider.has_role_in_group("Committee Member", self.committee)
        )
        # The automatic ending is saved row by row, so it is audited too.
        self.assertTrue(
            LogEntry.objects.get_for_object(delegation)
            .filter(action=LogEntry.Action.UPDATE)
            .exists()
        )

    def test_ending_a_delegates_membership_keeps_the_delegation(self):
        delegation = DutyDelegation.objects.create(
            group=self.committee,
            delegator=self.secretary,
            delegate=self.member,
            end_date=timezone.localdate() + timedelta(days=7),
        )
        self.client.force_login(self.secretary)
        self.client.post(
            reverse(
                "pwms:group_membership_end",
                kwargs={
                    "pk": self.committee.pk,
                    "membership_pk": self.member_membership.pk,
                },
            )
        )
        delegation.refresh_from_db()
        # The stand-in can serve from outside the group, so their own
        # membership of the group is irrelevant to the delegation.
        self.assertTrue(delegation.is_active)

    def test_ordinary_member_cannot_record_a_delegation(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            )
        )
        self.assertEqual(response.status_code, 403)

    def test_secretary_cannot_record_a_delegation_in_another_committee(self):
        self.client.force_login(self.other_secretary)
        response = self.client.get(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            )
        )
        self.assertEqual(response.status_code, 403)

    def test_delegate_may_be_outside_the_group(self):
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(self.member, self.outsider),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            DutyDelegation.objects.filter(
                group=self.committee, delegate=self.outsider
            ).exists()
        )

    def test_delegator_must_be_a_group_member(self):
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(self.outsider, self.member),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            DutyDelegation.objects.filter(
                group=self.committee, delegator=self.outsider
            ).exists()
        )

    def test_cannot_delegate_to_oneself(self):
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse(
                "pwms:duty_delegation_create",
                kwargs={"pk": self.committee.pk},
            ),
            self._delegation_payload(self.secretary, self.secretary),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            DutyDelegation.objects.filter(
                group=self.committee, delegate=self.secretary
            ).exists()
        )
