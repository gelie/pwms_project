import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import InternationalAgreement, InternationalResolution, State, WorkflowType


class AuditHistoryApiTests(TestCase):
    """The DRF audit-history endpoint for a workflow instance."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="alice", email="alice@example.com", password="pw"
        )
        wt = WorkflowType.objects.create(name="Test International Resolution")
        draft = State.objects.create(workflow_type=wt, name="Drafting", is_initial=True)
        cls.res = InternationalResolution.objects.create(
            workflow_type=wt,
            current_state=draft,
            resolution_number="IR-1",
            title="A test resolution",
            owner=cls.user,
        )
        cls.url = reverse("pwms_api:resolution-audit-history", args=[cls.res.public_id])

    def test_endpoint_requires_authentication(self):
        resp = APIClient().get(self.url)
        self.assertEqual(resp.status_code, 403)  # DRF: unauthenticated request

    def test_returns_audit_trail_for_instance(self):
        client = APIClient()
        client.force_authenticate(user=self.user)

        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 200)

        payload = resp.json()
        self.assertIsInstance(payload, list)
        # Creating the instance in setUpTestData produced an auditlog CREATE entry
        self.assertTrue(any(entry["action"] == 0 for entry in payload))
        # Each entry carries actor email + changes
        self.assertTrue(all("actor_email" in e and "changes" in e for e in payload))

    def test_missing_instance_returns_404(self):
        from uuid import uuid4

        client = APIClient()
        client.force_authenticate(user=self.user)
        url = reverse("pwms_api:resolution-audit-history", args=[uuid4()])
        resp = client.get(url)
        self.assertEqual(resp.status_code, 404)

    # --- Browsable API -----------------------------------------------------
    def test_endpoint_is_browsable_when_authenticated(self):
        """A browser request renders the DRF browsable API HTML, not raw JSON."""
        client = APIClient()
        client.force_authenticate(user=self.user)
        resp = client.get(self.url, HTTP_ACCEPT="text/html")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/html"))
        # The browsable page pretty-prints the JSON payload (incl. actor_email)
        self.assertContains(resp, "actor_email")

    def test_anonymous_browser_request_shows_login_not_raw_401(self):
        """Anonymous browser users get a browsable 403 page (with login link)."""
        resp = APIClient().get(self.url, HTTP_ACCEPT="text/html")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp["Content-Type"].startswith("text/html"))

    def test_drf_login_and_logout_pages_are_wired(self):
        """/api/auth/login + /api/auth/logout (rest_framework.urls) render."""
        login = APIClient().get("/api/auth/login/")
        self.assertEqual(login.status_code, 200)
        self.assertContains(login, "username")

        logout = APIClient().get("/api/auth/logout/")
        self.assertIn(logout.status_code, (200, 405))  # GET shows confirm page

    def test_api_root_indexes_endpoints(self):
        """GET /api/ returns a JSON index with links to the endpoints."""
        client = APIClient()
        client.force_authenticate(user=self.user)

        resp = client.get("/api/")
        self.assertEqual(resp.status_code, 200)

        payload = resp.json()
        self.assertIn("resolution-audit-history", payload["endpoints"])
        audit = payload["endpoints"]["resolution-audit-history"]
        self.assertEqual(audit["method"], "GET")
        # reverse() renders the pattern as an absolute URL on this host
        self.assertTrue(audit["url_pattern"].startswith("http://"))
        self.assertIn("/api/resolutions/{public_id}/audit/", audit["url_pattern"])
        self.assertIn("agreement-audit-history", payload["endpoints"])
        self.assertIn(
            "/api/agreements/{public_id}/audit/",
            payload["endpoints"]["agreement-audit-history"]["url_pattern"],
        )
        self.assertTrue(payload["login"].startswith("http://"))

    def test_api_root_is_browsable(self):
        """A browser request to /api/ renders the DRF browsable index."""
        client = APIClient()
        client.force_authenticate(user=self.user)

        resp = client.get("/api/", HTTP_ACCEPT="text/html")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/html"))
        self.assertContains(resp, "resolution-audit-history")

    # --- OpenAPI + interactive docs (drf-spectacular) ----------------------
    def test_openapi_schema_served_publicly(self):
        """GET /api/schema/ returns a valid OpenAPI document (no auth needed)."""
        resp = APIClient().get("/api/schema/?format=json")
        self.assertEqual(resp.status_code, 200)
        schema = json.loads(resp.content)  # parseable JSON proves JSON renderer
        self.assertTrue(schema["openapi"].startswith("3."))
        self.assertTrue(any("resolutions" in path for path in schema["paths"]))
        # api_root is annotated so it appears in the schema too
        self.assertIn("/api/", schema["paths"])

    def test_swagger_ui_and_redoc_render(self):
        """Interactive docs frontends are served."""
        swagger = APIClient().get("/api/docs/")
        self.assertEqual(swagger.status_code, 200)
        self.assertContains(swagger, "swagger-ui")

        redoc = APIClient().get("/api/redoc/")
        self.assertEqual(redoc.status_code, 200)
        self.assertContains(redoc, "redoc")

    def test_non_owner_without_access_is_forbidden(self):
        """An authenticated non-owner with no instance access gets 403."""
        User = get_user_model()
        stranger = User.objects.create_user(username="stranger", password="pw")

        client = APIClient()
        client.force_authenticate(user=stranger)

        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 403)


class AgreementAuditHistoryApiTests(TestCase):
    """The DRF audit-history endpoint for an InternationalAgreement."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="agreement-api", email="agreement-api@example.com", password="pw"
        )
        wt = WorkflowType.objects.create(name="Test International Agreement")
        tabled = State.objects.create(
            workflow_type=wt, name="Agreement Tabled", is_initial=True
        )
        cls.agreement = InternationalAgreement.objects.create(
            workflow_type=wt,
            current_state=tabled,
            title="A test agreement",
            owner=cls.user,
        )
        cls.url = reverse(
            "pwms_api:agreement-audit-history", args=[cls.agreement.public_id]
        )

    def test_endpoint_requires_authentication(self):
        resp = APIClient().get(self.url)
        self.assertEqual(resp.status_code, 403)  # DRF: unauthenticated request

    def test_returns_audit_trail_for_instance(self):
        client = APIClient()
        client.force_authenticate(user=self.user)

        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 200)

        payload = resp.json()
        self.assertIsInstance(payload, list)
        # Creating the instance in setUpTestData produced an auditlog CREATE entry
        self.assertTrue(any(entry["action"] == 0 for entry in payload))
        self.assertTrue(all("actor_email" in e and "changes" in e for e in payload))

    def test_missing_instance_returns_404(self):
        from uuid import uuid4

        client = APIClient()
        client.force_authenticate(user=self.user)
        url = reverse("pwms_api:agreement-audit-history", args=[uuid4()])
        resp = client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_non_owner_without_access_is_forbidden(self):
        """An authenticated non-owner with no instance access gets 403."""
        User = get_user_model()
        stranger = User.objects.create_user(
            username="agreement-stranger", password="pw"
        )

        client = APIClient()
        client.force_authenticate(user=stranger)

        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 403)
