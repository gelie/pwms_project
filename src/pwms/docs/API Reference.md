# PWMS API Reference

PWMS exposes a **read-only Django-Ninja API** for registered partner applications
under `/api/v1/`.

The API is intentionally narrow. Five resource families are available:

| Resource | Collection | Detail | Scope |
| --- | --- | --- | --- |
| Users | `GET /api/v1/users/` | `GET /api/v1/users/{public_id}/` | `users:read` |
| Groups | `GET /api/v1/groups/` | `GET /api/v1/groups/{public_id}/` | `groups:read` |
| Memberships | `GET /api/v1/memberships/` | `GET /api/v1/memberships/{public_id}/` | `memberships:read` |
| Bills | `GET /api/v1/bills/` | `GET /api/v1/bills/{public_id}/` | `bills:read` |
| Meetings | `GET /api/v1/meetings/` | `GET /api/v1/meetings/{public_id}/` | `meetings:read` |
| Search | `GET /api/v1/search/` | — | any of the users/groups/memberships/bills scopes |

There are no write endpoints for these resource families: `POST`, `PUT`,
`PATCH` and `DELETE` return `405 Method Not Allowed`. The exceptions are the
**Bill update receiver** at `POST /api/v1/bill-updates/` and its bi-directional
**rejection callback** at `POST /api/v1/bill-updates/rejections/` (§6). They
exist only so PWMS can test its own outbound Bill notifications before the
partner system exists; they are authenticated with a shared secret, not an
application token, and are not part of the partner read contract. Ninja's
interactive docs are served at `/api/v1/docs` and the raw OpenAPI document at
`/api/v1/openapi.json`.

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
| `scopes` | Subset of `users:read`, `groups:read`, `memberships:read`, `bills:read`, `meetings:read`. |
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
  "expires_in": 3600,
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
| Access token | **60 minutes** | Stateless signed payload; embeds the client id and granted scopes. |
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
| `meetings:read` | `GET /api/v1/meetings/`, `GET /api/v1/meetings/{public_id}/` |

`GET /api/v1/search/` is available to any key and searches only the resource
families it is allowed to read (users, groups, memberships and bills); naming an
out-of-scope resource in `resources` returns `403`. Meetings are not part of the
cross-resource search — query the meetings collection directly with `user=`,
`group=`, `start=` and `end=`. `GET /api/v1/` describes the API and lists only
the resources the calling key can use; `GET /api/v1/me/` returns the
registration's own name, scopes and rate limit.

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
| `group_type` | Any group type, e.g. `parliament`, `house`, `committee`, `delegation`, `members`, `parties`, `party`, `administration`, `branch`, `division`, `office`, `section`, `unit`, `ministry`, `department`, `province`. |
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
`bill_document_url`, `public_status`, `current_version`, `versions`,
`attachments`, `notes`, `referrals`, `identifier`, `is_overdue`, `deadline`,
`priority`, `created_at`, `updated_at`.

`public_status` is the citizen-facing status derived from the bill's internal
workflow state, not the internal state name.

`versions` is the bill's preserved version history, newest first. Each entry
carries `public_id`, `version_label`, `version_type` (`introduced`, `amended` or
`amendment_schedule`), `version_date`, `document_url`, `notes`, `recorded_by`
(nested user reference), `is_current`, `created_at` and `updated_at`.

`attachments` lists the SharePoint documents filed against the bill. Each entry
carries `public_id`, `name`, `type`, `mimetype`, `size`,
`sharepoint_web_url`, `download_url`, `sharepoint_folder_path`, `uploaded_by`
(nested user reference), `created_at`, `updated_at` and its own `versions`.
An attachment version carries `public_id`, `version_id` (SharePoint's label,
e.g. `2.0`), `size`, `modified_at`, `modified_by`, `is_current` and
`created_at`. Document content is fetched through the SharePoint URLs; the API
never streams file bytes.

