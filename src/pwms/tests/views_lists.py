from django.urls import reverse

from ..models import (
    Bill,
    DelegationReport,
    InternationalAgreement,
    InternationalResolution,
)
from .views_crud_base import WorkflowCrudViewTestBase


class WorkflowListViewTests(WorkflowCrudViewTestBase):
    """Workflow list pages: the unified register, filters and navigation."""

    def test_search_filters_the_list(self):
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Geneva delegation"),
        )
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="New York delegation"),
        )

        response = self.client.get(reverse("pwms:delegation_reports"), {"q": "Geneva"})
        self.assertContains(response, "Geneva delegation")
        self.assertNotContains(response, "New York delegation")

    def test_navbar_links_to_workflow_views(self):
        response = self.client.get(reverse("pwms:home"))
        for name in (
            "workflows",
            "delegation_reports",
            "international_resolutions",
            "international_agreements",
            "bills",
        ):
            self.assertContains(response, reverse(f"pwms:{name}"))

    def test_workflows_page_unifies_every_accessible_instance(self):
        """One table spans all four concrete workflow types."""
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Unified report"),
        )
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(title="Unified resolution"),
        )
        self.client.post(
            reverse("pwms:international_agreement_create"),
            self._agreement_payload(title="Unified agreement"),
        )
        self.client.post(
            reverse("pwms:bill_create"), self._bill_payload(title="Unified bill")
        )

        response = self.client.get(reverse("pwms:workflows"))
        self.assertEqual(response.status_code, 200)
        for title in (
            "Unified report",
            "Unified resolution",
            "Unified agreement",
            "Unified bill",
        ):
            self.assertContains(response, title)

        # The page's own template comment must not leak as literal text; a
        # multi-line ``{# #}`` comment (which Django does not accept) would.
        self.assertNotContains(response, "Data-driven create menu")

        # Every row links to its own detail page (not a placeholder).
        for model, view_name in (
            (DelegationReport, "pwms:delegation_report_detail"),
            (InternationalResolution, "pwms:international_resolution_detail"),
            (InternationalAgreement, "pwms:international_agreement_detail"),
            (Bill, "pwms:bill_detail"),
        ):
            instance = model.objects.get()
            self.assertContains(
                response, reverse(view_name, kwargs={"public_id": instance.public_id})
            )

    def test_workflows_filters_narrow_the_table(self):
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Geneva delegation", priority="high"),
        )
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(title="Geneva resolution"),
        )
        url = reverse("pwms:workflows")

        # Free text matches on the title.
        response = self.client.get(url, {"q": "Geneva"})
        self.assertContains(response, "Geneva delegation")
        self.assertContains(response, "Geneva resolution")

        # Type narrows to one register; the other type drops out.
        response = self.client.get(url, {"type": "Delegation Report"})
        self.assertContains(response, "Geneva delegation")
        self.assertNotContains(response, "Geneva resolution")

        # Priority filters on the stored value, not the display label.
        response = self.client.get(url, {"priority": "high"})
        self.assertContains(response, "Geneva delegation")
        self.assertNotContains(response, "Geneva resolution")
