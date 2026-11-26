import tempfile
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from ..models import (
    Bill,
    BillVersion,
    City,
    Country,
    DelegationParticipant,
    DelegationReport,
    Group,
    InternationalAgreement,
    InternationalResolution,
    WorkflowType,
)
from ..models.workflows import OVERDUE_IDENTIFIER


class DelegationReportTests(TestCase):
    """BRS attributes and validation for delegation reports (BR02/BR03/BR12)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="irpd", email="irpd@example.com", password="pw"
        )
        cls.wt = WorkflowType.objects.get(name="Delegation Report")
        cls.initial = cls.wt.get_initial_state()
        cls.switzerland = Country.objects.create(
            code="CH", iso3="CHE", name="Switzerland", continent="EU"
        )
        cls.geneva = City.objects.create(
            country=cls.switzerland,
            name="Geneva",
            ascii_name="Geneva",
            latitude="46.204391",
            longitude="6.143158",
            population=201818,
        )

    def _report(self, **overrides):
        data = {
            "workflow_type": self.wt,
            "current_state": self.initial,
            "title": "Delegation to the IPU",
            "owner": self.user,
            "assigned_to": self.user,
            "engagement_name": "IPU Assembly",
            "engagement_start_date": date(2026, 3, 1),
            "engagement_end_date": date(2026, 3, 5),
            "location_city": self.geneva,
            "location_country": self.switzerland,
        }
        data.update(overrides)
        return DelegationReport.objects.create(**data)

    def test_reference_number_is_generated_sequentially(self):
        first = self._report()
        second = self._report(title="Second delegation")
        year = timezone.now().year
        self.assertEqual(first.reference_number, f"DR-{year}-0001")
        self.assertEqual(second.reference_number, f"DR-{year}-0002")

    def test_future_or_inverted_engagement_dates_are_rejected(self):
        future = self._report()
        future.engagement_start_date = timezone.localdate() + timedelta(days=1)
        with self.assertRaises(ValidationError):
            future.full_clean()

        inverted = self._report(title="Inverted dates")
        inverted.engagement_start_date = date(2026, 3, 5)
        inverted.engagement_end_date = date(2026, 3, 1)
        with self.assertRaises(ValidationError):
            inverted.full_clean()

    def test_overdue_identifier_when_due_date_expired_and_open(self):
        report = self._report(deadline=timezone.now() - timedelta(days=1))
        self.assertTrue(report.is_overdue)
        self.assertEqual(report.overdue_identifier, OVERDUE_IDENTIFIER)

        report.deadline = timezone.now() + timedelta(days=1)
        self.assertFalse(report.is_overdue)
        self.assertEqual(report.overdue_identifier, "")

    def test_closed_report_is_not_overdue(self):
        closed = self.wt.states.get(name="Closed – House approved")
        report = self._report(
            current_state=closed, deadline=timezone.now() - timedelta(days=10)
        )
        self.assertFalse(report.is_overdue)

    def test_participants_and_resolutions_attach_to_report(self):
        report = self._report()
        participant = DelegationParticipant.objects.create(
            delegation_report=report,
            participant_type=DelegationParticipant.MEMBER,
            title="Hon.",
            first_name="Naledi",
            last_name="Mokoena",
            delegation_role="Leader of the Delegation",
        )
        self.assertEqual(participant.full_name, "Hon. Naledi Mokoena")
        self.assertEqual(list(report.participants.all()), [participant])

        res_type = WorkflowType.objects.get(name="International Resolution")
        resolution = InternationalResolution.objects.create(
            workflow_type=res_type,
            current_state=res_type.get_initial_state(),
            resolution_number="IR-BRS-1",
            title="Implement the resolution",
            owner=self.user,
        )
        report.add_sub_workflow(resolution)
        self.assertEqual(report.get_all_descendants(), [resolution])

    def test_the_same_person_cannot_be_added_twice(self):
        """The database refuses a duplicate delegate, whatever route creates it."""
        report = self._report()
        participant = {
            "first_name": "Naledi",
            "last_name": "Mokoena",
            "user": self.user,
        }
        DelegationParticipant.objects.create(delegation_report=report, **participant)

        with self.assertRaises(IntegrityError), transaction.atomic():
            DelegationParticipant.objects.create(
                delegation_report=report, **participant
            )

        # Somebody can still join a different delegation.
        DelegationParticipant.objects.create(
            delegation_report=self._report(), **participant
        )

    def test_assigned_official_email_is_exposed(self):
        report = self._report()
        self.assertEqual(report.assigned_to_email, "irpd@example.com")


class InternationalAgreementTests(TestCase):
    """BRS attributes for international agreements (BR02/BR12)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="agreement", email="agreement@example.com", password="pw"
        )
        cls.wt = WorkflowType.objects.get(name="International Agreement")
        cls.initial = cls.wt.get_initial_state()

    def _agreement(self, **overrides):
        data = {
            "workflow_type": self.wt,
            "current_state": self.initial,
            "title": "SADC Trade Protocol",
            "owner": self.user,
            "assigned_to": self.user,
            "agreement_type": InternationalAgreement.SECTION_231_2,
            "submitting_department": "Department of International Relations",
            "responsible_minister_name": "Minister of International Relations",
        }
        data.update(overrides)
        return InternationalAgreement.objects.create(**data)

    def test_reference_number_is_generated_sequentially(self):
        first = self._agreement()
        second = self._agreement(title="Second agreement")
        year = timezone.now().year
        self.assertEqual(first.reference_number, f"IA-{year}-0001")
        self.assertEqual(second.reference_number, f"IA-{year}-0002")

    def test_new_agreement_starts_agreement_tabled(self):
        agreement = self._agreement()
        self.assertEqual(
            agreement.current_state.name,
            "Agreement Tabled – referred to Committee",
        )

    def test_overdue_identifier_when_due_date_expired_and_open(self):
        agreement = self._agreement(deadline=timezone.now() - timedelta(days=1))
        self.assertTrue(agreement.is_overdue)
        self.assertEqual(agreement.overdue_identifier, OVERDUE_IDENTIFIER)

        agreement.deadline = timezone.now() + timedelta(days=1)
        self.assertFalse(agreement.is_overdue)
        self.assertEqual(agreement.overdue_identifier, "")

    def test_closed_agreement_is_not_overdue(self):
        closed = self.wt.states.get(name="Closed – House approved")
        agreement = self._agreement(
            current_state=closed, deadline=timezone.now() - timedelta(days=10)
        )
        self.assertFalse(agreement.is_overdue)

    def test_referral_committees_attach_to_agreement(self):
        committee = Group.objects.create(
            name="Portfolio Committee on Trade", group_type="portfolio_committee"
        )
        agreement = self._agreement()
        agreement.referral_committees.add(committee)
        self.assertEqual(list(agreement.referral_committees.all()), [committee])

    def test_assigned_official_email_is_exposed(self):
        agreement = self._agreement()
        self.assertEqual(agreement.assigned_to_email, "agreement@example.com")


