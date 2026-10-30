# PWMS API Reference

PWMS exposes a **read-only Django-Ninja API** for registered partner applications
under `/api/v1/`.

The API is intentionally narrow. Three resource families are available:

| Resource | Collection | Detail | Scope |
| --- | --- | --- | --- |
| Users | `GET /api/v1/users/` | `GET /api/v1/users/{public_id}/` | `users:read` |
| Groups | `GET /api/v1/groups/` | `GET /api/v1/groups/{public_id}/` | `groups:read` |
| Memberships | `GET /api/v1/memberships/` | `GET /api/v1/memberships/{public_id}/` | `memberships:read` |
| Bills | `GET /api/v1/bills/` | `GET /api/v1/bills/{public_id}/` | `bills:read` |
| Search | `GET /api/v1/search/` | — | any of the three scopes |

There are no write endpoints: `POST`, `PUT`, `PATCH` and `DELETE` return
`405 Method Not Allowed`. Ninja's interactive docs are served at
`/api/v1/docs` and the raw OpenAPI document at `/api/v1/openapi.json`.

> For printable walkthroughs for partner applications, see the
> [Bills API User Guide (PDF)](./Bills%20API%20User%20Guide.pdf) and the
> [Users API User Guide (PDF)](./Users%20API%20User%20Guide.pdf).

---

## 1. Registering an application

Applications are registered in the Django admin under **API → API
clients**. Each registration records:

| Field | Purpose |
| --- | --- |
| `name` | Application name shown in the admin, e.g. **Parking System**. |
| `description` | What the integration does. |
| `contact_name` / `contact_email` | Human owner of the integration. |
| `client_id` | Public identifier used in the token request. Generated on save. |
| `client_secret_hash` | One-way hash of the secret. The plaintext is never stored. |
| `scopes` | Subset of `users:read`, `memberships:read`, `bills:read`. |
| `rate_limit_per_minute` | Per-key request ceiling across all endpoints. |
| `is_active` | Disabling a client kills its existing access tokens immediately. |

### Issuing the secret

The secret is shown **once**, when the client is created or its secret is
rotated. To issue or replace one:

- in the admin, select the client and run **Rotate client secret (shown once)**;
- or run:

```bash
python manage.py rotate_api_secret parking-system
```

The command prints the new secret to stdout. Store it in the partner's own
secret manager; PWMS only keeps the hash.

### Seeded registrations

The `api.0002_seed_clients` data migration creates the initial three
registrations. They have no secret until one is rotated, as above.

| Name | Contact | Client id | Scopes |
| --- | --- | --- | --- |
| Parking System | `zfo@parliament.gov.za` | `parking-system` | `users:read`, `memberships:read` |
| Main Website | `jarendz@parliament.gov.za` | `main-website` | `bills:read` |
| eNominations | `sblowu@parliament.gov.za` | `enominations` | `memberships:read` |

---

## 2. Authentication

The API uses its own app-key credentials; there is no session, basic-auth or
user-password login.

### Obtain a token pair

```http
POST /api/v1/auth/token/
Content-Type: application/json

{
  "client_id": "parking-system",
  "client_secret": "…",
  "grant_type": "client_credentials"
}
```

```json
{
  "access_token": "…",
  "refresh_token": "…",
  "token_type": "Bearer",
  "expires_in": 900,
  "refresh_expires_in": 86400,
  "scope": "users:read memberships:read"
}
```

Send the access token on every request:

```http
GET /api/v1/users/
Authorization: Bearer <access_token>
```

| Credential | Lifetime | Notes |
| --- | --- | --- |
| Access token | **15 minutes** | Stateless signed payload; embeds the client id and granted scopes. |
| Refresh token | **24 hours** | Opaque random token, stored as a SHA-256 hash. Single use. |

Access tokens are re-checked against the client's **current** `is_active` flag
and scopes on every request, so deactivating a client or removing a scope takes
effect immediately rather than at expiry.

### Refresh

```http
POST /api/v1/auth/token/refresh/
Content-Type: application/json

{"refresh_token": "…"}
```

Returns a new token pair. The presented refresh token is revoked first, so it
cannot be replayed. A refresh token that is expired, revoked or already used
returns `401`.

### Revoke

```http
POST /api/v1/auth/token/revoke/
Content-Type: application/json

{"refresh_token": "…"}
```

Revokes the refresh token and returns `204`. The endpoint is idempotent and
deliberately does not reveal whether the token existed.

The token endpoints are throttled **per client IP** at
`EXTERNAL_API_TOKEN_THROTTLE_RATE` (default `20/min`) to blunt credential
guessing.

### Errors

| Status | Meaning |
| --- | --- |
| `400` | Bad request: unsupported `grant_type`, invalid search query/field/resource, etc. |
| `401` | Missing/invalid/expired bearer token, or bad client credentials. |
| `403` | Authenticated, but the key lacks the endpoint's scope. |
| `404` | The resource does not exist. |
| `405` | The resource is read-only. |
| `422` | A required query parameter (e.g. search `q`) is missing. |
| `429` | Rate limit exceeded; `Retry-After` is set (seconds). |

