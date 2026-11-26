from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.urls import reverse

from ..models import (
    DelegationReport,
    Group,
    WorkflowType,
)
from ..services.search import DEFAULT_PER_CATEGORY, search_site
from .base import make_state


class GlobalSearchTests(TestCase):
    """The header's site-wide search: fragment vs page, scope and ranking."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="searcher", password="pw")
        cls.other = User.objects.create_user(username="outsider", password="pw")
        # No group on the type: only the owner of a record may view it, which is
        # what lets a test prove another user's record never surfaces.
        cls.wt = WorkflowType.objects.create(name="Search Test Reports")
        cls.state = make_state(workflow_type=cls.wt, name="Drafting", is_initial=True)
        cls.report = DelegationReport.objects.create(
            workflow_type=cls.wt,
            title="Climate finance delegation",
            current_state=cls.state,
            owner=cls.user,
            engagement_name="COP climate summit",
        )
        cls.other_report = DelegationReport.objects.create(
            workflow_type=cls.wt,
            title="Climate finance delegation (someone else's)",
            current_state=cls.state,
            owner=cls.other,
        )
        cls.group = Group.objects.create(
            name="Climate Change Committee", group_type="committee"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _search(self, query, **extra):
        return self.client.get(reverse("pwms:global_search"), {"q": query}, **extra)

    def test_htmx_request_returns_the_results_fragment(self):
        """HTMX gets the fragment the header swaps in, not a whole page."""
        response = self._search("climate", HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/partials/global_search_results.html")
        self.assertContains(response, "Climate finance delegation")

    def test_plain_request_returns_a_full_results_page(self):
        """Without JavaScript the same URL serves a linkable results page."""
        response = self._search("climate")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/search-results.html")
        self.assertContains(response, "Climate finance delegation")

    def test_results_are_grouped_by_the_register_they_came_from(self):
        response = self._search("climate", HTTP_HX_REQUEST="true")

        self.assertContains(response, "Delegation reports")
        self.assertContains(response, "Groups")
        self.assertContains(response, self.group.name)

    def test_a_record_the_reader_cannot_view_is_never_returned(self):
        response = self._search("climate", HTTP_HX_REQUEST="true")

        self.assertContains(response, "Climate finance delegation")
        self.assertNotContains(response, "someone else's")

    def test_an_empty_query_returns_nothing(self):
        response = self._search("   ", HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "global-search-panel")

    def test_an_empty_query_renders_no_categories(self):
        response = self._search("")

        self.assertTemplateUsed(response, "pwms/search-results.html")
        self.assertEqual(response.context["total"], 0)

    def test_title_matches_rank_before_body_mentions(self):
        """A title hit leads; a passing mention in the body trails it."""
        DelegationReport.objects.create(
            workflow_type=self.wt,
            title="Budget review",
            current_state=self.state,
            owner=self.user,
        )
        DelegationReport.objects.create(
            workflow_type=self.wt,
            title="Annual planning",
            description="The budget review cycle explained",
            current_state=self.state,
            owner=self.user,
        )

        categories = search_site(self.user, "budget")
        reports = next(c for c in categories if c.key == "delegation-reports")

        titles = [hit.title for hit in reports.hits]
        self.assertEqual(titles[0], "Budget review")
        self.assertIn("Annual planning", titles)

    def test_each_category_is_capped(self):
        for index in range(DEFAULT_PER_CATEGORY + 2):
            DelegationReport.objects.create(
                workflow_type=self.wt,
                title=f"Capstone report {index}",
                current_state=self.state,
                owner=self.user,
            )

        categories = search_site(self.user, "capstone")
        reports = next(c for c in categories if c.key == "delegation-reports")

        self.assertEqual(len(reports.hits), DEFAULT_PER_CATEGORY)

    def test_an_anonymous_user_finds_nothing(self):
        self.assertEqual(search_site(AnonymousUser(), "climate"), [])


class GlobalSearchHeaderTests(TestCase):
    """The search box is part of the site chrome, but only once signed in."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="viewer", password="pw"
        )

    def test_the_box_appears_for_a_signed_in_reader(self):
        self.client.force_login(self.user)
        search_url = reverse("pwms:global_search")

        response = self.client.get(reverse("pwms:home"))

        self.assertContains(response, 'id="global-search-input"')
        self.assertContains(response, f'hx-get="{search_url}"')

    def test_the_box_is_absent_for_an_anonymous_visitor(self):
        # The home page is reachable anonymously, so its header renders too.
        response = self.client.get(reverse("pwms:home"))

        self.assertNotContains(response, 'id="global-search-input"')
