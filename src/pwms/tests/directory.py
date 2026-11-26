from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from ..models import (
    Group,
    GroupMembership,
    Role,
)


class GroupListFilterTests(TestCase):
    """The Name and Type filters above the all-groups list."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="group-filter", password="pw"
        )
        cls.committee = Group.objects.create(
            name="Zzz Filter Portfolio Committee", group_type="committee"
        )
        cls.ministry = Group.objects.create(
            name="Zzz Filter Ministry", short_name="ZFM", group_type="ministry"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_name_filter_narrows_the_table(self):
        response = self.client.get(
            reverse("pwms:all_groups"), {"q": "Zzz Filter Ministry"}
        )
        self.assertContains(response, self.ministry.name)
        self.assertNotContains(response, self.committee.name)

    def test_name_filter_matches_the_short_name(self):
        response = self.client.get(reverse("pwms:all_groups"), {"q": "zfm"})
        self.assertContains(response, self.ministry.name)
        self.assertNotContains(response, self.committee.name)

    def test_type_filter_narrows_the_table(self):
        response = self.client.get(reverse("pwms:all_groups"), {"type": "committee"})
        self.assertContains(response, self.committee.name)
        self.assertNotContains(response, self.ministry.name)

    def test_filters_combine(self):
        response = self.client.get(
            reverse("pwms:all_groups"), {"q": "Zzz Filter", "type": "ministry"}
        )
        self.assertContains(response, self.ministry.name)
        self.assertNotContains(response, self.committee.name)

    def test_type_options_cover_exactly_the_types_in_use(self):
        response = self.client.get(reverse("pwms:all_groups"))
        offered = {value for value, _ in response.context["type_choices"]}
        in_use = set(Group.objects.values_list("group_type", flat=True))
        self.assertEqual(offered, in_use)

    def test_no_match_message_names_the_filters(self):
        response = self.client.get(reverse("pwms:all_groups"), {"q": "nothing matches"})
        self.assertContains(response, "No groups match your filters.")
        self.assertNotContains(response, "No groups found.")


class UserProfileTests(TestCase):
    """The person profile page, its privacy boundary, and the links into it."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.person = User.objects.create_user(
            username="tmokoena",
            password="pw",
            first_name="Thandeka",
            last_name="Mokoena",
            positiondesc="Committee Secretary",
            constituency="Gauteng",
            bio="Long-serving committee secretary.",
            email="tmokoena@parliament.gov.za",
            is_staff_member=True,
        )
        # Identity / personnel fields the profile page must never surface.
        cls.person.set_idno("9001015800083")
        cls.person.date_of_birth = date(1990, 1, 1)
        cls.person.gender = "female"
        cls.person.employee_id = "EMP-42"
        cls.person.save()
        cls.group = Group.objects.create(
            name="Portfolio Committee on Health", group_type="portfolio_committee"
        )
        cls.role = Role.objects.create(name="Committee Secretary")
        GroupMembership.objects.create(user=cls.person, group=cls.group, role=cls.role)
        # Party is derived from an active membership of a ``party`` group; the
        # profile page shows it without any stored column.
        cls.party = Group.objects.create(name="ANC", group_type="party")
        mp_role, _ = Role.objects.get_or_create(
            name="Member of Parliament",
            defaults={"description": "Elected Member of Parliament"},
        )
        GroupMembership.objects.create(user=cls.person, group=cls.party, role=mp_role)

    def setUp(self):
        self.client.force_login(self.person)

    def _url(self):
        return reverse("pwms:user_detail", kwargs={"pk": self.person.pk})

    def test_the_profile_shows_the_person_and_their_groups(self):
        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/user-detail.html")
        self.assertContains(response, "Thandeka Mokoena")
        self.assertContains(response, "Committee Secretary")
        self.assertContains(response, self.group.name)
        self.assertContains(response, "ANC")

    def test_the_profile_omits_identity_and_personnel_fields(self):
        response = self.client.get(self._url())
        body = response.content.decode()

        # The ID number, date of birth, gender and employee id stay off the page.
        self.assertNotIn("9001015800083", body)
        self.assertNotIn("1990-01-01", body)
        self.assertNotIn("EMP-42", body)
        self.assertNotIn("female", body)

    def test_get_absolute_url_points_at_the_profile(self):
        self.assertEqual(self.person.get_absolute_url(), self._url())

    def test_a_group_page_links_its_members_to_their_profiles(self):
        response = self.client.get(
            reverse("pwms:group_detail", kwargs={"pk": self.group.pk})
        )

        self.assertContains(response, self._url())


