"""Executive-branch behaviour: the Cabinet structure and who owns an identity.

Ministers and Deputy Ministers are the first office holders who are not
necessarily in the Oracle ERP — they may be appointed from outside Parliament,
or remain on the ERP books as MPs while holding office. The pieces therefore
arrive together and are tested here:

* the ``Government of RSA`` → Cabinet ministries tree and the ``Minister`` /
  ``Deputy Minister`` roles seeded by migration
  ``0023_seed_executive_groups_and_roles``;
* the ``seed_executive`` snapshot command that adds the President and Deputy
  President offices and populates the current appointments by external
  username;
* the ``User.identity_source`` guard that keeps PWMS-managed identities out of
  ``sync_users_oracle``;
* the appointment lookups (``User.ministers()`` / ``current_portfolio``) and the
  responsible-minister / sponsor foreign keys that use them.
"""

import json
from datetime import date
from io import StringIO

from auditlog.models import LogEntry
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .forms import BillForm, InternationalAgreementForm
from .management.commands.seed_executive import DEFAULT_SNAPSHOT
from .management.commands.sync_base import OracleSyncBase
from .management.commands.sync_users_oracle import Command
from .membership.resolver import Alias, GroupResolver
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


class SeedExecutiveCommandTests(TestCase):
    """The current Cabinet snapshot loads into the executive tree safely."""

    def _run(self, *args):
        out = StringIO()
        call_command("seed_executive", *args, stdout=out)
        return out.getvalue()

    def _snapshot(self):
        return json.loads(DEFAULT_SNAPSHOT.read_text(encoding="utf-8"))

    def test_seeds_government_presidency_ministries_and_office_holders(self):
        self._run()

        root = Group.objects.get(name="Government of RSA", parent=None)
        self.assertEqual(root.group_type, "executive")
        presidency = root.children.get(name="The Presidency")
        self.assertEqual(presidency.group_type, "presidency")
        self.assertEqual(root.children.get(name="Health").group_type, "ministry")
        self.assertEqual(root.children.count(), 32)
        self.assertFalse(
            root.children.exclude(group_type__in=("ministry", "presidency")).exists()
        )

        for name in ("President", "Deputy President", "Minister", "Deputy Minister"):
            self.assertTrue(Role.objects.filter(name=name).exists())

        president = User.objects.get(username="cramaphosa")
        self.assertEqual(president.identity_source, "local")
        self.assertFalse(president.has_usable_password())
        self.assertTrue(president.has_role_in_group("President", presidency))

        health = root.children.get(name="Health")
        minister = User.objects.get(username="pmotsoaledi")
        self.assertTrue(minister.has_role_in_group("Minister", health))
        deputy = User.objects.get(username="mphaahla")
        self.assertTrue(deputy.has_role_in_group("Deputy Minister", health))

        snapshot = self._snapshot()
        expected = 2 + len(snapshot["ministers"]) + len(snapshot["deputy_ministers"])
        self.assertEqual(
            GroupMembership.objects.filter(
                group__group_type__in=Group.EXECUTIVE_GROUP_TYPES,
                role__name__in=(
                    "President",
                    "Deputy President",
                    "Minister",
                    "Deputy Minister",
                ),
            ).count(),
            expected,
        )

    def test_seeds_national_departments_under_their_ministries(self):
        self._run()
        snapshot = self._snapshot()
        root = Group.objects.get(name="Government of RSA", parent=None)

        departments = Group.objects.filter(group_type="department")
        self.assertEqual(departments.count(), len(snapshot["departments"]))

        health = root.children.get(name="Health")
        self.assertEqual(
            health.children.get(name="Department of Health").group_type,
            "department",
        )
        finance = root.children.get(name="Finance")
        self.assertTrue(finance.children.filter(name="National Treasury").exists())

        cogta = root.children.get(name="Cooperative Governance and Traditional Affairs")
        self.assertSetEqual(
            set(cogta.children.values_list("name", flat=True)),
            {
                "Department of Cooperative Governance",
                "Department of Traditional Affairs",
            },
        )

        police = root.children.get(name="Police")
        self.assertSetEqual(
            set(
                police.children.filter(group_type="department").values_list(
                    "name", flat=True
                )
            ),
            {
                "Civilian Secretariat for Police Service",
                "Independent Police Investigative Directorate",
                "South African Police Service",
            },
        )

        presidency = root.children.get(name="The Presidency")
        for name in (
            "Government Communication and Information System",
            "State Security Agency",
            "Statistics South Africa",
        ):
            self.assertTrue(presidency.children.filter(name=name).exists())

        self.assertEqual(
            departments.get(name="South African Police Service").description,
            "Agency with department status.",
        )

    def test_reimport_is_idempotent(self):
        self._run()
        counts = (
            User.objects.count(),
            Group.objects.count(),
            Role.objects.count(),
            GroupMembership.objects.count(),
        )

        out = self._run()

        self.assertEqual(
            (
                User.objects.count(),
                Group.objects.count(),
                Role.objects.count(),
                GroupMembership.objects.count(),
            ),
            counts,
        )
        self.assertIn("0 user(s) created", out)
        self.assertIn("0 appointment(s) created", out)

    def test_dry_run_writes_nothing(self):
        out = self._run("--dry-run")

        self.assertIn("DRY RUN", out)
        self.assertFalse(User.objects.filter(username="cramaphosa").exists())
        self.assertFalse(Group.objects.filter(group_type="department").exists())
        self.assertFalse(
            GroupMembership.objects.filter(
                group__group_type="presidency", role__name="President"
            ).exists()
        )

    def test_identity_source_option_applies_to_new_users(self):
        self._run("--identity-source", "erp")

        self.assertEqual(User.objects.get(username="cramaphosa").identity_source, "erp")

    def test_an_existing_identity_keeps_its_erp_owned_fields(self):
        existing = User.objects.create_user(
            username="PMotsoaledi",
            password="secret",
            first_name="Existing",
            last_name="Person",
            identity_source="erp",
        )

        self._run()

        existing.refresh_from_db()
        self.assertEqual(existing.username, "PMotsoaledi")
        self.assertEqual(existing.first_name, "Existing")
        self.assertEqual(existing.last_name, "Person")
        self.assertEqual(existing.identity_source, "erp")
        self.assertTrue(existing.has_usable_password())
        self.assertTrue(
            existing.has_role_in_group(
                "Minister",
                Group.objects.get(name="Government of RSA", parent=None).children.get(
                    name="Health"
                ),
            )
        )

    def test_prune_ends_seeded_appointments_but_not_manual_ones(self):
        self._run()
        health = Group.objects.get(name="Government of RSA", parent=None).children.get(
            name="Health"
        )
        minister_role = Role.objects.get(name="Minister")

        stale = User.objects.create_user(username="former-minister")
        stale_membership = GroupMembership.objects.create(
            user=stale,
            group=health,
            role=minister_role,
            start_date=date(2024, 1, 1),
            notes="Seeded by seed_executive for the 2024-01-01 snapshot.",
        )
        manual = User.objects.create_user(username="caretaker-minister")
        manual_membership = GroupMembership.objects.create(
            user=manual,
            group=health,
            role=minister_role,
            start_date=date(2024, 1, 1),
            notes="Manual caretaker appointment.",
        )

        out = self._run("--prune")

        stale_membership.refresh_from_db()
        manual_membership.refresh_from_db()
        self.assertFalse(stale_membership.is_active)
        self.assertEqual(
            stale_membership.end_date, date.fromisoformat(self._snapshot()["as_at"])
        )
        self.assertTrue(manual_membership.is_active)
        self.assertIn("1 pruned", out)


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

        # Membership rows are soft-delete-only and PROTECT their user, so a hard
        # user delete must clear them explicitly first. This test is about the
        # agreement's SET_NULL, not about membership retention.
        GroupMembership.objects.filter(user=minister).hard_delete()
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