All error bodies are `{"detail": "…"}`.

---

## 3. Scopes

A scope is granted at registration and embedded in the access token. Every
endpoint checks the one it needs:

| Scope | Grants |
| --- | --- |
| `users:read` | `GET /api/v1/users/`, `GET /api/v1/users/{public_id}/` |
| `groups:read` | `GET /api/v1/groups/`, `GET /api/v1/groups/{public_id}/` |
| `memberships:read` | `GET /api/v1/memberships/`, `GET /api/v1/memberships/{public_id}/` |
| `bills:read` | `GET /api/v1/bills/`, `GET /api/v1/bills/{public_id}/` |

`GET /api/v1/search/` is available to any key and searches only the resource
families it is allowed to read; naming an out-of-scope resource in `resources`
returns `403`. `GET /api/v1/` describes the API and lists only the resources the
calling key can use; `GET /api/v1/me/` returns the registration's own name,
scopes and rate limit.

The seeded partner registrations do not include `groups:read`; grant it in the
admin if a partner needs the group register.

---

## 4. Shared conventions, search and filters

- Resource identifiers are the **`public_id`** UUIDv7 values, never sequential
  database keys.
- Lists return a page:
  `{"count": N, "limit": L, "offset": O, "next": "…", "previous": "…", "results": [...]}`.
  `limit` defaults to `20` and is capped by `EXTERNAL_API_MAX_PAGE_SIZE`
  (default `100`); `offset` defaults to `0`. `next`/`previous` are absolute
  URLs that preserve the request's filters and set the adjacent offset; a page
  at either end has the corresponding link set to `null`.
- Lists support a case-insensitive `search=` across **multiple fields**, an
  optional `search_fields=` allow-list to narrow the matched fields,
  resource-specific filters and an `ordering=` field from a fixed allow-list. An
  unknown ordering value falls back to the resource's default order; an unknown
  search field is a `400`.
- Identifier-style filters accept either the related **`public_id`** or a
  human-readable value (username/email for users; name/short name/slug for
  groups), so callers can pass whichever they stored.
- Timestamps are ISO 8601 with timezone; `public_id` values are UUID strings.

### Cross-resource search

```http
GET /api/v1/search/?q=alice&resources=users,memberships&limit=10&offset=0
Authorization: Bearer <access_token>
```

| Query parameter | Meaning |
| --- | --- |
| `q` | **Required**, at least 2 characters, case-insensitive. |
| `resources` | Optional comma-separated subset of `users`, `groups`, `memberships`, `bills`; defaults to every resource the key may read. A resource outside the key's scopes is a `403`. |
| `limit` | Per-group page size; default `10`, capped by `EXTERNAL_API_MAX_PAGE_SIZE` (`100`). |
| `offset` | Per-group offset; default `0`. |

The term is matched as a case-insensitive **substring** (`ILIKE`) across every
searchable field of each selected resource (see the per-resource tables below);
it is a simple register search, not a ranked full-text search. Each group is a
full page object — the same `count` / `limit` / `offset` / `next` / `previous` /
`results` shape as its list endpoint — so each group paginates independently
with the same `limit`/`offset` (use `resources=users` to page one group on its
own):

```json
{
  "query": "alice",
  "resources": ["users", "groups", "memberships"],
  "skipped": ["bills"],
  "users": {
    "count": 1, "limit": 10, "offset": 0,
    "next": null, "previous": null,
    "results": [ … ]
  },
  "groups": {
    "count": 0, "limit": 10, "offset": 0,
    "next": null, "previous": null,
    "results": []
  },
  "memberships": {
    "count": 1, "limit": 10, "offset": 0,
    "next": null, "previous": null,
    "results": [ … ]
  },
  "bills": {
    "count": 0, "limit": 10, "offset": 0,
    "next": null, "previous": null,
    "results": []
  }
}
```

`skipped` lists the known resource groups that were not searched, either because
`resources` excluded them or because the key has no scope for them. The four
result keys are always present; inaccessible/excluded groups are empty pages.

### Users

| Query parameter | Meaning |
| --- | --- |
| `search` | Case-insensitive across the searchable fields below. |
| `search_fields` | Comma-separated subset of `username`, `first_name`, `middle_name`, `last_name`, `email`, `positiondesc`, `department`, `constituency`, `party_affiliation`, `phone`. |
| `is_active`, `is_mp`, `is_staff_member` | Boolean filters. |
| `department` | A group `public_id`, name, short name or slug. |
| `ordering` | `last_name` (default), `first_name`, `username`, `date_joined`; prefix with `-` to reverse. |

Exposed fields: `public_id`, `username`, `title`, `first_name`, `middle_name`,
`last_name`, `display_name`, `email`, `employee_type`, `positiondesc`,
`department` (nested group reference), `supervisor` (nested user reference:
`public_id`, `username`, `display_name`), `is_active`, `is_mp`,
`is_staff_member`, `constituency`, `party_affiliation`, `phone`.

