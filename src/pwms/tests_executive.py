"""Executive-branch behaviour: the Cabinet structure and who owns an identity.

Ministers and Deputy Ministers are the first office holders who are not
necessarily in the Oracle ERP — they may be appointed from outside Parliament,
or remain on the ERP books as MPs while holding office. The pieces therefore
arrive together and are tested here:

* the ``Government of RSA`` → Cabinet ministries tree and the ``Minister`` /
  ``Deputy Minister`` roles seeded by migration
  ``0023_seed_executive_groups_and_roles``;
* the ``User.identity_source`` guard that keeps PWMS-managed identities out of
  ``sync_users_oracle``;
* the appointment lookups (``User.ministers()`` / ``current_portfolio``) and the
  responsible-minister / sponsor foreign keys that use them.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from .forms import BillForm, InternationalAgreementForm
from .management.commands.sync_users_oracle import Command
from .membership.sync_service import MembershipSyncService
from .models import (
    Bill,
    Group,
    GroupMembership,
    InternationalAgreement,
    Role,
    WorkflowType,
)

User = get_user_model()


def appoint(username, *, group=None, role_name="Minister", is_active=True):
    """Create a user and (optionally) an active appointment in ``group``."""
    user = User.objects.create_user(
        username=username, password="pw", is_active=is_active
    )
    if group is not None:
        role, _ = Role.objects.get_or_create(name=role_name)
        GroupMembership.objects.create(user=user, group=group, role=role)
    return user


class IdentitySourceDefaultTests(TestCase):
    """Unflagged identities stay with the ERP, as they were before the field."""

    def test_users_default_to_erp_managed(self):
        user = User.objects.create_user(username="default-source")
        self.assertEqual(user.identity_source, "erp")


class ExecutiveBranchSeedTests(TestCase):
    """The executive branch seeded from the Cabinet ministries CSV."""

    def test_government_root_is_seeded_as_executive(self):
        root = Group.objects.get(name="Government of RSA", parent__isnull=True)
        self.assertEqual(root.group_type, "executive")
        self.assertTrue(root.is_active)

    def test_every_cabinet_ministry_is_a_child_of_the_root(self):
        root = Group.objects.get(name="Government of RSA", parent__isnull=True)
        children = set(root.children.values_list("name", flat=True))
        self.assertEqual(len(children), 32)
        # The source CSV is a flat list with no section heading, so its first
        # row is a ministry — not a heading to skip.
        self.assertIn("Agriculture", children)
        # ...and trailing whitespace in a row must not leak into a name.
        self.assertIn("Sport, Arts and Culture", children)

    def test_ministries_are_typed_ministry_and_the_presidency_is_not(self):
        root = Group.objects.get(name="Government of RSA", parent__isnull=True)
        self.assertEqual(
            root.children.get(name="The Presidency").group_type, "presidency"
        )
        self.assertFalse(
            root.children.exclude(name="The Presidency")
            .exclude(group_type="ministry")
            .exists()
        )

    def test_minister_roles_are_seeded_without_workflow_powers(self):
        for name in ("Minister", "Deputy Minister"):
            role = Role.objects.get(name=name)
            self.assertTrue(role.description)
            # The office identifies its holder; what they may *do* is granted
            # by the RBAC layer, not by holding the role.
            self.assertFalse(role.can_create_workflows)
            self.assertFalse(role.can_transition_workflows)
            self.assertFalse(role.can_assign_workflows)
            self.assertFalse(role.can_manage_permissions)

    def test_a_minister_is_a_membership_in_the_ministry(self):
        minister = appoint("minister-health", group=self._ministry())
        membership = minister.get_groups_with_roles().get()
        self.assertEqual(membership.role.name, "Minister")
        self.assertEqual(membership.group.get_full_path(), "Government of RSA > Health")

    @staticmethod
    def _ministry():
        return Group.objects.get(name="Health", group_type="ministry")


class MinisterLookupTests(TestCase):
    """``User.ministers()`` / ``current_portfolio`` follow the appointment."""

    def setUp(self):
        self.ministry = Group.objects.get(name="Health", group_type="ministry")
        self.committee = Group.objects.create(
            name="Portfolio Committee on Health", group_type="portfolio_committee"
        )

    def test_lookup_finds_serving_ministers_and_deputy_ministers(self):
        minister = appoint("serving-minister", group=self.ministry)
        deputy = appoint(
            "serving-deputy", group=self.ministry, role_name="Deputy Minister"
        )

        self.assertEqual(
            set(User.ministers().values_list("username", flat=True)),
            {minister.username, deputy.username},
        )

    def test_lookup_excludes_ended_appointments_and_other_roles(self):
        ended = appoint("ended-minister", group=self.ministry)
        GroupMembership.objects.filter(user=ended).update(
            is_active=False, end_date=timezone.now().date()
        )
        appoint("deactivated-minister", group=self.ministry, is_active=False)
        # The same role name outside the executive branch is not an office...
        appoint("committee-minister", group=self.committee)
        # ...and neither is a non-office role inside a ministry.
        appoint("ministry-staff", group=self.ministry, role_name="Staff Member")

        self.assertEqual(User.ministers().count(), 0)

    def test_current_portfolio_is_the_ministry_of_the_serving_appointment(self):
        minister = appoint("portfolio-minister", group=self.ministry)
        self.assertEqual(minister.current_portfolio, self.ministry)

        GroupMembership.objects.filter(user=minister).update(is_active=False)
        minister.refresh_from_db()
        self.assertIsNone(minister.current_portfolio)

    def test_non_office_holder_has_no_portfolio(self):
        self.assertIsNone(appoint("plain-user").current_portfolio)

    def test_agreement_form_offers_serving_ministers(self):
        minister = appoint("form-minister", group=self.ministry)
        committee_minister = appoint("form-committee-minister", group=self.committee)

        offered = (
            InternationalAgreementForm(user=None)
            .fields["responsible_minister"]
            .queryset
        )

        self.assertIn(minister, offered)
        self.assertNotIn(committee_minister, offered)

    def test_bill_form_offers_active_users_as_sponsors(self):
        active = appoint("sponsor-active", group=self.ministry)
        inactive = appoint("sponsor-inactive", is_active=False)

        offered = BillForm(user=None).fields["sponsor"].queryset

        self.assertIn(active, offered)
        self.assertNotIn(inactive, offered)


class ResponsiblePartyForeignKeyTests(TestCase):
    """The minister/sponsor links, and the recorded name it falls back to."""

    def setUp(self):
        self.owner = User.objects.create_user(username="party-owner", password="pw")
        self.agreement_type = WorkflowType.objects.get(name="International Agreement")
        self.bill_type = WorkflowType.objects.get(name="Bill")
        self.ministry = Group.objects.get(name="Health", group_type="ministry")

    def _agreement(self, **overrides):
        data = {
            "workflow_type": self.agreement_type,
            "current_state": self.agreement_type.get_initial_state(),
            "title": "Linked agreement",
            "owner": self.owner,
        }
        data.update(overrides)
        return InternationalAgreement.objects.create(**data)

    def _bill(self, **overrides):
        data = {
            "workflow_type": self.bill_type,
            "current_state": self.bill_type.get_initial_state(),
            "bill_number": "B 77—2026",
            "title": "Linked bill",
            "owner": self.owner,
        }
        data.update(overrides)
        return Bill.objects.create(**data)

    def test_agreement_links_the_minister_and_falls_back_to_the_name(self):
        minister = appoint("agreement-minister", group=self.ministry)

        linked = self._agreement(responsible_minister=minister)
        named = self._agreement(responsible_minister_name="Minister of Health (former)")

        self.assertEqual(linked.responsible_minister_display, minister)
        self.assertIsNone(named.responsible_minister)
        self.assertEqual(
            named.responsible_minister_display, "Minister of Health (former)"
        )

    def test_bill_links_the_sponsor_and_accepts_an_originating_authority(self):
        sponsor = appoint("bill-sponsor", group=self.ministry)

        linked = self._bill(sponsor=sponsor)
        authority = self._bill(
            bill_number="B 78—2026", sponsor_name="Portfolio Committee on Health"
        )

        self.assertEqual(linked.sponsor_display, sponsor)
        self.assertIsNone(authority.sponsor)
        self.assertEqual(authority.sponsor_display, "Portfolio Committee on Health")

    def test_deleting_a_user_keeps_the_record_and_its_recorded_name(self):
        minister = appoint("deleted-minister", group=self.ministry)
        agreement = self._agreement(
            responsible_minister=minister,
            responsible_minister_name="Hon. Former Minister",
        )

        minister.delete()

        agreement.refresh_from_db()
        self.assertIsNone(agreement.responsible_minister)
        self.assertEqual(agreement.responsible_minister_display, "Hon. Former Minister")


class ResponsibleMinisterValidatorTests(TestCase):
    """Only executive office holders may be linked as responsible minister."""

    def setUp(self):
        self.owner = User.objects.create_user(username="validator-owner", password="pw")
        self.agreement_type = WorkflowType.objects.get(name="International Agreement")
        self.ministry = Group.objects.get(name="Health", group_type="ministry")

    def _agreement(self, **overrides):
        """An unsaved agreement, so ``full_clean()`` can be called on it."""
        data = {
            "workflow_type": self.agreement_type,
            "current_state": self.agreement_type.get_initial_state(),
            "title": "Validated agreement",
            "owner": self.owner,
        }
        data.update(overrides)
        return InternationalAgreement(**data)

    def test_non_office_holder_is_rejected(self):
        agreement = self._agreement(responsible_minister=appoint("plain-member"))

        with self.assertRaises(ValidationError) as caught:
            agreement.full_clean()

        self.assertIn("responsible_minister", caught.exception.message_dict)

    def test_serving_minister_is_accepted(self):
        agreement = self._agreement(
            responsible_minister=appoint("serving-validator", group=self.ministry)
        )

        agreement.full_clean()

    def test_former_minister_stays_valid_on_a_record_already_on_file(self):
        former = appoint("former-validator", group=self.ministry)
        GroupMembership.objects.filter(user=former).update(
            is_active=False, end_date=timezone.now().date()
        )

        self._agreement(responsible_minister=former).full_clean()

    def test_named_minister_without_an_account_is_accepted(self):
        self._agreement(
            responsible_minister_name="Minister of Health (former)"
        ).full_clean()


@override_settings(IDNO_HMAC_KEY="test-identity-key")
class OracleSyncIdentityGuardTests(TestCase):
    """``identity_source`` decides which identities ``sync_users_oracle`` owns."""

    @staticmethod
    def _oracle_row(**overrides) -> tuple:
        """A complete Oracle row for ``process_single_user``."""
        row = {
            "title": "Mr",
            "lastname": "Mkhize",
            "firstname": "Zwelini",
            "middlenames": "",
            "email": "z.mkhize@parliament.gov.za",
            "idno": "9001015800085",
            "sex": "Male",
            "positiondesc": "Administrative Officer",
            "employeetype": "Staff",
            "partycode": "",
            "username": "z.mkhize",
            "child_org": "",
            "parent_org": "",
        }
        row.update(overrides)
        return tuple(row.values())

    def setUp(self):
        self.command = Command()
        self.command._membership_sync = MembershipSyncService(
            group_cache={},
            role_cache={},
            strip_group_code_prefix=self.command.strip_group_code_prefix,
            normalize_role_name=self.command.normalize_role_name,
        )
        self.by_id = {}
        self.by_username = {}

    def _user(self, *, username, source, idno="", **kwargs):
        """Create a user and register it in the sync's lookup maps."""
        user = User.objects.create_user(
            username=username,
            email=f"{username}@parliament.gov.za",
            first_name="Zwelini",
            password="pw",
            identity_source=source,
            **kwargs,
        )
        if idno:
            user.set_idno(idno)
            user.save(update_fields=["idno_encrypted", "idno_hmac"])
        if user.idno_hmac:
            self.by_id[user.idno_hmac] = user
        self.by_username[user.username.lower()] = user
        return user

    def _sync(self, oracle_row, *, force_update=True):
        self.command.process_single_user(
            idno=oracle_row[5],
            oracle_user=oracle_row,
            existing_users=self.by_id,
            existing_users_by_username=self.by_username,
            existing_memberships={},
            force_update=force_update,
        )

    def test_local_user_found_by_id_number_is_not_overwritten(self):
        user = self._user(username="z.mkhize", source="local", idno="9001015800085")

        self._sync(self._oracle_row(firstname="Overwritten", employeetype="Member"))

        user.refresh_from_db()
        self.assertEqual(user.first_name, "Zwelini")
        self.assertEqual(user.employee_type, "")
        self.assertFalse(user.is_mp)
        self.assertEqual(self.command.stats["skipped_local_users"], 1)

    def test_local_user_found_by_username_is_not_overwritten(self):
        user = self._user(username="z.mkhize", source="local")
        users_before = User.objects.count()

        self._sync(self._oracle_row())

        user.refresh_from_db()
        self.assertEqual(user.first_name, "Zwelini")
        self.assertEqual(user.idno_hmac, None)
        self.assertEqual(User.objects.count(), users_before)
        self.assertEqual(self.command.stats["skipped_local_users"], 1)

    def test_erp_user_is_still_updated(self):
        user = self._user(username="z.mkhize", source="erp", idno="9001015800085")

        self._sync(self._oracle_row(), force_update=False)

        user.refresh_from_db()
        self.assertEqual(user.employee_type, "staff")
        self.assertEqual(self.command.stats["updated_users"], 1)
        self.assertEqual(self.command.stats["skipped_local_users"], 0)

    def test_local_user_missing_from_oracle_is_left_alone(self):
        user = self._user(
            username="local.minister", source="local", idno="8001015800085"
        )
        GroupMembership.objects.create(
            user=user,
            group=Group.objects.get(name="Health", group_type="ministry"),
            role=Role.objects.get(name="Minister"),
        )

        self.command.deactivate_missing_users({}, self.by_id, dry_run=False)

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(
            GroupMembership.objects.filter(user=user, is_active=True).exists()
        )
        self.assertEqual(self.command.stats["disabled_users"], 0)

    def test_erp_user_keeps_executive_memberships_when_deactivated(self):
        user = self._user(username="erp.minister", source="erp", idno="7001015800085")
        ministry = Group.objects.get(name="Health", group_type="ministry")
        committee = Group.objects.create(
            name="Standing Committee on Public Accounts",
            group_type="public_accounts_committee",
        )
        GroupMembership.objects.create(
            user=user, group=ministry, role=Role.objects.get(name="Minister")
        )
        GroupMembership.objects.create(
            user=user,
            group=committee,
            role=Role.objects.create(name="Committee Member"),
        )

        self.command.deactivate_missing_users({}, self.by_id, dry_run=False)

        user.refresh_from_db()
        self.assertFalse(user.is_active)
        # The appointment survives the lost payroll record...
        self.assertTrue(
            GroupMembership.objects.get(user=user, group=ministry).is_active
        )
        # ...while the ERP-backed membership is ended.
        committee_membership = GroupMembership.objects.get(user=user, group=committee)
        self.assertFalse(committee_membership.is_active)
        self.assertEqual(committee_membership.end_date, timezone.now().date())
        self.assertEqual(self.command.stats["disabled_users"], 1)
        self.assertEqual(self.command.stats["deactivated_memberships"], 1)
        self.assertEqual(self.command.stats["preserved_executive_memberships"], 1)
        self.assertEqual(self.command.stats["warnings"], 1)

    def test_deactivation_dry_run_changes_nothing(self):
        user = self._user(username="erp.leaver", source="erp", idno="6001015800085")

        self.command.deactivate_missing_users({}, self.by_id, dry_run=True)

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(self.command.stats["disabled_users"], 1)
