"""Tests for the partner-facing Django-Ninja API (``pwms.api``)."""

import json
from datetime import timedelta
from io import StringIO
from unittest import mock
from urllib.parse import urlencode
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .api.models import ApiClient, ApiRefreshToken, ApiUsageLog
from .api.scopes import Scope
from .api.tokens import ACCESS_TOKEN_SALT
from .models import (
    Bill,
    Group,
    GroupMembership,
    PublicStatus,
    Role,
    State,
    WorkflowType,
)

User = get_user_model()

TOKEN_URL = "/api/v1/auth/token/"
REFRESH_URL = "/api/v1/auth/token/refresh/"
REVOKE_URL = "/api/v1/auth/token/revoke/"


class ApiTestCase(TestCase):
    def setUp(self):
        cache.clear()

    def make_client(self, name, scopes, rate_limit_per_minute=60):
        client = ApiClient(
            name=name,
            contact_email=f"{name.lower().replace(' ', '.')}@example.org",
            scopes=list(scopes),
            rate_limit_per_minute=rate_limit_per_minute,
        )
        secret = client.set_client_secret("test-secret")
        client.save()
        return client, secret

    def obtain(self, client, secret):
        return self.client.post(
            TOKEN_URL,
            data=json.dumps({"client_id": client.client_id, "client_secret": secret}),
            content_type="application/json",
        )

    def access_token(self, scopes):
        client, secret = self.make_client(f"Client {uuid4().hex[:8]}", scopes)
        response = self.obtain(client, secret)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["access_token"], client

    def auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class SeededRegistrationTests(TestCase):
    """The data migration registers the three initial apps with fixed scopes."""

    def test_three_registrations_exist_with_expected_scopes(self):
        expected = {
            "Parking System": (
                "zfo@parliament.gov.za",
                {"users:read", "memberships:read"},
            ),
            "Main Website": ("jarendz@parliament.gov.za", {"bills:read"}),
            "eNominations": ("sblowu@parliament.gov.za", {"memberships:read"}),
        }
        for name, (email, scopes) in expected.items():
            client = ApiClient.objects.get(name=name)
            self.assertEqual(client.contact_email, email)
            self.assertEqual(set(client.enabled_scopes), scopes)
            self.assertEqual(client.client_id, name.lower().replace(" ", "-"))

    def test_seeded_clients_have_no_secret_until_one_is_issued(self):
        for client in ApiClient.objects.filter(
            client_id__in=["parking-system", "main-website", "enominations"]
        ):
            self.assertFalse(client.has_secret)

    def test_unknown_scope_is_rejected(self):
        client = ApiClient(name="Bad Scope", contact_email="bad@example.org")
        client.set_client_secret("secret")
        client.scopes = ["users:read", "admin:write"]
        with self.assertRaises(ValidationError):
            client.save()

    def test_rotate_api_secret_command_issues_and_prints_a_secret(self):
        client = ApiClient.objects.get(client_id="parking-system")
        output = StringIO()

        call_command("rotate_api_secret", client.client_id, stdout=output)

        client.refresh_from_db()
        self.assertTrue(client.has_secret)
        self.assertIn("Client secret", output.getvalue())


class AdminRegistrationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="admin", email="admin@example.org", password="pw"
        )
        self.client.force_login(self.admin)

    def test_admin_can_register_a_client_and_secret_is_shown(self):
        response = self.client.post(
            reverse("admin:api_apiclient_add"),
            {
                "name": "New Partner Service",
                "description": "Integration used by the tests.",
                "contact_name": "Test Owner",
                "contact_email": "owner@example.org",
                "scopes": ["users:read", "bills:read"],
                "rate_limit_per_minute": 30,
                "is_active": "on",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        client = ApiClient.objects.get(name="New Partner Service")
        self.assertTrue(client.has_secret)
        self.assertEqual(client.enabled_scopes, ["users:read", "bills:read"])
        self.assertContains(response, "Client secret")

    def test_admin_can_rotate_an_existing_secret(self):
        client = ApiClient(name="Rotate Me", contact_email="rotate@example.org")
        client.set_client_secret("initial-secret")
        client.save()
        previous_hash = client.client_secret_hash

        response = self.client.post(
            reverse("admin:api_apiclient_changelist"),
            {
                "action": "rotate_secret",
                "_selected_action": [str(client.pk)],
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        client.refresh_from_db()
        self.assertNotEqual(client.client_secret_hash, previous_hash)
        self.assertContains(response, "New client secret")


class TokenLifecycleTests(ApiTestCase):
    def test_token_pair_has_expected_lifetimes(self):
        client, secret = self.make_client("Token Client", [Scope.USERS_READ])
        response = self.obtain(client, secret)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["access_token"])
        self.assertTrue(payload["refresh_token"])
        self.assertEqual(payload["token_type"], "Bearer")
        self.assertEqual(payload["expires_in"], 15 * 60)
        self.assertEqual(payload["refresh_expires_in"], 24 * 60 * 60)
        self.assertEqual(payload["scope"], "users:read")

    def test_bad_secret_and_unknown_client_are_401(self):
        client, _secret = self.make_client("Bad Secret", [Scope.USERS_READ])
        response = self.obtain(client, "wrong")
        self.assertEqual(response.status_code, 401)

        response = self.client.post(
            TOKEN_URL,
            data=json.dumps({"client_id": "does-not-exist", "client_secret": "wrong"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_unsupported_grant_type_is_400(self):
        client, secret = self.make_client("Grant Client", [Scope.USERS_READ])
        response = self.client.post(
            TOKEN_URL,
            data=json.dumps(
                {
                    "client_id": client.client_id,
                    "client_secret": secret,
                    "grant_type": "password",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_inactive_client_cannot_obtain_or_use_token(self):
        client, secret = self.make_client("Inactive", [Scope.USERS_READ])
        token = self.obtain(client, secret).json()["access_token"]

        client.is_active = False
        client.save()

        self.assertEqual(self.obtain(client, secret).status_code, 401)
        response = self.client.get("/api/v1/me/", **self.auth(token))
        self.assertEqual(response.status_code, 401)

    def test_refresh_rotates_and_old_token_stops_working(self):
        client, secret = self.make_client("Refresh Client", [Scope.USERS_READ])
        pair = self.obtain(client, secret).json()

        response = self.client.post(
            REFRESH_URL,
            data=json.dumps({"refresh_token": pair["refresh_token"]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        rotated = response.json()
        self.assertNotEqual(rotated["refresh_token"], pair["refresh_token"])
        # Rotation must leave exactly one live refresh token: the replacement.
        self.assertEqual(
            ApiRefreshToken.objects.filter(
                client=client, revoked_at__isnull=True
            ).count(),
            1,
        )

        replay = self.client.post(
            REFRESH_URL,
            data=json.dumps({"refresh_token": pair["refresh_token"]}),
            content_type="application/json",
        )
        self.assertEqual(replay.status_code, 401)

        fresh_access = self.client.get(
            "/api/v1/me/", **self.auth(rotated["access_token"])
        )
        self.assertEqual(fresh_access.status_code, 200)

    def test_expired_access_token_is_rejected(self):
        client, _secret = self.make_client("Expired Access", [Scope.USERS_READ])
        now = int(timezone.now().timestamp())
        payload = {
            "sub": client.client_id,
            "scopes": [Scope.USERS_READ],
            "typ": "access",
            "iat": now - 1000,
            "exp": now - 1,
            "jti": "expired-token",
        }
        token = signing.dumps(
            payload,
            salt=ACCESS_TOKEN_SALT,
            key=settings.EXTERNAL_API_SIGNING_KEY,
            compress=True,
        )

        response = self.client.get("/api/v1/me/", **self.auth(token))
        self.assertEqual(response.status_code, 401)

    def test_signed_token_of_the_wrong_type_is_rejected(self):
        client, _secret = self.make_client("Wrong Type", [Scope.USERS_READ])
        now = int(timezone.now().timestamp())
        payload = {
            "sub": client.client_id,
            "scopes": [Scope.USERS_READ],
            "typ": "refresh",
            "iat": now,
            "exp": now + 900,
            "jti": "wrong-type",
        }
        token = signing.dumps(
            payload,
            salt=ACCESS_TOKEN_SALT,
            key=settings.EXTERNAL_API_SIGNING_KEY,
            compress=True,
        )

        response = self.client.get("/api/v1/me/", **self.auth(token))
        self.assertEqual(response.status_code, 401)

    def test_expired_refresh_token_is_rejected(self):
        client, _secret = self.make_client("Expired Client", [Scope.USERS_READ])
        raw = "expired-refresh-token"
        ApiRefreshToken.objects.create(
            client=client,
            token_hash=ApiRefreshToken.hash_token(raw),
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        response = self.client.post(
            REFRESH_URL,
            data=json.dumps({"refresh_token": raw}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_revoke_is_idempotent(self):
        client, secret = self.make_client("Revoke Client", [Scope.USERS_READ])
        pair = self.obtain(client, secret).json()

        for _ in range(2):
            response = self.client.post(
                REVOKE_URL,
                data=json.dumps({"refresh_token": pair["refresh_token"]}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 204)

        response = self.client.post(
            REFRESH_URL,
            data=json.dumps({"refresh_token": pair["refresh_token"]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)


class ScopedResourceTests(ApiTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="alice",
            email="alice@example.com",
            password="pw",
            first_name="Alice",
            last_name="Molefe",
            is_mp=True,
            is_staff_member=False,
            phone="0123456789",
        )
        cls.colleague = User.objects.create_user(
            username="brian",
            email="brian@example.com",
            password="pw",
            first_name="Brian",
            last_name="Dlamini",
        )
        cls.group, _ = Group.objects.get_or_create(
            name="API Test Committee", group_type="committee"
        )
        cls.user.department = cls.group
        cls.user.supervisor = cls.colleague
        cls.user.save(update_fields=["department", "supervisor"])
        cls.role, _ = Role.objects.get_or_create(name="Member")
        cls.membership = GroupMembership.objects.create(
            user=cls.user, group=cls.group, role=cls.role
        )

        cls.workflow_type, _ = WorkflowType.objects.get_or_create(name="Bills")
        cls.status, _ = PublicStatus.objects.get_or_create(name="Introduced")
        cls.state = State.objects.create(
            workflow_type=cls.workflow_type,
            name="Introduced in the House",
            is_initial=True,
            public_status=cls.status,
        )
        cls.bill = Bill.objects.create(
            workflow_type=cls.workflow_type,
            current_state=cls.state,
            bill_number="B 1-2026",
            title="A test bill",
            short_title="Test Bill",
            bill_type=Bill.SECTION_75,
            house_of_origin=Bill.NA,
            responsible_committee=cls.group,
            owner=cls.user,
        )

    def test_authentication_is_required(self):
        for path in (
            "/api/v1/",
            "/api/v1/users/",
            "/api/v1/groups/",
            "/api/v1/memberships/",
            "/api/v1/bills/",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)

    def test_invalid_bearer_token_is_401(self):
        response = self.client.get("/api/v1/users/", **self.auth("not-a-token"))
        self.assertEqual(response.status_code, 401)

    def test_users_scope_reads_users_and_nothing_else(self):
        token, _ = self.access_token([Scope.USERS_READ])

        listing = self.client.get("/api/v1/users/?search=alice", **self.auth(token))
        self.assertEqual(listing.status_code, 200)
        payload = listing.json()
        self.assertEqual(payload["count"], 1)
        record = payload["results"][0]
        self.assertEqual(record["username"], "alice")
        self.assertEqual(record["display_name"], "Alice Molefe")
        self.assertEqual(record["supervisor"]["username"], "brian")
        self.assertNotIn("idno_encrypted", record)
        self.assertNotIn("date_of_birth", record)

        detail = self.client.get(
            f"/api/v1/users/{self.user.public_id}/", **self.auth(token)
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["department"]["name"], "API Test Committee")
        self.assertEqual(detail.json()["supervisor"]["display_name"], "Brian Dlamini")

        blocked = self.client.get("/api/v1/memberships/", **self.auth(token))
        self.assertEqual(blocked.status_code, 403)
        blocked = self.client.get("/api/v1/groups/", **self.auth(token))
        self.assertEqual(blocked.status_code, 403)
        blocked = self.client.get("/api/v1/bills/", **self.auth(token))
        self.assertEqual(blocked.status_code, 403)

    def test_groups_scope_reads_groups_with_nested_memberships(self):
        token, _ = self.access_token([Scope.GROUPS_READ])

        listing = self.client.get(
            f"/api/v1/groups/?{urlencode({'search': self.group.name})}",
            **self.auth(token),
        )
        self.assertEqual(listing.status_code, 200)
        payload = listing.json()
        self.assertEqual(payload["count"], 1)
        record = payload["results"][0]
        self.assertEqual(record["name"], "API Test Committee")
        self.assertEqual(record["group_type"], "committee")
        self.assertEqual(record["full_path"], "API Test Committee")
        self.assertEqual(len(record["memberships"]), 1)
        member = record["memberships"][0]
        self.assertEqual(member["user"]["username"], "alice")
        self.assertEqual(member["role"]["slug"], "member")
        self.assertTrue(member["is_active"])
        self.assertNotIn("notes", member)

        detail = self.client.get(
            f"/api/v1/groups/{self.group.public_id}/", **self.auth(token)
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["memberships"][0]["user"]["username"], "alice")

        root = self.client.get("/api/v1/", **self.auth(token)).json()
        self.assertEqual(
            {resource["name"] for resource in root["resources"]}, {"groups", "search"}
        )

        for path in ("/api/v1/users/", "/api/v1/memberships/", "/api/v1/bills/"):
            blocked = self.client.get(path, **self.auth(token))
            self.assertEqual(blocked.status_code, 403, path)

    def test_group_search_filters_and_search_fields(self):
        child, _ = Group.objects.get_or_create(
            name="API Health Subcommittee",
            parent=self.group,
            group_type="committee",
            defaults={"location": "API Test Location"},
        )
        token, _ = self.access_token([Scope.GROUPS_READ])

        by_type = self.client.get(
            f"/api/v1/groups/?{urlencode({'search': 'API', 'group_type': 'committee'})}",
            **self.auth(token),
        )
        self.assertEqual(by_type.json()["count"], 2)

        by_name = self.client.get(
            f"/api/v1/groups/?{urlencode({'search': 'API Health'})}",
            **self.auth(token),
        )
        self.assertEqual(by_name.json()["count"], 1)
        self.assertEqual(by_name.json()["results"][0]["name"], child.name)
        self.assertEqual(
            by_name.json()["results"][0]["parent"]["name"], self.group.name
        )

        by_field = self.client.get(
            f"/api/v1/groups/?{urlencode({'search': 'API Test Location', 'search_fields': 'location'})}",
            **self.auth(token),
        )
        self.assertEqual(by_field.json()["count"], 1)

        miss = self.client.get(
            f"/api/v1/groups/?{urlencode({'search': 'API Test Committee', 'search_fields': 'location'})}",
            **self.auth(token),
        )
        self.assertEqual(miss.json()["count"], 0)

        by_parent_name = self.client.get(
            f"/api/v1/groups/?{urlencode({'parent': self.group.name})}",
            **self.auth(token),
        )
        self.assertEqual(by_parent_name.json()["count"], 1)
        self.assertEqual(by_parent_name.json()["results"][0]["name"], child.name)

        by_parent_id = self.client.get(
            f"/api/v1/groups/?{urlencode({'parent': str(self.group.public_id)})}",
            **self.auth(token),
        )
        self.assertEqual(by_parent_id.json()["count"], 1)

        inactive = self.client.get(
            f"/api/v1/groups/?{urlencode({'is_active': 'false', 'search': 'API'})}",
            **self.auth(token),
        )
        self.assertEqual(inactive.json()["count"], 0)

    def test_unified_search_includes_groups(self):
        token, _ = self.access_token([Scope.GROUPS_READ])
        response = self.client.get(
            f"/api/v1/search/?{urlencode({'q': 'API Test'})}", **self.auth(token)
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["resources"], ["groups"])
        self.assertEqual(payload["groups"]["count"], 1)
        self.assertEqual(
            payload["groups"]["results"][0]["memberships"][0]["user"]["username"],
            "alice",
        )
        self.assertEqual(payload["users"]["count"], 0)

    def test_memberships_scope_reads_memberships_and_nothing_else(self):
        token, _ = self.access_token([Scope.MEMBERSHIPS_READ])

        listing = self.client.get("/api/v1/memberships/", **self.auth(token))
        self.assertEqual(listing.status_code, 200)
        payload = listing.json()
        self.assertEqual(payload["count"], 1)
        record = payload["results"][0]
        self.assertEqual(record["user"]["username"], "alice")
        self.assertEqual(record["group"]["public_id"], str(self.group.public_id))
        self.assertEqual(record["role"]["slug"], "member")
        self.assertNotIn("notes", record)

        detail = self.client.get(
            f"/api/v1/memberships/{self.membership.public_id}/", **self.auth(token)
        )
        self.assertEqual(detail.status_code, 200)

        blocked = self.client.get("/api/v1/users/", **self.auth(token))
        self.assertEqual(blocked.status_code, 403)

    def test_bills_scope_reads_bills_and_exposes_public_status(self):
        token, _ = self.access_token([Scope.BILLS_READ])

        listing = self.client.get("/api/v1/bills/?search=B 1", **self.auth(token))
        self.assertEqual(listing.status_code, 200)
        payload = listing.json()
        self.assertEqual(payload["count"], 1)
        record = payload["results"][0]
        self.assertEqual(record["bill_number"], "B 1-2026")
        self.assertEqual(record["public_status"], "Introduced")
        self.assertEqual(record["identifier"], "B 1-2026")
        self.assertNotIn("owner", record)

        detail = self.client.get(
            f"/api/v1/bills/{self.bill.public_id}/", **self.auth(token)
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["title"], "A test bill")

        blocked = self.client.get("/api/v1/users/", **self.auth(token))
        self.assertEqual(blocked.status_code, 403)

    def test_unified_search_covers_scoped_resources(self):
        token, _ = self.access_token([Scope.USERS_READ, Scope.MEMBERSHIPS_READ])
        response = self.client.get(
            f"/api/v1/search/?{urlencode({'q': 'alice'})}", **self.auth(token)
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["query"], "alice")
        self.assertEqual(payload["resources"], ["users", "memberships"])
        self.assertEqual(payload["skipped"], ["groups", "bills"])
        self.assertEqual(payload["users"]["count"], 1)
        self.assertEqual(payload["memberships"]["count"], 1)
        self.assertEqual(payload["users"]["results"][0]["username"], "alice")
        self.assertEqual(
            payload["memberships"]["results"][0]["user"]["username"], "alice"
        )
        self.assertEqual(payload["bills"]["count"], 0)
        self.assertEqual(payload["bills"]["results"], [])

        denied = self.client.get(
            f"/api/v1/search/?{urlencode({'q': 'alice', 'resources': 'bills'})}",
            **self.auth(token),
        )
        self.assertEqual(denied.status_code, 403)

    def test_unified_search_matches_multiple_fields(self):
        token, _ = self.access_token(
            [Scope.USERS_READ, Scope.MEMBERSHIPS_READ, Scope.BILLS_READ]
        )

        by_email = self.client.get(
            f"/api/v1/search/?{urlencode({'q': 'alice@example.com'})}",
            **self.auth(token),
        )
        self.assertEqual(by_email.json()["users"]["count"], 1)

        by_group = self.client.get(
            f"/api/v1/search/?{urlencode({'q': 'API Test'})}", **self.auth(token)
        )
        counts = by_group.json()
        self.assertEqual(counts["memberships"]["count"], 1)
        self.assertEqual(counts["bills"]["count"], 1)

        by_short_title = self.client.get(
            f"/api/v1/search/?{urlencode({'q': 'Test Bill'})}", **self.auth(token)
        )
        self.assertEqual(by_short_title.json()["bills"]["count"], 1)

    def test_unified_search_validates_query_and_resources(self):
        token, _ = self.access_token([Scope.USERS_READ])

        too_short = self.client.get("/api/v1/search/?q=a", **self.auth(token))
        self.assertEqual(too_short.status_code, 400)

        unknown = self.client.get(
            f"/api/v1/search/?{urlencode({'q': 'alice', 'resources': 'widgets'})}",
            **self.auth(token),
        )
        self.assertEqual(unknown.status_code, 400)

        missing = self.client.get("/api/v1/search/", **self.auth(token))
        self.assertEqual(missing.status_code, 422)

    def test_search_fields_restricts_which_fields_are_matched(self):
        token, _ = self.access_token([Scope.USERS_READ])

        miss = self.client.get(
            f"/api/v1/users/?{urlencode({'search': 'Molefe', 'search_fields': 'email'})}",
            **self.auth(token),
        )
        self.assertEqual(miss.json()["count"], 0)

        hit = self.client.get(
            f"/api/v1/users/?{urlencode({'search': 'alice@example.com', 'search_fields': 'email'})}",
            **self.auth(token),
        )
        self.assertEqual(hit.json()["count"], 1)

        bad = self.client.get(
            f"/api/v1/users/?{urlencode({'search': 'alice', 'search_fields': 'bogus'})}",
            **self.auth(token),
        )
        self.assertEqual(bad.status_code, 400)

    def test_identifier_filters_accept_public_id_or_names(self):
        token, _ = self.access_token(
            [Scope.USERS_READ, Scope.MEMBERSHIPS_READ, Scope.BILLS_READ]
        )

        for value in ("alice", str(self.user.public_id)):
            response = self.client.get(
                f"/api/v1/memberships/?{urlencode({'user': value})}",
                **self.auth(token),
            )
            self.assertEqual(response.json()["count"], 1, value)

        for value in ("API Test Committee", str(self.group.public_id)):
            response = self.client.get(
                f"/api/v1/memberships/?{urlencode({'group': value})}",
                **self.auth(token),
            )
            self.assertEqual(response.json()["count"], 1, value)

            response = self.client.get(
                f"/api/v1/users/?{urlencode({'department': value})}",
                **self.auth(token),
            )
            self.assertEqual(response.json()["count"], 1, value)

            response = self.client.get(
                f"/api/v1/bills/?{urlencode({'responsible_committee': value})}",
                **self.auth(token),
            )
            self.assertEqual(response.json()["count"], 1, value)

    def test_list_pagination_includes_navigation_links(self):
        token, _ = self.access_token([Scope.USERS_READ])

        first = self.client.get(
            "/api/v1/users/?limit=1&offset=0&ordering=-username",
            **self.auth(token),
        ).json()
        self.assertEqual(first["count"], 2)
        self.assertEqual(len(first["results"]), 1)
        self.assertIsNone(first["previous"])
        self.assertIn("offset=1", first["next"])
        self.assertIn("ordering=-username", first["next"])

        second = self.client.get(
            "/api/v1/users/?limit=1&offset=1&ordering=-username",
            **self.auth(token),
        ).json()
        self.assertIsNone(second["next"])
        self.assertIn("offset=0", second["previous"])

    def test_search_results_are_paginated_per_group(self):
        User.objects.create_user(
            username="alicia",
            email="alicia@example.com",
            password="pw",
            first_name="Alicia",
            last_name="Ndlovu",
        )
        token, _ = self.access_token([Scope.USERS_READ])

        first = self.client.get(
            f"/api/v1/search/?{urlencode({'q': 'ali', 'resources': 'users', 'limit': 1, 'offset': 0})}",
            **self.auth(token),
        ).json()
        self.assertEqual(first["users"]["count"], 2)
        self.assertEqual(len(first["users"]["results"]), 1)
        self.assertIsNone(first["users"]["previous"])
        self.assertIn("offset=1", first["users"]["next"])
        self.assertIn("q=ali", first["users"]["next"])
        self.assertIn("resources=users", first["users"]["next"])
        # Groups that were not selected are still present as empty pages.
        self.assertEqual(first["memberships"]["count"], 0)
        self.assertEqual(first["bills"]["results"], [])

        second = self.client.get(
            f"/api/v1/search/?{urlencode({'q': 'ali', 'resources': 'users', 'limit': 1, 'offset': 1})}",
            **self.auth(token),
        ).json()
        self.assertIsNone(second["users"]["next"])
        self.assertIn("offset=0", second["users"]["previous"])

    def test_missing_resource_is_404(self):
        token, _ = self.access_token([Scope.BILLS_READ])
        response = self.client.get(f"/api/v1/bills/{uuid4()}/", **self.auth(token))
        self.assertEqual(response.status_code, 404)

    def test_resources_are_read_only(self):
        token, _ = self.access_token(
            [Scope.USERS_READ, Scope.MEMBERSHIPS_READ, Scope.BILLS_READ]
        )
        for path in ("/api/v1/users/", "/api/v1/memberships/", "/api/v1/bills/"):
            self.assertEqual(
                self.client.post(
                    path, data="{}", content_type="application/json", **self.auth(token)
                ).status_code,
                405,
                path,
            )
            self.assertEqual(
                self.client.delete(path, **self.auth(token)).status_code,
                405,
                path,
            )

    def test_root_and_me_reflect_scopes(self):
        token, client = self.access_token([Scope.USERS_READ, Scope.BILLS_READ])

        root = self.client.get("/api/v1/", **self.auth(token))
        self.assertEqual(root.status_code, 200)
        payload = root.json()
        names = {resource["name"] for resource in payload["resources"]}
        self.assertEqual(names, {"users", "bills", "search"})
        self.assertEqual(payload["client"], client.name)

        me = self.client.get("/api/v1/me/", **self.auth(token))
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["client_id"], client.client_id)
        self.assertEqual(set(me.json()["scopes"]), {"users:read", "bills:read"})


class ThrottleAndUsageLogTests(ApiTestCase):
    def test_per_key_rate_limit_returns_429_with_retry_after(self):
        token, client = self.access_token([Scope.USERS_READ])
        client.rate_limit_per_minute = 1
        client.save()
        cache.clear()

        # Freeze the clock so both requests land in the same one-minute bucket.
        with mock.patch("pwms.api.throttling.time.time", return_value=1_000_000.0):
            first = self.client.get("/api/v1/users/", **self.auth(token))
            self.assertEqual(first.status_code, 200)

            second = self.client.get("/api/v1/users/", **self.auth(token))
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)

    def test_client_throttle_fails_open_when_the_cache_is_down(self):
        token, _client = self.access_token([Scope.USERS_READ])

        with mock.patch(
            "pwms.api.throttling.cache.add",
            side_effect=ConnectionError("redis down"),
        ):
            response = self.client.get("/api/v1/users/", **self.auth(token))

        self.assertEqual(response.status_code, 200)

    def test_usage_log_records_key_request_and_throttle(self):
        client, secret = self.make_client(
            "Usage Client", [Scope.USERS_READ], rate_limit_per_minute=1
        )
        pair = self.obtain(client, secret).json()
        token = pair["access_token"]

        with mock.patch("pwms.api.throttling.time.time", return_value=1_000_000.0):
            self.client.get("/api/v1/users/", **self.auth(token))
            self.client.get("/api/v1/users/", **self.auth(token))

        logs = ApiUsageLog.objects.filter(
            client=client, path="/api/v1/users/"
        ).order_by("created_at")
        self.assertEqual(logs.count(), 2)
        self.assertEqual(logs[0].status_code, 200)
        self.assertFalse(logs[0].was_throttled)
        self.assertEqual(logs[0].method, "GET")
        self.assertEqual(logs[1].status_code, 429)
        self.assertTrue(logs[1].was_throttled)

    def test_failed_token_attempt_is_logged_anonymous(self):
        response = self.client.post(
            TOKEN_URL,
            data=json.dumps({"client_id": "does-not-exist", "client_secret": "wrong"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

        log = ApiUsageLog.objects.filter(path=TOKEN_URL).latest("created_at")
        self.assertIsNone(log.client)
        self.assertEqual(log.status_code, 401)

    def test_failed_token_attempt_for_known_key_is_attributed(self):
        client, _secret = self.make_client("Known Client", [Scope.USERS_READ])
        self.obtain(client, "wrong")

        log = ApiUsageLog.objects.filter(client=client, path=TOKEN_URL).latest(
            "created_at"
        )
        self.assertEqual(log.status_code, 401)