class IdentityNumberStorageTests(TestCase):
    """Identity numbers have to be stored, encrypted and searchable.

    ``User.set_idno`` reads its keys through ``getattr(settings, ..., None)``, so
    a settings module that never declares them turns it into a silent no-op:
    ``idno_hmac`` stays empty on every row, nothing is encrypted, and
    ``sync_users_oracle.deactivate_missing_users`` is left with an empty candidate
    set because it matches on ``idno_hmac``. That is what had happened, so these
    tests deliberately run against the real settings rather than an
    ``override_settings`` substitute, and they fail if the wiring is lost again.
    """

    def test_settings_declare_both_identity_number_keys(self):
        for name in ("IDNO_HMAC_KEY", "IDNO_ENC_KEY"):
            self.assertTrue(hasattr(settings, name), f"settings.{name} is not declared")
            self.assertTrue(getattr(settings, name), f"settings.{name} is empty")

    def test_set_idno_stores_a_digest_and_a_recoverable_value(self):
        user = User.objects.create_user(username="idno-holder")
        user.set_idno("9001015800086")
        user.save()

        stored = User.objects.get(pk=user.pk)
        self.assertEqual(len(stored.idno_hmac), 64)  # hex-encoded SHA-256
        self.assertNotEqual(stored.idno_hmac, "9001015800086")
        self.assertNotEqual(stored.idno_encrypted, "9001015800086")
        self.assertEqual(stored.get_idno(), "9001015800086")

    def test_digest_is_deterministic_so_the_sync_can_look_identities_up(self):
        """``process_single_user`` finds an existing row by recomputing this."""
        digest = User._compute_idno_hmac("9001015800087")

        self.assertEqual(digest, User._compute_idno_hmac("9001015800087"))
        self.assertEqual(len(digest), 64)


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
            role_cache={},
            strip_group_code_prefix=self.command.strip_group_code_prefix,
            normalize_role_name=self.command.normalize_role_name,
            resolver=GroupResolver(normalize=lambda value: value),
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
            group_type="committee",
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
        # The bulk ending is audited row by row, so the lost appointment has a
        # trail even though it came from a sync rather than a person.
        self.assertTrue(
            LogEntry.objects.get_for_object(committee_membership)
            .filter(action=LogEntry.Action.UPDATE)
            .exists()
        )

    def test_deactivation_dry_run_changes_nothing(self):
        user = self._user(username="erp.leaver", source="erp", idno="6001015800085")

        self.command.deactivate_missing_users({}, self.by_id, dry_run=True)

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(self.command.stats["disabled_users"], 1)


