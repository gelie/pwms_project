import json
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from .api.throttles import TokenEndpointThrottle
from .models import (
    Bill,
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Role,
    State,
    Transition,
    WorkflowType,
)


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
        self.assertIn("bill-audit-history", payload["endpoints"])
        self.assertIn(
            "/api/bills/{public_id}/audit/",
            payload["endpoints"]["bill-audit-history"]["url_pattern"],
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


class BillAuditHistoryApiTests(TestCase):
    """The DRF audit-history endpoint for a Bill."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="bill-api", email="bill-api@example.com", password="pw"
        )
        wt = WorkflowType.objects.create(name="Test Bill Type")
        introduced = State.objects.create(
            workflow_type=wt, name="Introduced", is_initial=True
        )
        cls.bill = Bill.objects.create(
            workflow_type=wt,
            current_state=introduced,
            bill_number="B 7—2026",
            title="A test bill",
            owner=cls.user,
        )
        cls.url = reverse("pwms_api:bill-audit-history", args=[cls.bill.public_id])

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
        url = reverse("pwms_api:bill-audit-history", args=[uuid4()])
        resp = client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_non_owner_without_access_is_forbidden(self):
        """An authenticated non-owner with no instance access gets 403."""
        User = get_user_model()
        stranger = User.objects.create_user(username="bill-stranger", password="pw")

        client = APIClient()
        client.force_authenticate(user=stranger)

        resp = client.get(self.url)
        self.assertEqual(resp.status_code, 403)


# ===========================================================================
# Token authentication
# ===========================================================================

#: The local directory-less fallback, so ``authenticate()`` never dials AD in
#: these tests regardless of the host's .env (CI leaves LDAP unconfigured).
LOCAL_AUTH = override_settings(AUTH_LDAP_SERVER_URI="")


class TokenAuthenticationApiTests(TestCase):
    """Obtaining, using, rotating and revoking API tokens."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="token-user", email="token@example.com", password="s3cret"
        )

    def _obtain(self, username="token-user", password="s3cret"):
        return APIClient().post(
            "/api/auth/token/",
            {"username": username, "password": password},
            format="json",
        )

    def test_obtain_token_returns_a_usable_key(self):
        with LOCAL_AUTH:
            resp = self._obtain()

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["username"], "token-user")
        self.assertTrue(
            Token.objects.filter(key=payload["token"], user=self.user).exists()
        )

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {payload['token']}")
        me = client.get("/api/users/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], "token-user")

    def test_wrong_password_is_rejected(self):
        with LOCAL_AUTH:
            resp = self._obtain(password="nope")
        self.assertEqual(resp.status_code, 401)

    def test_inactive_account_is_rejected(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        try:
            with LOCAL_AUTH:
                resp = self._obtain()
            self.assertEqual(resp.status_code, 401)
        finally:
            self.user.is_active = True
            self.user.save(update_fields=["is_active"])

    def test_anonymous_request_is_forbidden_not_challenged(self):
        # Session auth leads the authenticator list, so the contract stays 403.
        self.assertEqual(APIClient().get("/api/users/").status_code, 403)

    def test_rotation_invalidates_the_old_key(self):
        old = Token.objects.create(user=self.user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {old.key}")

        resp = client.post("/api/auth/token/refresh/")

        self.assertEqual(resp.status_code, 200)
        new_key = resp.json()["token"]
        self.assertNotEqual(new_key, old.key)
        self.assertFalse(Token.objects.filter(key=old.key).exists())
        self.assertTrue(Token.objects.filter(key=new_key, user=self.user).exists())

    def test_revoke_removes_the_token(self):
        token = Token.objects.create(user=self.user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        resp = client.post("/api/auth/token/revoke/")

        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Token.objects.filter(user=self.user).exists())


# ===========================================================================
# Identity, structure and the workflow registry
# ===========================================================================


class ResourceApiTests(TestCase):
    """Users, groups, roles and memberships over the API."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="res-user", password="pw")
        cls.manager = User.objects.create_user(username="res-manager", password="pw")
        cls.role = Role.objects.create(name="Manager Role", can_manage_permissions=True)
        cls.group = Group.objects.create(
            name="Resource Committee", group_type="committee"
        )
        GroupMembership.objects.create(user=cls.manager, group=cls.group, role=cls.role)

    @staticmethod
    def _client(user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_user_list_requires_authentication(self):
        self.assertEqual(APIClient().get("/api/users/").status_code, 403)

    def test_user_list_is_paginated(self):
        resp = self._client(self.user).get("/api/users/")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIn("results", payload)
        self.assertTrue(any(u["username"] == "res-user" for u in payload["results"]))

    def test_user_detail_is_looked_up_by_username(self):
        resp = self._client(self.user).get("/api/users/res-user/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["username"], "res-user")

    def test_dotted_username_is_addressable(self):
        get_user_model().objects.create_user(username="z.mkhize", password="pw")
        resp = self._client(self.user).get("/api/users/z.mkhize/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["username"], "z.mkhize")

    def test_username_lookup_is_case_insensitive(self):
        resp = self._client(self.user).get("/api/users/RES-USER/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["username"], "res-user")

    def test_unknown_username_is_a_404(self):
        resp = self._client(self.user).get("/api/users/nobody-here/")
        self.assertEqual(resp.status_code, 404)

    def test_me_returns_the_caller(self):
        resp = self._client(self.user).get("/api/users/me/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["username"], "res-user")

    def test_user_payload_never_leaks_secrets(self):
        resp = self._client(self.user).get("/api/users/res-user/")
        for forbidden in ("password", "idno_encrypted", "idno_hmac"):
            self.assertNotIn(forbidden, resp.json())

    def test_reading_a_group_is_open_to_any_signed_in_user(self):
        resp = self._client(self.user).get(f"/api/groups/{self.group.public_id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Resource Committee")

    def test_plain_user_cannot_create_a_user(self):
        resp = self._client(self.user).post(
            "/api/users/",
            {"username": "newbie", "password": "pw12345"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_manager_can_create_a_user_with_a_password(self):
        resp = self._client(self.manager).post(
            "/api/users/",
            {
                "username": "newbie",
                "email": "newbie@example.com",
                "password": "pw12345",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        created = get_user_model().objects.get(username="newbie")
        self.assertTrue(created.check_password("pw12345"))
        # Privilege flags are read-only: a manager cannot mint an admin.
        self.assertNotIn("password", resp.json())
        self.assertFalse(created.is_superuser)

    def test_membership_list_returns_rows(self):
        resp = self._client(self.user).get("/api/memberships/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)
        self.assertEqual(resp.json()["results"][0]["group_name"], "Resource Committee")

    def test_creating_a_membership_records_the_actor(self):
        resp = self._client(self.manager).post(
            "/api/memberships/",
            {
                "user": self.user.pk,
                "group": self.group.pk,
                "role": self.role.pk,
                "start_date": "2026-01-01",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["updated_by"], self.manager.pk)

    def test_role_list_exposes_capability_flags(self):
        resp = self._client(self.user).get(f"/api/roles/{self.role.public_id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["can_manage_permissions"])


# ===========================================================================
# Workflow instances
# ===========================================================================


class WorkflowResourceApiTests(TestCase):
    """Delegation-report/resolution/agreement/bill endpoints, exercised via Bill."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="wf-owner", password="pw")
        cls.stranger = User.objects.create_user(username="wf-stranger", password="pw")
        cls.group = Group.objects.create(name="Bill Committee", group_type="committee")
        cls.role = Role.objects.create(name="Bill Clerk", can_create_workflows=True)
        GroupMembership.objects.create(user=cls.owner, group=cls.group, role=cls.role)

        cls.workflow_type = WorkflowType.objects.create(
            name="Bill Machine", group=cls.group
        )
        cls.workflow_type.create_roles.add(cls.role)
        cls.draft = State.objects.create(
            workflow_type=cls.workflow_type, name="Draft", is_initial=True
        )
        cls.assent = State.objects.create(
            workflow_type=cls.workflow_type, name="Assent", is_terminal=True
        )
        cls.send = Transition.objects.create(
            workflow_type=cls.workflow_type,
            name="Send for assent",
            from_state=cls.draft,
            to_state=cls.assent,
            requires_comment=True,
        )
        cls.bill = Bill.objects.create(
            workflow_type=cls.workflow_type,
            current_state=cls.draft,
            bill_number="B 1\u20142026",
            title="A test bill",
            owner=cls.owner,
        )

    @staticmethod
    def _client(user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_collection_requires_authentication(self):
        self.assertEqual(APIClient().get("/api/bills/").status_code, 403)

    def test_list_only_returns_visible_instances(self):
        mine = self._client(self.owner).get("/api/bills/")
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(mine.json()["count"], 1)

        theirs = self._client(self.stranger).get("/api/bills/")
        self.assertEqual(theirs.json()["count"], 0)

    def test_detail_is_looked_up_by_public_id(self):
        resp = self._client(self.owner).get(f"/api/bills/{self.bill.public_id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["bill_number"], "B 1\u20142026")

    def test_detail_is_forbidden_for_an_unrelated_user(self):
        resp = self._client(self.stranger).get(f"/api/bills/{self.bill.public_id}/")
        self.assertEqual(resp.status_code, 403)

    def test_member_may_create_and_state_defaults_to_initial(self):
        resp = self._client(self.owner).post(
            "/api/bills/",
            {
                "workflow_type": self.workflow_type.pk,
                "title": "Another bill",
                "bill_number": "B 2\u20142026",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        created = Bill.objects.get(bill_number="B 2\u20142026")
        self.assertEqual(created.owner, self.owner)
        self.assertEqual(created.current_state, self.draft)

    def test_user_without_a_create_role_cannot_create(self):
        resp = self._client(self.stranger).post(
            "/api/bills/",
            {
                "workflow_type": self.workflow_type.pk,
                "title": "Disallowed bill",
                "bill_number": "B 3\u20142026",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_current_state_is_not_writable_through_update(self):
        # State changes must go through the audited transition action; a PATCH
        # carrying current_state has it ignored.
        resp = self._client(self.owner).patch(
            f"/api/bills/{self.bill.public_id}/",
            {"current_state": self.assent.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.current_state, self.draft)

    def test_workflow_type_cannot_be_changed(self):
        other = WorkflowType.objects.create(name="Other Machine", group=self.group)
        resp = self._client(self.owner).patch(
            f"/api/bills/{self.bill.public_id}/",
            {"workflow_type": other.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_transitions_action_lists_the_reachable_transition(self):
        resp = self._client(self.owner).get(
            f"/api/bills/{self.bill.public_id}/transitions/"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([t["name"] for t in resp.json()], ["Send for assent"])

    def test_transition_action_advances_the_state(self):
        resp = self._client(self.owner).post(
            f"/api/bills/{self.bill.public_id}/transition/",
            {"transition": str(self.send.public_id), "comment": "Passed both Houses."},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["to_state_name"], "Assent")

        self.bill.refresh_from_db()
        self.assertEqual(self.bill.current_state, self.assent)

    def test_transition_requiring_a_comment_is_refused_without_one(self):
        resp = self._client(self.owner).post(
            f"/api/bills/{self.bill.public_id}/transition/",
            {"transition": str(self.send.public_id)},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.current_state, self.draft)

    def test_an_unknown_transition_is_a_validation_error(self):
        from uuid import uuid4

        resp = self._client(self.owner).post(
            f"/api/bills/{self.bill.public_id}/transition/",
            {"transition": str(uuid4()), "comment": "x"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_audit_action_returns_the_crud_trail(self):
        resp = self._client(self.owner).get(f"/api/bills/{self.bill.public_id}/audit/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(e["action"] == 0 for e in resp.json()))

    def test_events_action_returns_the_domain_log(self):
        resp = self._client(self.owner).get(f"/api/bills/{self.bill.public_id}/events/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])


class ApiRootListingTests(TestCase):
    """The API root advertises the token endpoints and the resource index."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="root-user", password="pw"
        )

    def test_root_lists_auth_and_resources(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        payload = client.get("/api/").json()

        self.assertIn("obtain_token", payload["authentication"])
        self.assertIn("users", payload["resources"])
        self.assertIn("memberships", payload["resources"])
        self.assertIn("bills", payload["resources"])
        self.assertTrue(payload["resources"]["bills"].startswith("http"))
        self.assertIn("group-by-name", payload["lookups"])


class TokenExpiryTests(TestCase):
    """The absolute TTL on API tokens, and the purge_tokens command."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="ttl-user", password="pw")
        # Token.user is OneToOne, so a second token needs a second user.
        cls.other = User.objects.create_user(username="ttl-other", password="pw")

    @staticmethod
    def _auth_client(token):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return client

    @staticmethod
    def _token_for(user, age):
        """A token for ``user`` whose ``created`` is ``age`` in the past."""
        token = Token.objects.create(user=user)
        Token.objects.filter(pk=token.pk).update(created=timezone.now() - age)
        token.refresh_from_db()
        return token

    @override_settings(TOKEN_TTL=timedelta(days=30))
    def test_fresh_token_is_accepted(self):
        token = Token.objects.create(user=self.user)
        self.assertEqual(
            self._auth_client(token).get("/api/users/me/").status_code, 200
        )

    @override_settings(TOKEN_TTL=timedelta(days=30))
    def test_expired_token_is_refused(self):
        token = self._token_for(self.user, timedelta(days=31))
        resp = self._auth_client(token).get("/api/users/me/")
        self.assertEqual(resp.status_code, 403)  # auth failures surface as 403 here

    @override_settings(TOKEN_TTL=None)
    def test_expiry_disabled_accepts_any_age(self):
        token = self._token_for(self.user, timedelta(days=3650))
        self.assertEqual(
            self._auth_client(token).get("/api/users/me/").status_code, 200
        )

    @override_settings(TOKEN_TTL=timedelta(days=30))
    def test_obtain_rotates_an_expired_key(self):
        stale = self._token_for(self.user, timedelta(days=31))
        with LOCAL_AUTH:
            resp = APIClient().post(
                "/api/auth/token/",
                {"username": "ttl-user", "password": "pw"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        new_key = resp.json()["token"]
        self.assertNotEqual(new_key, stale.key)
        self.assertFalse(Token.objects.filter(key=stale.key).exists())
        fresh = Token.objects.get(key=new_key)
        self.assertEqual(
            self._auth_client(fresh).get("/api/users/me/").status_code, 200
        )

    @override_settings(TOKEN_TTL=timedelta(days=30))
    def test_obtain_reuses_an_unexpired_key(self):
        existing = Token.objects.create(user=self.user)
        with LOCAL_AUTH:
            resp = APIClient().post(
                "/api/auth/token/",
                {"username": "ttl-user", "password": "pw"},
                format="json",
            )
        self.assertEqual(resp.json()["token"], existing.key)

    @override_settings(TOKEN_TTL=timedelta(days=30))
    def test_purge_deletes_only_expired_tokens(self):
        stale = self._token_for(self.user, timedelta(days=40))
        fresh = Token.objects.create(user=self.other)
        out = StringIO()
        call_command("purge_tokens", stdout=out)
        self.assertIn("Deleted 1 expired token(s)", out.getvalue())
        self.assertFalse(Token.objects.filter(pk=stale.pk).exists())
        self.assertTrue(Token.objects.filter(pk=fresh.pk).exists())

    @override_settings(TOKEN_TTL=timedelta(days=30))
    def test_purge_dry_run_keeps_rows(self):
        stale = self._token_for(self.user, timedelta(days=40))
        out = StringIO()
        call_command("purge_tokens", "--dry-run", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertTrue(Token.objects.filter(pk=stale.pk).exists())

    @override_settings(TOKEN_TTL=timedelta(days=30))
    def test_purge_days_override(self):
        token = self._token_for(self.user, timedelta(days=10))
        call_command("purge_tokens", "--days", "5", stdout=StringIO())
        self.assertFalse(Token.objects.filter(pk=token.pk).exists())

    @override_settings(TOKEN_TTL=None)
    def test_purge_is_a_noop_when_expiry_disabled(self):
        token = self._token_for(self.user, timedelta(days=3650))
        out = StringIO()
        call_command("purge_tokens", stdout=out)
        self.assertIn("disabled", out.getvalue())
        self.assertTrue(Token.objects.filter(pk=token.pk).exists())


class TokenThrottleTests(TestCase):
    """The anonymous rate limit on the credential endpoint."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="throttle-user", password="pw"
        )

    def setUp(self):
        # Throttle counters live in the cache; clear them so this class neither
        # inherits counts from nor leaks them into the rest of the suite.
        cache.clear()
        self.addCleanup(cache.clear)

    @staticmethod
    def _post(password="wrong"):
        return APIClient().post(
            "/api/auth/token/",
            {"username": "throttle-user", "password": password},
            format="json",
        )

    def test_requests_beyond_the_rate_are_throttled(self):
        with (
            LOCAL_AUTH,
            mock.patch.dict(TokenEndpointThrottle.THROTTLE_RATES, {"token": "3/min"}),
        ):
            statuses = [self._post().status_code for _ in range(4)]
        self.assertEqual(statuses, [401, 401, 401, 429])

    def test_throttled_response_carries_retry_after(self):
        with (
            LOCAL_AUTH,
            mock.patch.dict(TokenEndpointThrottle.THROTTLE_RATES, {"token": "1/min"}),
        ):
            self._post()
            resp = self._post()
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Retry-After", resp)

    def test_authenticated_endpoints_are_not_throttled(self):
        token = Token.objects.create(user=self.user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        for _ in range(5):
            self.assertEqual(client.get("/api/users/me/").status_code, 200)


class GroupLookupApiTests(TestCase):
    """``GET /api/groups/lookup/`` — group details + member roster by (partial) name."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.health = Group.objects.create(
            name="Portfolio Committee on Health",
            short_name="Health",
            group_type="portfolio_committee",
        )
        cls.education = Group.objects.create(
            name="Portfolio Committee on Education",
            group_type="portfolio_committee",
        )
        cls.finance = Group.objects.create(
            name="Standing Committee on Finance",
            short_name="SCOF",
            group_type="portfolio_committee",
        )
        cls.chair = Role.objects.create(name="Chairperson")
        cls.member_role = Role.objects.create(name="Member")

        cls.alice = User.objects.create_user(
            username="health-alice",
            first_name="Alice",
            last_name="Adams",
            email="alice@example.com",
            is_mp=True,
        )
        cls.bob = User.objects.create_user(
            username="health-bob", first_name="Bob", last_name="Brown"
        )
        cls.carol = User.objects.create_user(
            username="health-carol", first_name="Carol", last_name="Clark"
        )
        GroupMembership.objects.create(user=cls.alice, group=cls.health, role=cls.chair)
        GroupMembership.objects.create(
            user=cls.bob, group=cls.health, role=cls.member_role
        )
        # A past member, to exercise include_inactive.
        GroupMembership.objects.create(
            user=cls.carol, group=cls.health, role=cls.member_role, is_active=False
        )

    @staticmethod
    def _client(user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_requires_authentication(self):
        resp = APIClient().get("/api/groups/lookup/", {"name": "health"})
        self.assertEqual(resp.status_code, 403)

    def test_partial_name_matches_case_insensitively(self):
        resp = self._client(self.alice).get(
            "/api/groups/lookup/", {"name": "portfolio"}
        )
        self.assertEqual(resp.status_code, 200)
        names = [group["name"] for group in resp.json()]
        self.assertIn("Portfolio Committee on Health", names)
        self.assertIn("Portfolio Committee on Education", names)
        self.assertNotIn("Standing Committee on Finance", names)

    def test_short_name_is_matched_too(self):
        resp = self._client(self.alice).get("/api/groups/lookup/", {"name": "SCOF"})
        self.assertEqual(
            [group["name"] for group in resp.json()],
            ["Standing Committee on Finance"],
        )

    def test_exact_narrows_to_a_single_group(self):
        resp = self._client(self.alice).get(
            "/api/groups/lookup/",
            {"name": "Portfolio Committee on Education", "exact": "true"},
        )
        self.assertEqual(
            [group["name"] for group in resp.json()],
            ["Portfolio Committee on Education"],
        )

    def test_missing_name_is_a_validation_error(self):
        resp = self._client(self.alice).get("/api/groups/lookup/")
        self.assertEqual(resp.status_code, 400)

    def test_no_match_is_an_empty_list(self):
        resp = self._client(self.alice).get("/api/groups/lookup/", {"name": "zzz-nope"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_members_come_with_user_and_role_details(self):
        resp = self._client(self.alice).get(
            "/api/groups/lookup/",
            {"name": "Portfolio Committee on Health", "exact": "true"},
        )
        self.assertEqual(resp.status_code, 200)
        group = resp.json()[0]
        self.assertEqual(group["group_type"], "portfolio_committee")

        by_username = {m["user"]["username"]: m for m in group["members"]}
        # The ended membership (carol) is excluded by default.
        self.assertEqual(set(by_username), {"health-alice", "health-bob"})

        alice = by_username["health-alice"]
        self.assertEqual(alice["role"], "Chairperson")
        self.assertEqual(alice["user"]["full_name"], "Alice Adams")
        self.assertEqual(alice["user"]["email"], "alice@example.com")
        self.assertTrue(alice["user"]["is_mp"])
        # Ordered by surname.
        self.assertEqual(
            [m["user"]["last_name"] for m in group["members"]], ["Adams", "Brown"]
        )

    def test_include_inactive_lists_past_members(self):
        resp = self._client(self.alice).get(
            "/api/groups/lookup/",
            {
                "name": "Portfolio Committee on Health",
                "exact": "true",
                "include_inactive": "true",
            },
        )
        members = resp.json()[0]["members"]
        by_username = {m["user"]["username"]: m for m in members}
        self.assertIn("health-carol", by_username)
        self.assertFalse(by_username["health-carol"]["is_active"])


class UserHierarchyApiTests(TestCase):
    """User detail and /me carry the manager (supervisor) and immediate reports."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.manager = User.objects.create_user(
            username="hier-manager",
            first_name="Mary",
            last_name="Manager",
            email="manager@example.com",
        )
        cls.adams = User.objects.create_user(
            username="hier-adams", first_name="Amy", last_name="Adams"
        )
        cls.brown = User.objects.create_user(
            username="hier-brown", first_name="Ben", last_name="Brown"
        )
        cls.unrelated = User.objects.create_user(username="hier-unrelated")
        # Set on the instance (not a queryset update): force_authenticate reuses
        # these objects, so their in-memory supervisor_id must be current too.
        cls.adams.supervisor = cls.manager
        cls.adams.save(update_fields=["supervisor"])
        cls.brown.supervisor = cls.manager
        cls.brown.save(update_fields=["supervisor"])

    @staticmethod
    def _client(user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_detail_nests_the_manager(self):
        resp = self._client(self.unrelated).get("/api/users/hier-adams/")
        self.assertEqual(resp.status_code, 200)
        manager = resp.json()["supervisor"]
        self.assertEqual(manager["username"], "hier-manager")
        self.assertEqual(manager["full_name"], "Mary Manager")

    def test_detail_lists_immediate_subordinates_ordered_by_surname(self):
        resp = self._client(self.unrelated).get("/api/users/hier-manager/")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIsNone(payload["supervisor"])
        self.assertEqual(
            [u["username"] for u in payload["subordinates"]],
            ["hier-adams", "hier-brown"],
        )

    def test_me_includes_the_reporting_line(self):
        resp = self._client(self.adams).get("/api/users/me/")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["supervisor"]["username"], "hier-manager")
        self.assertEqual(payload["subordinates"], [])

    def test_list_stays_flat(self):
        resp = self._client(self.unrelated).get("/api/users/")
        self.assertEqual(resp.status_code, 200)
        for item in resp.json()["results"]:
            self.assertNotIn("subordinates", item)