class BillTests(TestCase):
    """Bill profile fields and the public-status mapping (Online Bill Tracking BRS)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="bill", email="bill@example.com", password="pw"
        )
        cls.wt = WorkflowType.objects.get(name="Bill")
        cls.initial = cls.wt.get_initial_state()

    def _bill(self, **overrides):
        data = {
            "workflow_type": self.wt,
            "current_state": self.initial,
            "bill_number": "B 12—2026",
            "title": "National Health Amendment Bill",
            "short_title": "Health Amendment",
            "bill_type": Bill.SECTION_76,
            "house_of_origin": Bill.NA,
            "sponsor_name": "Minister of Health",
            "introduced_date": date(2026, 6, 1),
            "owner": self.user,
            "assigned_to": self.user,
        }
        data.update(overrides)
        return Bill.objects.create(**data)

    def test_bill_profile_fields_are_captured(self):
        committee = Group.objects.create(
            name="Portfolio Committee on Health", group_type="portfolio_committee"
        )
        bill = self._bill(responsible_committee=committee)
        self.assertEqual(bill.short_title, "Health Amendment")
        self.assertEqual(bill.bill_type, Bill.SECTION_76)
        self.assertEqual(bill.house_of_origin, Bill.NA)
        self.assertEqual(bill.sponsor_name, "Minister of Health")
        self.assertEqual(bill.responsible_committee, committee)
        self.assertEqual(bill.__str__(), "B 12—2026 – National Health Amendment Bill")

    def test_public_status_collapses_internal_stages(self):
        bill = self._bill()
        self.assertEqual(bill.public_status, "Introduced")

        # Each detailed committee stage publishes as the same status (BRS §12).
        for stage in (
            "Referred to Committee",
            "Committee Deliberation",
            "House Debate and Voting",
        ):
            bill.current_state = self.wt.states.get(name=stage)
            self.assertEqual(bill.public_status, "Under Parliamentary Consideration")

        bill.current_state = self.wt.states.get(name="NCOP Consideration")
        self.assertEqual(bill.public_status, "National Council of Provinces")

    def test_public_status_is_empty_without_a_state(self):
        bill = self._bill()
        bill.current_state = None
        self.assertEqual(bill.public_status, "")

    def test_overdue_identifier_when_due_date_expired_and_open(self):
        bill = self._bill(deadline=timezone.now() - timedelta(days=1))
        self.assertTrue(bill.is_overdue)
        self.assertEqual(bill.overdue_identifier, OVERDUE_IDENTIFIER)

        closed = self.wt.states.get(name="Signed into Law")
        bill.current_state = closed
        self.assertFalse(bill.is_overdue)

    def test_assigned_official_email_is_exposed(self):
        bill = self._bill()
        self.assertEqual(bill.assigned_to_email, "bill@example.com")


class BillVersionTests(TestCase):
    """Preserved bill version history and the derived current version (BRS §15A)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="bill-version", password="pw")
        cls.wt = WorkflowType.objects.get(name="Bill")
        cls.bill = Bill.objects.create(
            workflow_type=cls.wt,
            current_state=cls.wt.get_initial_state(),
            bill_number="B 20—2026",
            title="Version integrity bill",
            owner=cls.user,
        )

    def test_current_version_is_empty_until_versions_exist(self):
        self.assertEqual(self.bill.current_version, "")

    def test_current_version_falls_back_to_the_latest_version(self):
        BillVersion.objects.create(
            bill=self.bill,
            version_label="B 20—2026",
            version_type=BillVersion.INTRODUCED,
            version_date=date(2026, 5, 1),
            recorded_by=self.user,
        )
        latest = BillVersion.objects.create(
            bill=self.bill,
            version_label="B 20—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            version_date=date(2026, 6, 1),
            recorded_by=self.user,
        )
        self.assertEqual(self.bill.current_version, latest.version_label)

    def test_flagging_a_version_current_demotes_the_previous_one(self):
        introduced = BillVersion.objects.create(
            bill=self.bill,
            version_label="B 20—2026",
            version_type=BillVersion.INTRODUCED,
            version_date=date(2026, 5, 1),
            is_current=True,
        )
        amended = BillVersion.objects.create(
            bill=self.bill,
            version_label="B 20—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            version_date=date(2026, 6, 1),
            is_current=True,
        )

        introduced.refresh_from_db()
        self.assertFalse(introduced.is_current)
        self.assertTrue(amended.is_current)
        self.assertEqual(
            BillVersion.objects.filter(bill=self.bill, is_current=True).count(), 1
        )
        self.assertEqual(self.bill.current_version, amended.version_label)

    def test_amendment_schedule_is_distinguished_from_the_amended_bill(self):
        schedule = BillVersion.objects.create(
            bill=self.bill,
            version_label="B 20—2026 (amendment schedule)",
            version_type=BillVersion.AMENDMENT_SCHEDULE,
            version_date=date(2026, 6, 1),
        )
        self.assertEqual(schedule.get_version_type_display(), "Amendment schedule")
        self.assertFalse(schedule.is_current)

    def test_versions_are_listed_newest_first(self):
        older = BillVersion.objects.create(
            bill=self.bill, version_label="older", version_date=date(2026, 1, 1)
        )
        newer = BillVersion.objects.create(
            bill=self.bill, version_label="newer", version_date=date(2026, 2, 1)
        )
        self.assertEqual(list(self.bill.versions.all()), [newer, older])