class OracleSyncSupervisorTests(TestCase):
    """The ERP ``EMPLOYEEID`` is stored and the ``SUPERVISORID`` chain is linked."""

    def setUp(self):
        self.command = Command()
        self.command._membership_sync = MembershipSyncService(
            role_cache={},
            strip_group_code_prefix=self.command.strip_group_code_prefix,
            normalize_role_name=self.command.normalize_role_name,
            resolver=GroupResolver(normalize=lambda value: value),
        )

    @staticmethod
    def _row(employee_id, supervisor_id, *, username, idno="", employeetype="Staff"):
        """A full 15-column fetcher row (EMPLOYEEID / SUPERVISORID appended last)."""
        return (
            "Mr",
            "Smith",
            "Sam",
            "",
            f"{username}@parliament.gov.za",
            idno,
            "Male",
            "Administrative Officer",
            employeetype,
            "",
            username,
            "",
            "",
            employee_id,
            supervisor_id,
        )

    def test_employee_id_is_stored_on_create(self):
        self.command.process_single_user(
            idno="9001015800085",
            oracle_user=self._row("E2", "E1", username="newbie", idno="9001015800085"),
            existing_users={},
            existing_users_by_username={},
            existing_memberships={},
            force_update=True,
        )
        self.assertEqual(User.objects.get(username="newbie").employee_id, "E2")

    def test_employee_id_is_backfilled_on_update(self):
        user = User.objects.create_user(username="existing", password="pw")
        self.assertIsNone(user.employee_id)

        self.command.process_single_user(
            idno="8001015800085",
            oracle_user=self._row(
                "E7", "E1", username="existing", idno="8001015800085"
            ),
            existing_users={},
            existing_users_by_username={"existing": user},
            existing_memberships={},
            force_update=False,
        )

        user.refresh_from_db()
        self.assertEqual(user.employee_id, "E7")

    def test_supervisors_are_linked_from_the_chain(self):
        manager = User.objects.create(username="mgr", employee_id="E1")
        report = User.objects.create(username="rep", employee_id="E2")
        feed = {
            "mgr": self._row("E1", "", username="mgr"),
            "rep": self._row("E2", "E1", username="rep"),
        }

        self.command.assign_supervisors(feed)

        report.refresh_from_db()
        self.assertEqual(report.supervisor_id, manager.pk)
        self.assertEqual(self.command.stats["supervisors_linked"], 1)

    def test_unresolved_manager_is_left_untouched(self):
        other = User.objects.create(username="other")
        report = User.objects.create(username="rep", employee_id="E2", supervisor=other)
        feed = {"rep": self._row("E2", "E9", username="rep")}

        self.command.assign_supervisors(feed)

        report.refresh_from_db()
        # E9 is absent from the feed, so the existing line survives.
        self.assertEqual(report.supervisor_id, other.pk)
        self.assertEqual(self.command.stats["supervisors_linked"], 0)
        self.assertEqual(self.command.stats["supervisors_unresolved"], 1)

    def test_dry_run_reports_without_writing(self):
        User.objects.create(username="mgr", employee_id="E1")
        report = User.objects.create(username="rep", employee_id="E2")
        feed = {
            "mgr": self._row("E1", "", username="mgr"),
            "rep": self._row("E2", "E1", username="rep"),
        }

        self.command.assign_supervisors(feed, dry_run=True)

        report.refresh_from_db()
        self.assertIsNone(report.supervisor_id)
        self.assertEqual(self.command.stats["supervisors_linked"], 1)

    def test_dry_run_previews_links_without_writing(self):
        """A dry run resolves the chain from this run's users, not the DB."""
        manager = User.objects.create_user(username="mgr", password="pw")
        report = User.objects.create_user(username="rep", password="pw")
        by_username = {"mgr": manager, "rep": report}

        for row, idno in (
            (self._row("E1", "", username="mgr", idno="1"), "1"),
            (self._row("E2", "E1", username="rep", idno="2"), "2"),
        ):
            self.command.process_single_user(
                idno=idno,
                oracle_user=row,
                existing_users={},
                existing_users_by_username=by_username,
                existing_memberships={},
                force_update=False,
                dry_run=True,
            )

        feed = {
            "mgr": self._row("E1", "", username="mgr"),
            "rep": self._row("E2", "E1", username="rep"),
        }
        self.command.assign_supervisors(feed, dry_run=True)

        report.refresh_from_db()
        manager.refresh_from_db()
        self.assertIsNone(report.supervisor_id)  # dry run wrote nothing...
        self.assertIsNone(manager.employee_id)
        self.assertEqual(
            self.command.stats["supervisors_linked"], 1
        )  # ...but previewed