`notes` is the bill's note log, newest first. Each entry carries `public_id`,
`author` (nested user reference), `body`, `created_at` and `updated_at`.

`referrals` lists the groups the bill was referred to, newest first. Each entry
carries `public_id`, `referred_to` (nested group reference), `referred_by`
(nested user reference), `referred_at`, `due_date`, `status` (`open`,
`responded`, `recalled`, `expired` or `cancelled`), `is_open`, `is_overdue`,
`responded_at` / `responded_by`, `response_document_url`, `response_notes`,
`recalled_at` / `recalled_by`, `recall_reason`, `notes`, `created_at` and
`updated_at`.

`versions`, `attachments`, `notes` and `referrals` are always present as arrays —
empty when the bill has none recorded.

### Meetings

Sittings of the Events workflow: House plenaries, joint sittings, portfolio /
select / ad hoc committee meetings, and staff engagements (management,
administration, task teams, workshops, public participation). The headline use
is **a member's diary**: pass `user=` and the collection returns the meetings
that person is a party to.

```http
GET /api/v1/meetings/?user=jsmith&start=2026-01-01&end=2026-06-30
Authorization: Bearer <access_token>
```

`user=` is the precise filter (it also returns the member's own register row).
The same list is available as a nested route,
`GET /api/v1/users/{public_id}/meetings/`, for integrations that address the
person first; it accepts the same filters below (except `user=` itself) and
returns `404` when the user does not exist.
`search=` additionally matches attendee usernames and names, so
`?search=Khumalo` finds meetings where a person called Khumalo is on the
register; combine it with `user=` and the period/group filters when both the
person and their register row are needed.

| Query parameter | Meaning |
| --- | --- |
| `user` | A user `public_id`, username or email. Narrows the list to that person's meetings: those where they are on the attendance register, chair or minute the meeting, own or are assigned it, or where the convening group is one they actively belong to. Each result then carries `member_attendance`. An identifier that matches no user is a `404` (rather than a silently empty page), so a bad username is easy to diagnose. |
| `group` | A group `public_id`, name, short name or slug. Filters on the **convening** (`host`) group. |
| `meeting_type` | One type or a comma-separated list: `plenary`, `joint_sitting`, `committee`, `portfolio_committee`, `select_committee`, `ad_hoc_committee`, `administration`, `management`, `task_team`, `workshop`, `public_participation`, `other`. |
| `start`, `end` | Inclusive `YYYY-MM-DD` bounds on the meeting's start date. Either may be supplied alone. A malformed date, or `start` after `end`, is a `400`. |
| `state` | A workflow state slug or exact name, e.g. `scheduled`, `in-progress`, `completed`, `postponed`, `cancelled`. |
| `minutes_status` | `not_recorded`, `draft`, `filed` or `approved`. |
| `attendance_mode` | `on_premise`, `hybrid` or `virtual`. |
| `search` | Case-insensitive across the meeting fields below. |
| `search_fields` | Comma-separated subset of `reference_number`, `title`, `description`, `host_group`, `chair`, `secretary`, `venue`, `off_site_venue`, `attendee`. |
| `ordering` | `-start_datetime` (default), `start_datetime`, `end_datetime`, `title`, `meeting_type`, `created_at`; prefix with `-` to reverse. |

Exposed fields: `public_id`, `reference_number`, `title`, `description`,
`meeting_type`, `meeting_type_display`, `start_datetime`, `end_datetime`,
`all_day`, `duration_minutes`, `attendance_mode`, `venue` (nested reference with
`public_id`, `name`, `floor`, `capacity` and its `building`), `venue_display`,
`off_site_venue`, `off_site_address`, `meeting_link`, `host_group` (nested group
reference), `chaired_by` / `secretary` (nested user references), `quorum`,
`state` (nested reference with `public_id`, `name`, `slug`, `is_terminal`),
`calendar_state` (`scheduled`, `running`, `past`, `closed` or `cancelled`),
`is_upcoming`, `is_finished`, `minutes_status`, `priority`, `deadline`,
`attendance_counts`, `member_attendance` (only when `user=` was supplied),
`created_at` and `updated_at`.

`attendance_counts` is the register tally: `invited`, `present`, `absent`,
`excused`, `late`, `not_recorded`, `accepted`, `declined`, `required`,
`host_group`, `invitee` and `quorum_met`.

`member_attendance` is the requesting member's own row: `public_id`, `user`
(nested user reference, `null` for a guest), `guest_name`, `display_name`,
`email`, `source` (`host_group` or `invitee`), `capacity`, `is_required`,
`rsvp_status`, `attendance_status`, `is_present`, `invited_at`, `responded_at`,
`recorded_at` and `note`.

The **detail** endpoint returns the same shape plus `attendances` — the full
register, one object per person with the same fields as `member_attendance`. It
also accepts `?user=` to populate `member_attendance`:

```http
GET /api/v1/meetings/{public_id}/?user=jsmith
```

Neither the agenda body nor attendees' recorded IP addresses are exposed.

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

---

## 6. Bill update webhook (testing receiver)

Every Bill create, edit and status change is posted — in the background, after
the change is saved — to `BILL_UPDATE_ENDPOINT_URL`. Until the partner system
exists that setting is blank, and PWMS posts to its own receiver:

```
POST /api/v1/bill-updates/
Authorization: Bearer <BILL_UPDATE_WEBHOOK_TOKEN>
Content-Type: application/json
```

The receiver is **closed** while `BILL_UPDATE_WEBHOOK_TOKEN` is blank: every post
is rejected with `401`. Set the same value in the worker's environment so it can
authenticate.

Body (the sender builds exactly this):

```json
{
  "update_id": "4c1f0e3a-9f6e-4a1e-8f0c-2d5b7a9e1c33",
  "event": "STATUS_CHANGE",
  "occurred_at": "2026-11-20T12:30:00+02:00",
  "bill": {
    "public_id": "01a1f8ee-5c5b-770f-8d43-8e132515cde3",
    "bill_number": "B 7-2026",
    "title": "Webhook bill",
    "short_title": "Webhook",
    "description": "",
    "bill_type": "section-75",
    "house_of_origin": "na",
    "sponsor": null,
    "sponsor_name": "",
    "introduced_date": null,
    "responsible_committee": null,
    "current_state": "Introduced in the House",
    "public_status": "Introduced",
    "owner": {"public_id": "…", "username": "jdoe", "display_name": "J Doe"},
    "assigned_to": null,
    "deadline": null,
    "priority": "medium",
    "is_overdue": false,
    "identifier": "B 7-2026",
    "url": "http://localhost:8000/pwms/workflows/bills/01a1f8ee-…/"
  },
  "actor": {"public_id": "…", "username": "jdoe", "display_name": "J Doe"},
  "status_change": {
    "from_state": "Introduced in the House",
    "to_state": "Referred to committee",
    "transition": "Refer",
    "comment": "Please review.",
    "ip_address": "10.0.0.9"
  }
}
```

`event` is `NEW`, `EDITS` or `STATUS_CHANGE`; `status_change` is present only
for the last. `occurred_at`, `actor` and the nested references are optional on
receive, and unknown keys are preserved in the log, so a future sender can add
detail without breaking the receiver.

The receiver **never mutates the Bill**. It checks that `bill.public_id` still
exists (`404` with a rejected log row otherwise), stores the exact body as an
inbound `BillUpdateLog` row and returns:

```json
{
  "update_id": "4c1f0e3a-9f6e-4a1e-8f0c-2d5b7a9e1c33",
  "event": "STATUS_CHANGE",
  "received_at": "2026-11-20T12:30:01+02:00",
  "duplicate": false,
  "public_status": "Under Parliamentary Consideration"
}
```

`public_status` echoes the citizen-facing status the sender reported, so the
caller's own log records what was acknowledged.

`update_id` is the idempotency key: posting the same update again returns the
original receipt with `"duplicate": true` and writes nothing.

### Rejection callback (bi-directional)

When a reviewer rejects an update from the tracker, the receiving side posts the
decision back to the sender's API:

```
POST /api/v1/bill-updates/rejections/
Authorization: Bearer <BILL_UPDATE_WEBHOOK_TOKEN>
Content-Type: application/json
```

```json
{
  "rejection_id": "9b7c1a52-0f8e-4a1e-9f7d-3c2b1a0e9d88",
  "update_id": "4c1f0e3a-9f6e-4a1e-8f0c-2d5b7a9e1c33",
  "event": "STATUS_CHANGE",
  "occurred_at": "2026-11-20T12:41:00+02:00",
  "bill": {"public_id": "01a1f8ee-…", "bill_number": "B 7-2026", "title": "Webhook bill"},
  "reason": "The committee stage is missing.",
  "rejected_at": "2026-11-20T12:41:00+02:00",
  "rejected_by": {"username": "jdoe", "display_name": "J Doe"}
}
```

`rejection_id` is the rejection message's own idempotency key; `update_id`
identifies the update being rejected. The endpoint logs an inbound `REJECTION`
`BillUpdateLog` row (even when the original update is no longer present) and
stamps the sender's original outbound row with `rejection_reason`,
`rejected_at` and `rejected_by_name`, so its log answers *was this accepted?*
without joining the two sides. It returns:

```json
{
  "rejection_id": "9b7c1a52-0f8e-4a1e-9f7d-3c2b1a0e9d88",
  "update_id": "4c1f0e3a-9f6e-4a1e-8f0c-2d5b7a9e1c33",
  "received_at": "2026-11-20T12:41:01+02:00",
  "duplicate": false
}
```

The callback is queued (`pwms.tasks.post_bill_update`) and posted to
`BILL_UPDATE_REJECTION_ENDPOINT_URL` (blank derives this project's route), using
the same shared token and retry policy as outbound updates. In a split
deployment only that URL and token change.

Both halves of the exchange are logged in `BillUpdateLog`: the outbound attempt
(target URL, HTTP status, response excerpt, attempts and error) and the inbound
receipt or rejection. The table is read-only in the admin (**PWMS → Bill update
logs**). Swapping `BILL_UPDATE_ENDPOINT_URL` for the real API changes the target,
not the log.

To watch the exchange end to end, open
`/pwms/workflows/bills/tracker/` (also linked from the Bills list as **Live
tracker**). It shows every bill with a horizontal stepper of its **public
status** — the citizen-facing status its internal state publishes (BRS §12), so
the four committee stages collapse onto *Under Parliamentary Consideration* —
marking the statuses the bill has visited, the one it is in now, and those still
ahead, with the internal stage name beside the badge. Below it is a feed of the
most recent inbound posts and the last `NEW` / `EDITS` / `STATUS_CHANGE` received
for each bill. It polls the same view every three seconds, so a bill created in
PWMS appears once its `NEW` post is received and a status change advances the
stepper; `EDITS` only refreshes the details. The page adds no bill state of its
own: it renders the `Bill` table the API publishes and the inbound
`BillUpdateLog` rows, and approving only marks the log row (the source bill is
never changed).

Inbound posts wait for **review** before they reach the row: a bill with
unapproved changes shows a **Review** button, which opens a popup listing the
incoming post's field changes against the last accepted snapshot (or the whole
record, when nothing has been accepted yet). **Approve and apply** marks the
`BillUpdateLog` row `approved`, records the reviewer and time, and adopts that
snapshot on the tracker — so a `STATUS_CHANGE` moves the stepper and an `EDITS`
updates the displayed fields only after approval. `NEW` records follow the same
step. The source `Bill` is never changed by the receiver or the approval; the
tracker is the receiving side's accepted copy. Approvals are visible in the
admin log via `reviewed_by` / `reviewed_at`.