class ImportBillVersionsCommandTests(TestCase):
    """The bill-version bulk importer (``import_bill_versions``)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="version-import", password="pw")
        cls.wt = WorkflowType.objects.get(name="Bill")
        cls.bill = Bill.objects.create(
            workflow_type=cls.wt,
            current_state=cls.wt.get_initial_state(),
            bill_number="B 40—2026",
            title="Imported versions",
            owner=cls.user,
        )

    def _csv(self, text):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as handle:
            handle.write(text)
        self.addCleanup(Path(handle.name).unlink)
        return handle.name

    def _rows(self):
        return BillVersion.objects.filter(bill=self.bill)

    def test_import_creates_versions_and_honours_is_current(self):
        path = self._csv(
            "bill_number,version_label,version_type,version_date,document_url,notes,is_current\n"
            "B 40—2026,B 40—2026,introduced,2026-05-01,,Introduced,yes\n"
            "B 40—2026,B 40—2026 (1st amendment),amended,2026-06-01,"
            "https://example.com/v2,Amended clause 4,yes\n"
        )
        out = StringIO()
        call_command("import_bill_versions", path, stdout=out)

        self.assertEqual(self._rows().count(), 2)
        # The model normalises is_current, so only the last row stays current.
        self.assertEqual(self._rows().filter(is_current=True).count(), 1)
        self.assertEqual(
            self._rows().get(is_current=True).version_label,
            "B 40—2026 (1st amendment)",
        )
        self.assertEqual(self.bill.current_version, "B 40—2026 (1st amendment)")
        self.assertIn("Created:", out.getvalue())

    def test_accepts_version_type_labels_and_blank_date(self):
        path = self._csv(
            "bill_number,version_label,version_type,version_date\n"
            "B 40—2026,B 40—2026 (schedule),Amendment schedule,\n"
        )
        call_command("import_bill_versions", path, stdout=StringIO())

        version = self._rows().get()
        self.assertEqual(version.version_type, BillVersion.AMENDMENT_SCHEDULE)
        self.assertEqual(version.version_date, timezone.localdate())

    def test_reimport_is_idempotent(self):
        path = self._csv(
            "bill_number,version_label,version_date\nB 40—2026,B 40—2026,2026-05-01\n"
        )
        call_command("import_bill_versions", path, stdout=StringIO())
        call_command("import_bill_versions", path, stdout=StringIO())
        self.assertEqual(self._rows().count(), 1)

    def test_dry_run_writes_nothing(self):
        path = self._csv("bill_number,version_label\nB 40—2026,B 40—2026\n")
        out = StringIO()
        call_command("import_bill_versions", path, "--dry-run", stdout=out)
        self.assertEqual(self._rows().count(), 0)
        self.assertIn("DRY RUN", out.getvalue())

    def test_unknown_bill_number_is_reported_and_skipped(self):
        path = self._csv("bill_number,version_label\nB 999—2026,Nope\n")
        out = StringIO()
        call_command("import_bill_versions", path, stdout=out)
        self.assertEqual(BillVersion.objects.count(), 0)
        self.assertIn("no bill with number", out.getvalue())

    def test_invalid_version_type_is_reported(self):
        path = self._csv(
            "bill_number,version_label,version_type\nB 40—2026,B 40—2026,sideways\n"
        )
        out = StringIO()
        call_command("import_bill_versions", path, stdout=out)
        self.assertEqual(self._rows().count(), 0)
        self.assertIn("unknown version type", out.getvalue())

    def test_missing_required_column_is_rejected(self):
        path = self._csv("version_label\nOnly a label\n")
        with self.assertRaises(CommandError):
            call_command("import_bill_versions", path, stdout=StringIO())