class OracleSyncProfileTests(TestCase):
    """The ERP profile fields are written alongside the identity fields."""

    def setUp(self):
        self.command = Command()
        # A minimal seeded tree: the resolver only indexes the administration
        # subtree, and the profile tests resolve their units by unique name.
        self.admin = Group.objects.create(
            name="Parliament Administration", group_type="administration"
        )
        self.secretary = Group.objects.create(
            name="Secretary to Parliament", group_type="office", parent=self.admin
        )
        self.section = Group.objects.create(
            name="Sync Test Section", group_type="section", parent=self.secretary
        )
        self.division = Group.objects.create(
            name="Sync Test Division", group_type="division", parent=self.secretary
        )
        self.resolver = GroupResolver.from_database(
            normalize=lambda value: value, aliases=[]
        )
        self.command._resolver = self.resolver
        self.command._membership_sync = MembershipSyncService(
            role_cache={},
            strip_group_code_prefix=self.command.strip_group_code_prefix,
            normalize_role_name=self.command.normalize_role_name,
            resolver=self.resolver,
        )

    @staticmethod
    def _row(
        *,
        username,
        idno="",
        positiondesc="Administrative Officer",
        child_org="",
        parent_org="",
    ):
        """A full 15-column fetcher row."""
        return (
            "Mr",
            "Mkhize",
            "Zwelini",
            "",
            f"{username}@parliament.gov.za",
            idno,
            "Male",
            positiondesc,
            "Staff",
            "",
            username,
            child_org,
            parent_org,
            "",
            "",
        )

    def _sync(self, row, *, by_username, by_id=None):
        self.command.process_single_user(
            idno=row[5],
            oracle_user=row,
            existing_users=by_id or {},
            existing_users_by_username=by_username,
            existing_memberships={},
            force_update=False,
        )

    def test_positiondesc_and_department_are_written_on_update(self):
        department = self.section
        user = User.objects.create_user(
            username="z.mkhize", password="pw", identity_source="erp"
        )

        self._sync(
            self._row(
                username="z.mkhize",
                idno="9001015800085",
                positiondesc="Senior Administrative Officer",
                child_org="Sync Test Section",
            ),
            by_username={"z.mkhize": user},
        )

        user.refresh_from_db()
        self.assertEqual(user.positiondesc, "Senior Administrative Officer")
        self.assertEqual(user.department_id, department.pk)

    def test_new_user_gets_positiondesc_and_department(self):
        department = self.division

        self._sync(
            self._row(
                username="new.person",
                idno="8001015800085",
                positiondesc="Committee Secretary",
                parent_org="Sync Test Division",
            ),
            by_username={},
        )

        user = User.objects.get(username="new.person")
        self.assertEqual(user.positiondesc, "Committee Secretary")
        self.assertEqual(user.department_id, department.pk)

    def test_unresolved_department_leaves_existing_link_untouched(self):
        existing = Group.objects.create(
            name="Sync Existing Department", group_type="department"
        )
        user = User.objects.create_user(
            username="z.mkhize",
            password="pw",
            identity_source="erp",
            department=existing,
            positiondesc="Old Position",
        )

        self._sync(
            self._row(
                username="z.mkhize",
                positiondesc="New Position",
                child_org="Unknown Sync Unit",
            ),
            by_username={"z.mkhize": user},
        )

        user.refresh_from_db()
        self.assertEqual(user.positiondesc, "New Position")
        self.assertEqual(user.department_id, existing.pk)