Deliberately **not** exposed: identity numbers, date of birth, gender, bio,
avatar and the subordinate/reports list. The supervisor's own manager is not
expanded, so the reference cannot recurse.

### Groups

| Query parameter | Meaning |
| --- | --- |
| `search` | Case-insensitive across the group fields below. |
| `search_fields` | Comma-separated subset of `name`, `short_name`, `slug`, `description`, `parent`, `contact_email`, `contact_phone`, `location`. |
| `group_type` | Any group type, e.g. `house`, `committee`, `portfolio_committee`, `administration`, `division`, `section`, `party`, `ministry`, `department`. |
| `parent` | A group `public_id`, name, short name or slug; returns that group's direct children. |
| `is_active` | Boolean filter. |
| `ordering` | `name` (default), `group_type`, `start_date`, `end_date`, `created_at`; prefix with `-` to reverse. |

Exposed fields: `public_id`, `name`, `slug`, `short_name`, `group_type`,
`parent` (nested group reference), `full_path`, `description`, `start_date`,
`end_date`, `is_active`, `contact_email`, `contact_phone`, `location` and
`memberships`.

`memberships` is a nested list of the group's membership rows (current and
ended), each with `public_id`, `user` (`public_id`, `username`, `display_name`),
`role` (`name`, `slug`), `start_date`, `end_date` and `is_active`. Internal
membership `notes` are not exposed. Both the list and detail endpoints include
the nested memberships.

### Memberships

| Query parameter | Meaning |
| --- | --- |
| `search` | Case-insensitive across the user, group and role fields below. |
| `search_fields` | Comma-separated subset of `user_username`, `user_first_name`, `user_middle_name`, `user_last_name`, `user_email`, `group_name`, `group_short_name`, `group_slug`, `role_name`, `role_slug`. |
| `is_active` | Boolean filter. |
| `user` | A user `public_id`, username or email. |
| `group` | A group `public_id`, name, short name or slug. |
| `role` | A role slug or exact name. |
| `ordering` | `-start_date` (default), `start_date`, `end_date`, `created_at`; prefix with `-` to reverse. |

Exposed fields: `public_id`, `user` (nested reference), `group` (nested
reference), `role` (name + slug), `start_date`, `end_date`, `is_active`,
`created_at`, `updated_at`. Internal `notes` are not exposed.

### Bills

| Query parameter | Meaning |
| --- | --- |
| `search` | Case-insensitive across the bill, sponsor, committee and status fields below. |
| `search_fields` | Comma-separated subset of `bill_number`, `title`, `short_title`, `description`, `sponsor_name`, `responsible_committee`, `atc_reference`, `order_paper_reference`, `public_status`. |
| `bill_type` | `section-74`, `section-75`, `section-76` or `section-77`. |
| `house_of_origin` | `na` or `ncop`. |
| `public_status` | A public-status slug or exact name, e.g. `introduced`. |
| `responsible_committee` | A group `public_id`, name, short name or slug. |
| `ordering` | `-created_at` (default), `created_at`, `updated_at`, `bill_number`, `title`, `introduced_date`; prefix with `-` to reverse. |

Exposed fields: `public_id`, `bill_number`, `title`, `short_title`,
`description`, `bill_type`, `house_of_origin`, `sponsor` (nested user
reference), `sponsor_name`, `introduced_date`, `responsible_committee` (nested
group reference), `atc_reference`, `order_paper_reference`,
`bill_document_url`, `public_status`, `current_version`, `identifier`,
`is_overdue`, `deadline`, `priority`, `created_at`, `updated_at`.

`public_status` is the citizen-facing status derived from the bill's internal
workflow state, not the internal state name.

---

## 5. Throttling and usage logs

Every authenticated request counts against
`ApiClient.rate_limit_per_minute` (default `60`). The counter is shared across
all endpoints and keyed by `client_id`, not IP address; a request over the limit
returns `429` with `Retry-After`. Counters live in Django's cache: set
`REDIS_URL` (e.g. `redis://127.0.0.1:6379/1`) so every gunicorn worker shares
the same Redis store and the limit is exact. Without it the project falls back
to a per-process `LocMemCache` for development; both throttles fail open (and
log an error) if the cache itself is unreachable. The increment itself is
atomic (`add` + `incr`), so concurrent workers cannot both slip past the limit.

Each request under `/api/v1/` writes an **API usage log** row:

| Field | Meaning |
| --- | --- |
| `client` | The key used, or blank for a failed token request. |
| `method`, `path`, `endpoint` | Request target. |
| `status_code`, `duration_ms` | Response outcome and server time. |
| `remote_addr`, `user_agent` | Caller metadata. |
| `was_throttled` | True for `429` responses. |
| `created_at` | Request time. |

Usage logs are read-only in the admin (**API → API usage logs**) and can be
filtered by client, status and throttle flag. They are an operational audit
trail, separate from the domain `auditlog` history.
