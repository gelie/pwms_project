from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class InformationalPageTests(TestCase):
    """The About and Contact pages describe the system, not placeholder copy."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="pages-user", password="pw"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_contact_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("pwms:contact"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/pwms/login/", response["Location"])

    def test_about_is_public(self):
        self.client.logout()
        response = self.client.get(reverse("pwms:about"))
        self.assertEqual(response.status_code, 200)

    def test_about_describes_the_system(self):
        response = self.client.get(reverse("pwms:about"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Parliament Workflow Management System")
        # The instruments the system actually tracks.
        for label in (
            "Delegation reports",
            "International resolutions",
            "International agreements",
            "Bills",
        ):
            self.assertContains(response, label)

    def test_contact_renders_its_own_page(self):
        """The contact route must not fall back to the about page."""
        response = self.client.get(reverse("pwms:contact"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Before you report a problem")
        self.assertNotContains(response, "Why PWMS exists")


class HomePageTests(TestCase):
    """The home page is public, and the landing page after sign-in."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="home-user", password="pw"
        )

    def test_home_is_reachable_without_signing_in(self):
        """The one public page: nothing behind it, but nothing in front of it."""
        response = self.client.get(reverse("pwms:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Welcome to PWMS")
        self.assertContains(response, reverse("pwms:login"))

    def test_home_is_the_post_login_landing_page(self):
        """Sign-in redirects here, so the route cannot simply be dropped."""
        self.assertEqual(settings.LOGIN_REDIRECT_URL, reverse("pwms:home"))

    def test_signed_in_users_get_a_launchpad(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("pwms:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Welcome back")
        for name in ("dashboard", "workflows", "my_groups"):
            self.assertContains(response, reverse(f"pwms:{name}"))

    def test_page_titles_reach_the_document(self):
        """base.html's title block renders (it was hardcoded before)."""
        response = self.client.get(reverse("pwms:home"))
        self.assertContains(response, "<title>Home</title>")

        self.client.force_login(self.user)
        response = self.client.get(reverse("pwms:about"))
        self.assertContains(response, "<title>About</title>")