class PartyAffiliationTests(TestCase):
    """Party is derived from the party group, and the sync keeps it current."""

    def setUp(self):
        self.service = MembershipSyncService(
            role_cache={},
            strip_group_code_prefix=lambda value: value,
            normalize_role_name=lambda positiondesc, employee_type: "",
            resolver=GroupResolver(normalize=lambda value: value),
        )
        self.user = User.objects.create_user(username="p.member", password="pw")

    @staticmethod
    def _row(*, partycode, employeetype="Member", positiondesc="Member: NA"):
        return (
            "Mr",
            "Mkhize",
            "Zwelini",
            "",
            "p.member@parliament.gov.za",
            "9001015800085",
            "Male",
            positiondesc,
            employeetype,
            partycode,
            "p.member",
            "",
            "",
        )

    def test_current_party_is_derived_from_the_party_group(self):
        self.service.sync_user_membership(self.user, self._row(partycode="ANC"))

        self.user.refresh_from_db()
        self.assertEqual(self.user.current_party.name, "ANC")
        self.assertEqual(self.user.party_affiliation, "ANC")
        # The annotation used by list/search/serialisation agrees.
        annotated = User.with_party_affiliation().get(pk=self.user.pk)
        self.assertEqual(annotated.party_affiliation, "ANC")

    def test_party_change_ends_the_previous_membership(self):
        self.service.sync_user_membership(self.user, self._row(partycode="ANC"))
        self.service.sync_user_membership(self.user, self._row(partycode="DA"))

        self.user.refresh_from_db()
        self.assertEqual(self.user.party_affiliation, "DA")
        anc = GroupMembership.objects.get(user=self.user, group__name="ANC")
        self.assertFalse(anc.is_active)
        self.assertIsNotNone(anc.end_date)
        # The House-members membership is not a party and must survive.
        self.assertTrue(
            GroupMembership.objects.filter(
                user=self.user, group__name="NA Members", is_active=True
            ).exists()
        )

    def test_staff_row_ends_every_party_membership(self):
        self.service.sync_user_membership(self.user, self._row(partycode="ANC"))
        self.service.sync_user_membership(
            self.user,
            self._row(
                partycode="",
                employeetype="Staff",
                positiondesc="Administrative Officer",
            ),
        )

        self.user.refresh_from_db()
        self.assertEqual(self.user.party_affiliation, "")
        self.assertFalse(
            GroupMembership.objects.filter(
                user=self.user, group__group_type="party", is_active=True
            ).exists()
        )

    def test_dry_run_does_not_end_memberships(self):
        self.service.sync_user_membership(self.user, self._row(partycode="ANC"))

        result = self.service.sync_user_membership(
            self.user, self._row(partycode="DA"), dry_run=True
        )

        self.assertEqual(result["ended"], 1)
        self.assertTrue(
            GroupMembership.objects.filter(
                user=self.user, group__name="ANC", is_active=True
            ).exists()
        )


