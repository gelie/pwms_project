import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import InternationalResolution, State, WorkflowType


class NinjaAuditSpikeTests(TestCase):
    """Prototype endpoints mounted at /ninja/ (session auth, typed schemas)."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="alice", email="alice@example.com", password="pw"
        )
        wt = WorkflowType.objects.create(name="International Resolution")
        draft = State.objects.create(workflow_type=wt, name="Drafting", is_initial=True)
        cls.res = InternationalResolution.objects.create(
            workflow_type=wt,
            current_state=draft,
            resolution_number="IR-1",
            title="A test resolution",
            owner=cls.user,
        )
        cls.audit_url = f"/ninja/resolutions/{cls.res.public_id}/audit/"

    def test_endpoint_requires_authentication(self):
        resp = self.client.get(self.audit_url)
        self.assertEqual(resp.status_code, 401)  # ninja: missing auth -> 401

    def test_audit_history_for_authenticated_user(self):
        self.assertTrue(self.client.login(username="alice", password="pw"))

        resp = self.client.get(self.audit_url)
        self.assertEqual(resp.status_code, 200)

        payload = resp.json()
        self.assertIsInstance(payload, list)
        # setUpTestData create produced an auditlog CREATE entry
        self.assertTrue(any(e["action"] == 0 for e in payload))
        self.assertTrue(all("actor_email" in e and "changes" in e for e in payload))

    def test_root_and_openapi_docs_served(self):
        self.assertTrue(self.client.login(username="alice", password="pw"))

        root = self.client.get("/ninja/")
        self.assertEqual(root.status_code, 200)
        self.assertIn("resolution-audit-history", root.json()["endpoints"])

        schema = self.client.get("/ninja/openapi.json")
        self.assertEqual(schema.status_code, 200)
        doc = json.loads(schema.content)
        self.assertTrue(any("resolutions" in path for path in doc["paths"]))

        docs = self.client.get("/ninja/docs")
        self.assertEqual(docs.status_code, 200)
        self.assertContains(docs, "swagger")