class PersonSearchTests(TestCase):
    """People are a search source, found by name or username."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.viewer = User.objects.create_user(username="viewer", password="pw")
        cls.person = User.objects.create_user(
            username="tmokoena",
            password="pw",
            first_name="Thandeka",
            last_name="Mokoena",
            positiondesc="Committee Secretary",
        )
        cls.departed = User.objects.create_user(
            username="gone",
            password="pw",
            first_name="Departed",
            last_name="Zulu",
            is_active=False,
        )

    def setUp(self):
        self.client.force_login(self.viewer)

    def _search(self, query):
        return self.client.get(
            reverse("pwms:global_search"), {"q": query}, HTTP_HX_REQUEST="true"
        )

    def test_a_person_is_found_and_linked_to_their_profile(self):
        response = self._search("mokoena")

        self.assertContains(response, "People")
        self.assertContains(response, "Thandeka Mokoena")
        self.assertContains(
            response, reverse("pwms:user_detail", kwargs={"pk": self.person.pk})
        )

    def test_a_person_is_found_by_username(self):
        response = self._search("tmokoena")

        self.assertContains(response, "Thandeka Mokoena")

    def test_inactive_people_are_not_offered(self):
        response = self._search("Zulu")

        self.assertNotContains(response, "Departed Zulu")


class PeopleDirectoryTests(TestCase):
    """The browsable people directory and its filters."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.viewer = User.objects.create_user(
            username="directory-viewer", password="pw"
        )
        cls.mp = User.objects.create_user(
            username="mp-one",
            password="pw",
            first_name="Amara",
            last_name="Nkosi",
            positiondesc="Member of Parliament",
            is_mp=True,
        )
        cls.staff = User.objects.create_user(
            username="staff-one",
            password="pw",
            first_name="Sipho",
            last_name="Dlamini",
            positiondesc="Committee Secretary",
            is_staff_member=True,
        )
        cls.former = User.objects.create_user(
            username="former-one",
            password="pw",
            first_name="Fikile",
            last_name="Botha",
            is_staff_member=True,
            is_active=False,
        )

    def setUp(self):
        self.client.force_login(self.viewer)

    def _get(self, **params):
        return self.client.get(reverse("pwms:people"), params)

    def test_the_directory_lists_current_people_with_profile_links(self):
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/all-people.html")
        self.assertContains(response, "Amara Nkosi")
        self.assertContains(response, "Sipho Dlamini")
        self.assertContains(
            response, reverse("pwms:user_detail", kwargs={"pk": self.mp.pk})
        )

    def test_the_directory_hides_former_staff_by_default_but_can_show_them(self):
        self.assertNotContains(self._get(), "Fikile Botha")
        self.assertContains(self._get(status="former"), "Fikile Botha")
        self.assertContains(self._get(status="all"), "Fikile Botha")

    def test_the_directory_filters_by_free_text(self):
        response = self._get(q="committee secretary")

        self.assertContains(response, "Sipho Dlamini")
        self.assertNotContains(response, "Amara Nkosi")

    def test_the_directory_filters_by_kind(self):
        response = self._get(kind="mp")

        self.assertContains(response, "Amara Nkosi")
        self.assertNotContains(response, "Sipho Dlamini")

    def test_the_directory_is_linked_from_the_nav(self):
        response = self.client.get(reverse("pwms:home"))

        self.assertContains(response, reverse("pwms:people"))


