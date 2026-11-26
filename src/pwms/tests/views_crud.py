from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from ..models import (
    Bill,
    BillVersion,
    DelegationReport,
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Role,
    WorkflowType,
)
from .views_crud_base import WorkflowCrudViewTestBase


class WorkflowCrudViewTests(WorkflowCrudViewTestBase):
    """Create/read/update/delete cycles for every workflow instrument."""

    def test_delegation_report_crud_cycle(self):
        # Create: the initial state is derived from the chosen workflow type.
        response = self.client.post(
            reverse("pwms:delegation_report_create"), self._report_payload()
        )
        report = DelegationReport.objects.get(title="CRUD report")
        self.assertRedirects(
            response,
            reverse("pwms:delegation_report_detail", args=[report.public_id]),
        )
        self.assertEqual(report.current_state, self.report_type.get_initial_state())
        self.assertTrue(report.reference_number.startswith("DR-"))

        # Detail page renders the report.
        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, report.reference_number)

        # Update: workflow_type is fixed, current_state is shown read-only.
        edit = self.client.get(
            reverse("pwms:delegation_report_update", args=[report.public_id])
        )
        self.assertContains(edit, "current_state")
        self.assertNotContains(edit, 'name="workflow_type"')
        response = self.client.post(
            reverse("pwms:delegation_report_update", args=[report.public_id]),
            self._report_payload(
                title="CRUD report (edited)",
                current_state=report.current_state.pk,
                priority="high",
                engagement_name="",
            ),
        )
        self.assertRedirects(
            response,
            reverse("pwms:delegation_report_detail", args=[report.public_id]),
        )
        report.refresh_from_db()
        self.assertEqual(report.title, "CRUD report (edited)")
        self.assertEqual(report.priority, "high")

        # Delete: POST removes the row, GET only confirms.
        confirm = self.client.get(
            reverse("pwms:delegation_report_delete", args=[report.public_id])
        )
        self.assertEqual(confirm.status_code, 200)
        response = self.client.post(
            reverse("pwms:delegation_report_delete", args=[report.public_id])
        )
        self.assertRedirects(response, reverse("pwms:delegation_reports"))
        self.assertFalse(DelegationReport.objects.filter(pk=report.pk).exists())

    def test_international_resolution_crud_cycle(self):
        response = self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(),
        )
        resolution = InternationalResolution.objects.get(resolution_number="IR-CRUD-1")
        self.assertRedirects(
            response,
            reverse(
                "pwms:international_resolution_detail", args=[resolution.public_id]
            ),
        )
        self.assertEqual(
            resolution.current_state, self.resolution_type.get_initial_state()
        )

        response = self.client.get(
            reverse("pwms:international_resolution_detail", args=[resolution.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "IR-CRUD-1")

        response = self.client.post(
            reverse("pwms:international_resolution_delete", args=[resolution.public_id])
        )
        self.assertRedirects(response, reverse("pwms:international_resolutions"))
        self.assertFalse(
            InternationalResolution.objects.filter(pk=resolution.pk).exists()
        )

    def test_international_agreement_crud_cycle(self):
        # Create: the initial state is derived from the chosen workflow type.
        committee = Group.objects.create(
            name="CRUD Committee", group_type="portfolio_committee"
        )
        response = self.client.post(
            reverse("pwms:international_agreement_create"),
            self._agreement_payload(referral_committees=[committee.pk]),
        )
        agreement = InternationalAgreement.objects.get(title="CRUD agreement")
        self.assertRedirects(
            response,
            reverse("pwms:international_agreement_detail", args=[agreement.public_id]),
        )
        self.assertEqual(
            agreement.current_state, self.agreement_type.get_initial_state()
        )
        self.assertTrue(agreement.reference_number.startswith("IA-"))
        self.assertEqual(list(agreement.referral_committees.all()), [committee])

        # The list page renders the new row.
        listing = self.client.get(reverse("pwms:international_agreements"))
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, agreement.reference_number)

        # Detail page renders the agreement and its BRS attributes.
        response = self.client.get(
            reverse("pwms:international_agreement_detail", args=[agreement.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, agreement.reference_number)
        self.assertContains(response, "Department of Justice")

        # Update: workflow_type is fixed, current_state is shown read-only.
        edit = self.client.get(
            reverse("pwms:international_agreement_update", args=[agreement.public_id])
        )
        self.assertContains(edit, "current_state")
        self.assertNotContains(edit, 'name="workflow_type"')
        response = self.client.post(
            reverse("pwms:international_agreement_update", args=[agreement.public_id]),
            {
                "title": "CRUD agreement (edited)",
                "description": "",
                "current_state": agreement.current_state.pk,
                "owner": self.user.pk,
                "assigned_to": "",
                "deadline": "",
                "priority": "high",
                "agreement_type": InternationalAgreement.SECTION_231_2,
                "submitting_department": "Department of Justice",
                "responsible_minister_name": "Minister of Justice",
                "atc_tabling_date": "",
                "atc_reference": "",
                "referral_committees": [],
                "notes": "",
                "agreement_document_url": "",
                "explanatory_memorandum_url": "",
            },
        )
        self.assertRedirects(
            response,
            reverse("pwms:international_agreement_detail", args=[agreement.public_id]),
        )
        agreement.refresh_from_db()
        self.assertEqual(agreement.title, "CRUD agreement (edited)")
        self.assertEqual(agreement.priority, "high")

        # Delete: POST removes the row.
        response = self.client.post(
            reverse("pwms:international_agreement_delete", args=[agreement.public_id])
        )
        self.assertRedirects(response, reverse("pwms:international_agreements"))
        self.assertFalse(
            InternationalAgreement.objects.filter(pk=agreement.pk).exists()
        )

    def test_agreement_create_links_a_serving_minister(self):
        """The responsible-minister picker round-trips the linked account."""
        ministry = Group.objects.create(
            name="Ministry of Testing", group_type="ministry"
        )
        minister = get_user_model().objects.create_user(
            username="linked-minister", password="pw"
        )
        GroupMembership.objects.create(
            user=minister,
            group=ministry,
            role=Role.objects.get_or_create(name="Minister")[0],
        )

        response = self.client.post(
            reverse("pwms:international_agreement_create"),
            self._agreement_payload(responsible_minister=minister.pk),
        )

        agreement = InternationalAgreement.objects.get()
        self.assertRedirects(
            response,
            reverse("pwms:international_agreement_detail", args=[agreement.public_id]),
        )
        self.assertEqual(agreement.responsible_minister, minister)

    def test_bill_crud_cycle(self):
        # Create: the initial state is derived from the chosen workflow type.
        response = self.client.post(
            reverse("pwms:bill_create"),
            self._bill_payload(),
        )
        bill = Bill.objects.get(bill_number="B 99—2026")
        self.assertRedirects(
            response,
            reverse("pwms:bill_detail", args=[bill.public_id]),
        )
        self.assertEqual(bill.current_state, self.bill_type.get_initial_state())
        self.assertEqual(bill.public_status, "Introduced")

        # List and detail render the bill and its simplified public status.
        listing = self.client.get(reverse("pwms:bills"))
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "B 99—2026")

        detail = self.client.get(reverse("pwms:bill_detail", args=[bill.public_id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Public status")
        self.assertContains(detail, "Introduced")

        # Version history renders on the bill page (BRS §15A).
        BillVersion.objects.create(
            bill=bill,
            version_label="B 99—2026 (1st amendment)",
            version_type=BillVersion.AMENDED,
            version_date=timezone.localdate(),
            is_current=True,
            recorded_by=self.user,
        )
        detail = self.client.get(reverse("pwms:bill_detail", args=[bill.public_id]))
        self.assertContains(detail, "Version history")
        self.assertContains(detail, "B 99—2026 (1st amendment)")

        # Update: workflow_type is fixed, current_state is shown read-only.
        edit = self.client.get(reverse("pwms:bill_update", args=[bill.public_id]))
        self.assertContains(edit, "current_state")
        self.assertNotContains(edit, 'name="workflow_type"')
        response = self.client.post(
            reverse("pwms:bill_update", args=[bill.public_id]),
            {
                "title": "CRUD bill (edited)",
                "description": "",
                "current_state": bill.current_state.pk,
                "owner": self.user.pk,
                "assigned_to": "",
                "deadline": "",
                "priority": "high",
                "bill_number": "B 99—2026",
                "short_title": "CRUD",
                "bill_type": Bill.SECTION_75,
                "house_of_origin": Bill.NA,
                "sponsor_name": "Minister of Justice",
                "introduced_date": "",
                "responsible_committee": "",
                "atc_reference": "",
                "order_paper_reference": "",
                "bill_document_url": "",
                "notes": "",
            },
        )
        self.assertRedirects(
            response,
            reverse("pwms:bill_detail", args=[bill.public_id]),
        )
        bill.refresh_from_db()
        self.assertEqual(bill.title, "CRUD bill (edited)")
        self.assertEqual(bill.priority, "high")

        # Delete: POST removes the row.
        response = self.client.post(reverse("pwms:bill_delete", args=[bill.public_id]))
        self.assertRedirects(response, reverse("pwms:bills"))
        self.assertFalse(Bill.objects.filter(pk=bill.pk).exists())

    def test_bill_number_change_requires_clearing_the_document_url(self):
        url = "https://contoso.sharepoint.com/doc.pdf"
        with mock.patch("pwms.views.bill_views._file_bill_folder"):
            self.client.post(
                reverse("pwms:bill_create"),
                self._bill_payload(bill_document_url=url),
            )
            bill = Bill.objects.get(bill_number="B 99—2026")

            # The B-number names the folder: the old document URL would point
            # into the old folder, so the save is refused until it is cleared.
            response = self.client.post(
                reverse("pwms:bill_update", args=[bill.public_id]),
                self._bill_payload(bill_number="B 100—2026", bill_document_url=url),
            )
            self.assertContains(response, "Clear the Bill document URL")
            bill.refresh_from_db()
            self.assertEqual(bill.bill_number, "B 99—2026")

            response = self.client.post(
                reverse("pwms:bill_update", args=[bill.public_id]),
                self._bill_payload(bill_number="B 100—2026", bill_document_url=""),
            )

        self.assertRedirects(
            response, reverse("pwms:bill_detail", args=[bill.public_id])
        )
        bill.refresh_from_db()
        self.assertEqual(bill.bill_number, "B 100—2026")
        self.assertEqual(bill.bill_document_url, "")

    def test_bill_version_capture_from_the_web_ui(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")

        response = self.client.post(
            reverse("pwms:bill_version_create", args=[bill.public_id]),
            {
                "version_label": "B 99—2026 (1st amendment)",
                "version_type": BillVersion.AMENDED,
                "version_date": "2026-07-01",
                "is_current": "on",
                "document_url": "",
                "notes": "Amended clause 4",
            },
        )
        self.assertRedirects(
            response, reverse("pwms:bill_detail", args=[bill.public_id])
        )

        version = BillVersion.objects.get(bill=bill)
        self.assertEqual(version.recorded_by, self.user)
        self.assertTrue(version.is_current)
        self.assertEqual(bill.current_version, "B 99—2026 (1st amendment)")

        detail = self.client.get(reverse("pwms:bill_detail", args=[bill.public_id]))
        self.assertContains(detail, "Record version")
        self.assertContains(detail, "B 99—2026 (1st amendment)")
        self.assertContains(
            detail,
            reverse(
                "pwms:bill_version_update", args=[bill.public_id, version.public_id]
            ),
        )

    def test_bill_version_edit_from_the_web_ui(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")
        version = BillVersion.objects.create(
            bill=bill,
            version_label="B 99—2026",
            version_type=BillVersion.INTRODUCED,
            version_date=date(2026, 5, 1),
            recorded_by=self.user,
        )

        response = self.client.post(
            reverse(
                "pwms:bill_version_update", args=[bill.public_id, version.public_id]
            ),
            {
                "version_label": "B 99—2026 (corrected)",
                "version_type": BillVersion.AMENDED,
                "version_date": "2026-05-02",
                "is_current": "on",
                "document_url": "https://example.com/v1",
                "notes": "Corrected label",
            },
        )
        self.assertRedirects(
            response, reverse("pwms:bill_detail", args=[bill.public_id])
        )

        version.refresh_from_db()
        self.assertEqual(version.version_label, "B 99—2026 (corrected)")
        self.assertEqual(version.version_type, BillVersion.AMENDED)
        self.assertEqual(version.version_date, date(2026, 5, 2))
        self.assertTrue(version.is_current)
        self.assertEqual(version.notes, "Corrected label")
        # The correction is in place: identity and recorder are preserved.
        self.assertEqual(version.recorded_by, self.user)
        self.assertEqual(BillVersion.objects.filter(bill=bill).count(), 1)

    def test_bill_version_edit_can_move_the_current_flag(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")
        older = BillVersion.objects.create(
            bill=bill,
            version_label="B 99—2026",
            version_date=date(2026, 5, 1),
        )
        newer = BillVersion.objects.create(
            bill=bill,
            version_label="B 99—2026 (1st amendment)",
            version_date=date(2026, 6, 1),
            is_current=True,
        )

        response = self.client.post(
            reverse("pwms:bill_version_update", args=[bill.public_id, older.public_id]),
            {
                "version_label": older.version_label,
                "version_type": BillVersion.INTRODUCED,
                "version_date": "2026-05-01",
                "is_current": "on",
                "document_url": "",
                "notes": "",
            },
        )
        self.assertRedirects(
            response, reverse("pwms:bill_detail", args=[bill.public_id])
        )

        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertTrue(older.is_current)
        self.assertFalse(newer.is_current)
        self.assertEqual(bill.current_version, "B 99—2026")

    def test_create_requires_a_type_with_states(self):
        empty_type = WorkflowType.objects.create(
            name="Test Empty Workflow Type", group=self.group
        )
        empty_type.create_roles.add(self.creator_role)
        response = self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(workflow_type=empty_type.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DelegationReport.objects.exists())
