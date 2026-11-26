from django.contrib.auth import get_user_model
from django.test import TestCase

from ..models import (
    Bill,
    DelegationParticipant,
    Group,
    GroupMembership,
    InternationalAgreement,
    Role,
    WorkflowType,
)


class WorkflowCrudViewTestBase(TestCase):
    """Shared fixtures and request payloads for the workflow view tests."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="crud", email="crud@example.com", password="pw"
        )
        # Group-scoped RBAC: creation needs one of a type's create roles, held
        # in the type's own group.
        cls.group = Group.objects.create(
            name="Test Workflow Group", group_type="portfolio_committee"
        )
        cls.creator_role = Role.objects.create(name="Test Workflow Creator")
        cls.other_role = Role.objects.create(name="Test Workflow Bystander")
        GroupMembership.objects.create(
            user=cls.user, group=cls.group, role=cls.creator_role
        )
        cls.report_type = WorkflowType.objects.get(name="Delegation Report")
        cls.resolution_type = WorkflowType.objects.get(name="International Resolution")
        cls.agreement_type = WorkflowType.objects.get(name="International Agreement")
        cls.bill_type = WorkflowType.objects.get(name="Bill")
        for workflow_type in (
            cls.report_type,
            cls.resolution_type,
            cls.agreement_type,
            cls.bill_type,
        ):
            workflow_type.group = cls.group
            workflow_type.save(update_fields=["group"])
            workflow_type.create_roles.add(cls.creator_role)
        # Option-list lengths are asserted below, so the shared group table must
        # hold only this class's group: seeded groups (e.g. the Government of
        # RSA ministries branch) would flip the group field to a search picker.
        Group.objects.exclude(pk=cls.group.pk).delete()

    def setUp(self):
        self.client.force_login(self.user)

    def _add_extra_users(self, count=11):
        """Push the user list past the form's search-picker threshold."""
        User = get_user_model()
        for number in range(count):
            User.objects.create_user(
                username=f"extra{number}", first_name=f"Extra{number}", last_name="User"
            )

    def _add_extra_groups(self, count=11):
        """Push the group list past the form's search-picker threshold."""
        for number in range(count):
            Group.objects.create(name=f"Extra group {number}")

    def _report_payload(self, **overrides):
        payload = {
            "workflow_type": self.report_type.pk,
            "title": "CRUD report",
            "description": "",
            "owner": self.user.pk,
            "assigned_to": "",
            "deadline": "",
            "priority": "medium",
            "engagement_name": "CRUD engagement",
            "engagement_start_date": "",
            "engagement_end_date": "",
            "location_city": "",
            "location_country": "",
            "notes": "",
            "report_document_url": "",
            # The report form also carries the delegates and adopted-resolutions
            # formsets. This is the management data for one untouched blank row in
            # each; ``participant_type`` repeats the select's initial value, as a
            # browser would, so the row counts as empty and is ignored.
            "participants-TOTAL_FORMS": "1",
            "participants-INITIAL_FORMS": "0",
            "participants-MIN_NUM_FORMS": "0",
            "participants-MAX_NUM_FORMS": "1000",
            "participants-0-id": "",
            "participants-0-user": "",
            "participants-0-participant_type": DelegationParticipant.MEMBER,
            "participants-0-delegation_role": "",
            "participants-0-DELETE": "",
            "resolutions-TOTAL_FORMS": "1",
            "resolutions-INITIAL_FORMS": "0",
            "resolutions-MIN_NUM_FORMS": "0",
            "resolutions-MAX_NUM_FORMS": "1000",
            "resolutions-0-resolution_number": "",
            "resolutions-0-title": "",
            "resolutions-0-adoption_date": "",
        }
        payload.update(overrides)
        return payload

    def _resolution_payload(self, **overrides):
        payload = {
            "workflow_type": self.resolution_type.pk,
            "title": "CRUD resolution",
            "description": "",
            "owner": self.user.pk,
            "assigned_to": "",
            "deadline": "",
            "priority": "medium",
            "resolution_number": "IR-CRUD-1",
            "resolution_text": "",
            "adoption_date": "",
            "responsible_group": "",
            "implementation_progress": "",
        }
        payload.update(overrides)
        return payload

    def _agreement_payload(self, **overrides):
        payload = {
            "workflow_type": self.agreement_type.pk,
            "title": "CRUD agreement",
            "description": "",
            "owner": self.user.pk,
            "assigned_to": "",
            "deadline": "",
            "priority": "medium",
            "agreement_type": InternationalAgreement.SECTION_231_3,
            "submitting_department": "Department of Justice",
            "responsible_minister_name": "Minister of Justice",
            "atc_tabling_date": "",
            "atc_reference": "",
            "referral_committees": [],
            "notes": "",
            "agreement_document_url": "",
            "explanatory_memorandum_url": "",
        }
        payload.update(overrides)
        return payload

    def _bill_payload(self, **overrides):
        payload = {
            "workflow_type": self.bill_type.pk,
            "title": "CRUD bill",
            "description": "",
            "owner": self.user.pk,
            "assigned_to": "",
            "deadline": "",
            "priority": "medium",
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
        }
        payload.update(overrides)
        return payload