class DirectoryPaginationTests(TestCase):
    """Both directory listings paginate, and their page links keep the filters."""

    @classmethod
    def setUpTestData(cls):
        cls.viewer = get_user_model().objects.create_user(
            username="pager", password="pw"
        )
        # Enough rows to spill onto a second page of DIRECTORY_PAGE_SIZE (25).
        for index in range(30):
            Group.objects.create(
                name=f"Paging Group {index:02d}", group_type="committee"
            )
            get_user_model().objects.create(
                username=f"paging-user-{index:02d}",
                first_name="Paging",
                last_name=f"User{index:02d}",
            )

    def setUp(self):
        self.client.force_login(self.viewer)

    def test_the_group_list_paginates(self):
        # Scoped by the free-text filter: the test database also carries the
        # groups the data migrations seed, so the unfiltered order is not ours.
        first = self.client.get(reverse("pwms:all_groups"), {"q": "Paging"})
        second = self.client.get(reverse("pwms:all_groups"), {"q": "Paging", "page": 2})

        self.assertEqual(len(first.context["page_obj"]), 25)
        self.assertTrue(first.context["page_obj"].has_next())
        self.assertContains(first, "Paging Group 24")
        self.assertNotContains(first, "Paging Group 29")
        self.assertContains(second, "Paging Group 29")

    def test_the_people_list_paginates(self):
        first = self.client.get(reverse("pwms:people"), {"q": "Paging"})
        second = self.client.get(reverse("pwms:people"), {"q": "Paging", "page": 2})

        self.assertEqual(len(first.context["page_obj"]), 25)
        self.assertTrue(first.context["page_obj"].has_next())
        self.assertContains(first, "Paging User23")
        self.assertNotContains(first, "Paging User29")
        self.assertContains(second, "Paging User29")

    def test_page_links_keep_the_active_filters(self):
        response = self.client.get(
            reverse("pwms:all_groups"), {"q": "Paging", "type": "committee"}
        )

        self.assertEqual(response.context["base_query"], "q=Paging&type=committee")
        self.assertContains(response, "?q=Paging&amp;type=committee&amp;page=2")

    def test_an_unusable_page_falls_back_instead_of_erroring(self):
        last = self.client.get(reverse("pwms:all_groups"), {"q": "Paging", "page": 999})
        self.assertEqual(last.context["page_obj"].number, 2)

        first = self.client.get(
            reverse("pwms:all_groups"), {"q": "Paging", "page": "not-a-number"}
        )
        self.assertEqual(first.context["page_obj"].number, 1)

    def test_a_per_page_size_is_honoured(self):
        response = self.client.get(
            reverse("pwms:people"), {"q": "Paging", "per_page": 10}
        )

        self.assertEqual(len(response.context["page_obj"]), 10)
        self.assertEqual(response.context["page_obj"].paginator.num_pages, 3)

    def test_an_unlisted_per_page_size_falls_back_to_the_default(self):
        response = self.client.get(
            reverse("pwms:people"), {"q": "Paging", "per_page": 9999}
        )

        self.assertEqual(len(response.context["page_obj"]), 25)

    def test_the_page_size_is_configurable(self):
        with self.settings(DIRECTORY_PAGE_SIZE=5, DIRECTORY_PAGE_SIZES=(5, 50)):
            response = self.client.get(reverse("pwms:people"), {"q": "Paging"})

        self.assertEqual(len(response.context["page_obj"]), 5)
        # The default is folded into the offered sizes even when it is not listed.
        self.assertEqual(response.context["page_sizes"], [5, 50])

    def test_the_listing_is_paged_in_the_database(self):
        with CaptureQueriesContext(connection) as captured:
            self.client.get(reverse("pwms:people"), {"q": "Paging", "per_page": 10})

        sql = " ".join(query["sql"] for query in captured.captured_queries)
        self.assertIn("LIMIT 10", sql.upper())
