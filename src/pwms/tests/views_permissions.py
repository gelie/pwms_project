from datetime import date

from django.contrib.auth import get_user_model
from django.urls import reverse

from ..models import (
    Bill,
    BillVersion,
    DelegationReport,
    Group,
    GroupMembership,
    InternationalResolution,
    WorkflowType,
)
from .views_crud_base import WorkflowCrudViewTestBase


class WorkflowPermissionViewTests(WorkflowCrudViewTestBase):
    """RBAC gates for create/detail/edit/delete and list visibility."""

    def test_pages_require_login(self):
        self.client.logout()
        for name in (
            "delegation_reports",
            "international_resolutions",
            "international_agreements",
            "bills",
        ):
            response = self.client.get(reverse(f"pwms:{name}"))
            self.assertEqual(response.status_code, 302)
            self.assertIn("/pwms/login/", response["Location"])

    def test_create_cannot_be_retargeted_at_another_workflow_type(self):
        """A forged workflow_type cannot create a mis-typed instance."""
        response = self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(workflow_type=self.agreement_type.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DelegationReport.objects.exists())

    def test_create_requires_a_role_from_the_type_group(self):
        """No create role in the type's group -> no creation (403 on POST)."""
        User = get_user_model()
        outsider = User.objects.create_user(username="outsider", password="pw")
        self.client.force_login(outsider)

        response = self.client.get(reverse("pwms:delegation_report_create"))
        self.assertRedirects(response, reverse("pwms:delegation_reports"))

        response = self.client.post(
            reverse("pwms:delegation_report_create"), self._report_payload()
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DelegationReport.objects.exists())

    def test_create_page_requires_a_role_for_that_specific_type(self):
        """A create role for another type does not open this type's create form."""
        # The acting user keeps a create role for bills only, so the report form
        # (a different type) must not be offered. Rolled back with the test.
        self.report_type.create_roles.remove(self.creator_role)

        response = self.client.get(reverse("pwms:delegation_report_create"))
        self.assertRedirects(response, reverse("pwms:delegation_reports"))

        # The type whose role they do hold still opens.
        self.assertEqual(self.client.get(reverse("pwms:bill_create")).status_code, 200)

    def test_can_create_needs_the_role_in_the_types_own_group(self):
        User = get_user_model()
        other_group = Group.objects.create(
            name="Test Other Group", group_type="portfolio_committee"
        )

        # Right role, wrong group.
        wrong_group = User.objects.create_user(username="wrong_group", password="pw")
        GroupMembership.objects.create(
            user=wrong_group, group=other_group, role=self.creator_role
        )
        self.assertFalse(self.report_type.can_create(wrong_group))

        # Right group, role that is not a declared create role.
        wrong_role = User.objects.create_user(username="wrong_role", password="pw")
        GroupMembership.objects.create(
            user=wrong_role, group=self.group, role=self.other_role
        )
        self.assertFalse(self.report_type.can_create(wrong_role))

        self.assertTrue(self.report_type.can_create(self.user))
        self.assertFalse(self.report_type.can_create(None))

    def test_group_roles_come_from_the_types_group(self):
        User = get_user_model()
        colleague = User.objects.create_user(username="colleague", password="pw")
        GroupMembership.objects.create(
            user=colleague, group=self.group, role=self.other_role
        )

        self.assertEqual(
            set(self.report_type.group_roles()),
            {self.creator_role, self.other_role},
        )
        # other_role is held in the group but is not a declared create role.
        self.assertFalse(self.report_type.can_create(colleague))

    def test_superuser_can_create_any_enabled_type(self):
        User = get_user_model()
        root = User.objects.create_superuser(username="root", password="pw")

        self.assertTrue(self.report_type.can_create(root))
        self.assertIn(self.report_type, WorkflowType.creatable_by(root))

    def test_create_form_only_offers_creatable_types(self):
        ungrouped = WorkflowType.objects.create(name="Test Ungrouped Type")

        response = self.client.get(reverse("pwms:delegation_report_create"))
        self.assertEqual(response.status_code, 200)
        offered = response.context["form"].fields["workflow_type"].queryset
        self.assertIn(self.report_type, offered)
        self.assertNotIn(ungrouped, offered)

    def test_bill_version_edit_requires_edit_permission(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")
        version = BillVersion.objects.create(
            bill=bill,
            version_label="B 99—2026",
            version_date=date(2026, 5, 1),
        )

        outsider = get_user_model().objects.create_user(
            username="version-editor-outsider", password="pw"
        )
        self.client.force_login(outsider)
        response = self.client.post(
            reverse(
                "pwms:bill_version_update", args=[bill.public_id, version.public_id]
            ),
            {
                "version_label": "Hijacked",
                "version_type": BillVersion.INTRODUCED,
                "version_date": "2026-05-01",
            },
        )
        self.assertEqual(response.status_code, 403)
        version.refresh_from_db()
        self.assertEqual(version.version_label, "B 99—2026")

    def test_bill_version_capture_requires_edit_permission(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")

        outsider = get_user_model().objects.create_user(
            username="version-outsider", password="pw"
        )
        self.client.force_login(outsider)
        response = self.client.post(
            reverse("pwms:bill_version_create", args=[bill.public_id]),
            {
                "version_label": "Sneaky version",
                "version_type": BillVersion.INTRODUCED,
                "version_date": "2026-07-01",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BillVersion.objects.filter(bill=bill).exists())

    def test_update_and_delete_require_edit_and_delete_permission(self):
        """A non-owner without instance access gets 403 on edit/delete."""
        self.client.post(
            reverse("pwms:delegation_report_create"), self._report_payload()
        )
        report = DelegationReport.objects.get(title="CRUD report")

        User = get_user_model()
        stranger = User.objects.create_user(username="stranger", password="pw")
        self.client.force_login(stranger)

        edit_url = reverse("pwms:delegation_report_update", args=[report.public_id])
        delete_url = reverse("pwms:delegation_report_delete", args=[report.public_id])

        self.assertEqual(self.client.get(edit_url).status_code, 403)
        self.assertEqual(
            self.client.post(edit_url, {"title": "Hijacked"}).status_code, 403
        )
        self.assertEqual(self.client.get(delete_url).status_code, 403)
        self.assertEqual(self.client.post(delete_url).status_code, 403)
        self.assertTrue(DelegationReport.objects.filter(pk=report.pk).exists())

    def test_detail_requires_view_access(self):
        """A non-owner without instance access cannot open a detail page."""
        self.client.post(
            reverse("pwms:delegation_report_create"), self._report_payload()
        )
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(),
        )
        report = DelegationReport.objects.get(title="CRUD report")
        resolution = InternationalResolution.objects.get(resolution_number="IR-CRUD-1")

        User = get_user_model()
        stranger = User.objects.create_user(username="view_stranger", password="pw")
        self.client.force_login(stranger)

        report_url = reverse("pwms:delegation_report_detail", args=[report.public_id])
        resolution_url = reverse(
            "pwms:international_resolution_detail", args=[resolution.public_id]
        )
        self.assertEqual(self.client.get(report_url).status_code, 403)
        self.assertEqual(self.client.get(resolution_url).status_code, 403)

    def test_lists_hide_instances_without_view_access(self):
        """List pages only render rows the signed-in user may view."""
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Owner only report"),
        )
        report = DelegationReport.objects.get(title="Owner only report")

        User = get_user_model()
        stranger = User.objects.create_user(username="list_stranger", password="pw")
        self.client.force_login(stranger)

        response = self.client.get(reverse("pwms:delegation_reports"))
        self.assertNotContains(response, "Owner only report")

        # The owner still sees their own row.
        self.client.force_login(self.user)
        response = self.client.get(reverse("pwms:delegation_reports"))
        self.assertContains(response, "Owner only report")
        self.assertContains(response, report.reference_number)

    def test_workflows_page_only_lists_instances_the_user_may_view(self):
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Private report"),
        )
        self.assertContains(
            self.client.get(reverse("pwms:workflows")), "Private report"
        )

        outsider = get_user_model().objects.create_user(
            username="workflow-outsider", password="pw"
        )
        self.client.force_login(outsider)

        response = self.client.get(reverse("pwms:workflows"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Private report")
        self.assertContains(response, "0 workflows")