class GroupCodePrefixTests(TestCase):
    """Administrative prefixes are matched case-insensitively.

    Oracle writes both ``60300-ISS: …`` and ``Household: …``. The mixed-case
    entries in the prefix list used to be compared against ``.upper()``, so
    those units silently lost their hierarchy and collapsed onto the leaf name.
    """

    def setUp(self):
        self.base = OracleSyncBase()

    def test_a_mixed_case_prefix_keeps_the_hierarchy(self):
        self.assertEqual(
            self.base.strip_group_code_prefix(
                "60302-Household: Auxiliary Services Unit"
            ),
            "Household: Auxiliary Services Unit",
        )
        self.assertEqual(
            self.base.strip_group_code_prefix(
                "15052-Protection Services: Physical Security Unit"
            ),
            "Protection Services: Physical Security Unit",
        )

    def test_an_uppercase_prefix_still_keeps_the_hierarchy(self):
        self.assertEqual(
            self.base.strip_group_code_prefix("12345-ICT: CIO: Security"),
            "ICT: CIO: Security",
        )

    def test_a_non_administrative_prefix_still_uses_the_leaf(self):
        self.assertEqual(
            self.base.strip_group_code_prefix("80002-Members: NA: National Assembly"),
            "National Assembly",
        )


class SeededStaffMembershipTests(TestCase):
    """Staff memberships resolve through the seeded tree, not a name cache."""

    ALIASES = [
        Alias(
            child="LEG: Legal Advice Unit",
            parent="LEGAL SERVICES SECTION",
            path=(
                "Secretary to Parliament/Core Business Branch/Legal Services/"
                "Legal Advice Unit"
            ),
        ),
        Alias(
            child="CBS: Committee Support Unit",
            parent="CBS: COMMITTEES SECTION",
            path=(
                "Secretary to Parliament/Core Business Branch/"
                "Committee Support Services/Committee Support Unit"
            ),
        ),
    ]

    def setUp(self):
        admin = Group.objects.create(
            name="Parliament Administration", group_type="administration"
        )
        secretary = Group.objects.create(
            name="Secretary to Parliament", group_type="office", parent=admin
        )
        branch = Group.objects.create(
            name="Core Business Branch", group_type="branch", parent=secretary
        )
        self.legal = Group.objects.create(
            name="Legal Services", group_type="division", parent=branch
        )
        self.legal_unit = Group.objects.create(
            name="Legal Advice Unit", group_type="unit", parent=self.legal
        )
        self.committees = Group.objects.create(
            name="Committee Support Services", group_type="division", parent=branch
        )
        self.committees_unit = Group.objects.create(
            name="Committee Support Unit", group_type="unit", parent=self.committees
        )
        # An executive appointment that reconciliation must never end.
        self.ministry = Group.objects.create(
            name="Test Ministry", group_type="ministry"
        )

        self.user = User.objects.create_user(username="s.staff", password="pw")
        self.resolver = GroupResolver.from_database(
            normalize=lambda value: value, aliases=self.ALIASES
        )
        self.service = MembershipSyncService(
            role_cache={},
            strip_group_code_prefix=lambda value: value,
            normalize_role_name=lambda positiondesc, employee_type: "",
            resolver=self.resolver,
        )

    @staticmethod
    def _row(child_org, parent_org, *, username="s.staff", employeetype="Staff"):
        return (
            "Ms",
            "Dlamini",
            "Sipho",
            "",
            f"{username}@parliament.gov.za",
            "9001015800085",
            "Female",
            "Legal Advisor",
            employeetype,
            "",
            username,
            child_org,
            parent_org,
        )

    def _legal_row(self):
        return self._row("LEG: Legal Advice Unit", "LEGAL SERVICES SECTION")

    def _committees_row(self):
        return self._row("CBS: Committee Support Unit", "CBS: COMMITTEES SECTION")

    def test_staff_get_the_unit_and_its_division(self):
        result = self.service.sync_user_membership(self.user, self._legal_row())

        self.assertEqual(result["created"], 2)
        active = set(
            GroupMembership.objects.filter(user=self.user, is_active=True).values_list(
                "group__name", flat=True
            )
        )
        self.assertEqual(active, {"Legal Advice Unit", "Legal Services"})

    def test_moving_unit_ends_the_previous_memberships(self):
        self.service.sync_user_membership(self.user, self._legal_row())

        result = self.service.sync_user_membership(self.user, self._committees_row())

        self.assertEqual(result["created"], 2)
        self.assertEqual(result["ended"], 2)
        active = set(
            GroupMembership.objects.filter(user=self.user, is_active=True).values_list(
                "group__name", flat=True
            )
        )
        self.assertEqual(
            active, {"Committee Support Unit", "Committee Support Services"}
        )
        ended = GroupMembership.objects.filter(user=self.user, is_active=False)
        self.assertEqual(ended.count(), 2)
        self.assertTrue(all(row.end_date is not None for row in ended))

    def test_unresolved_org_leaves_existing_memberships_alone(self):
        self.service.sync_user_membership(self.user, self._legal_row())

        result = self.service.sync_user_membership(
            self.user, self._row("UNKNOWN: Unit", "UNKNOWN SECTION")
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["ended"], 0)
        self.assertEqual(
            GroupMembership.objects.filter(user=self.user, is_active=True).count(), 2
        )

    def test_executive_appointments_survive_an_org_move(self):
        role, _ = Role.objects.get_or_create(name="Minister")
        GroupMembership.objects.create(user=self.user, group=self.ministry, role=role)
        self.service.sync_user_membership(self.user, self._legal_row())

        self.service.sync_user_membership(self.user, self._committees_row())

        self.assertTrue(
            GroupMembership.objects.filter(
                user=self.user, group=self.ministry, is_active=True
            ).exists()
        )

    def test_dry_run_reports_the_move_without_ending_anything(self):
        self.service.sync_user_membership(self.user, self._legal_row())

        result = self.service.sync_user_membership(
            self.user, self._committees_row(), dry_run=True
        )

        self.assertEqual(result["ended"], 2)
        active = set(
            GroupMembership.objects.filter(user=self.user, is_active=True).values_list(
                "group__name", flat=True
            )
        )
        self.assertEqual(active, {"Legal Advice Unit", "Legal Services"})

    def test_a_collapsed_name_does_not_borrow_another_units_alias(self):
        """Aliases key on the raw name, not the normalised one.

        ``strip_group_code_prefix`` reduces both "14501-PPS: …: Man & Gen" and
        "86100-PBO: Director: Man & Gen" to "Man & Gen". Keying aliases on that
        would hand the PBO director the publishing unit's alias.
        """
        base = OracleSyncBase()
        resolver = GroupResolver.from_database(
            normalize=base.strip_group_code_prefix,
            aliases=[
                Alias(
                    child="14501-PPS: Production and Publishing: Man & Gen",
                    parent="55200-PCS: PRODUCTION AND PUBLISHING SECTION (PPS)",
                    path=(
                        "Secretary to Parliament/Core Business Branch/Legal Services"
                    ),
                ),
            ],
        )

        aliased = resolver.resolve_org(
            "14501-PPS: Production and Publishing: Man & Gen",
            "55200-PCS: PRODUCTION AND PUBLISHING SECTION (PPS)",
        )
        self.assertEqual(aliased.method, "alias-child")

        bystander = resolver.resolve_org(
            "86100-PBO: Director: Man & Gen",
            "86000-PBO: PARLIAMENTARY BUDGET OFFICE (PBO) SECTION",
        )
        self.assertFalse(bystander.resolved)
