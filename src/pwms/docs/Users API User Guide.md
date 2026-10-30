# Users API — User Guide

**For:** approved PWMS partner applications (for example, Parking System)
**Scope:** read-only access to the parliamentary user register
**API version:** `/api/v1/`

> **Printable one-page version:** [Users API User Guide (PDF)](./Users%20API%20User%20Guide.pdf).
> This Markdown file is the longer editable version; the one-page handout is
> generated from the HTML source.

This guide shows how to obtain a token and retrieve users. You cannot create,
edit or deactivate users through this API.

---

## 1. What you need

Your PWMS administrator will give you:

- the PWMS base address, e.g. `https://workflows-dev.parliament.gov.za`
- a **client ID**
- a **client secret**
- the **`users:read`** scope

Keep the client secret private. If it is exposed, ask the administrator to
rotate it.

The interactive reference — including every field — is at
`/api/v1/docs` on the PWMS host.

---

## 2. Get an access token

```bash
curl -s -X POST "https://pwms.example.gov.za/api/v1/auth/token/" \
  -H "Content-Type: application/json" \
  -d '{"client_id": "YOUR_CLIENT_ID", "client_secret": "YOUR_SECRET"}'
```

```json
{
  "access_token": "eyJ…",
  "refresh_token": "8fK…",
  "token_type": "Bearer",
  "expires_in": 900,
  "refresh_expires_in": 86400,
  "scope": "users:read"
}
```

- **Access token** — valid for **15 minutes**. Send it with every request.
- **Refresh token** — valid for **24 hours**, single use. Exchange it for a new
  pair before it expires (or when a call returns `401`):

```bash
curl -s -X POST "https://pwms.example.gov.za/api/v1/auth/token/refresh/" \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "YOUR_REFRESH_TOKEN"}'
```

A refresh token that has expired or already been used cannot be replayed; obtain
a new token pair with your client ID and secret.

---

## 3. Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/users/` | List, search and filter users |
| `GET` | `/api/v1/users/{public_id}/` | One user by their `public_id` |
| `GET` | `/api/v1/search/` | Search users together with any other resources your key may read |
| `GET` | `/api/v1/me/` | Check your client ID and scopes (not a person) |

Every request needs:

```http
Authorization: Bearer <access_token>
```

---

## 4. List and filter users

```bash
curl -sG "https://pwms.example.gov.za/api/v1/users/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "limit=20" \
  --data-urlencode "offset=0"
```

| Filter | What it does | Examples |
| --- | --- | --- |
| `search` | Searches all text fields below (case-insensitive substring) | `search=Molefe` |
| `search_fields` | Limits `search` to chosen fields; comma-separated | `search_fields=first_name,last_name` |
| `is_active` | Active/inactive accounts | `is_active=true` |
| `is_mp` | Members of Parliament | `is_mp=true` |
| `is_staff_member` | Staff members | `is_staff_member=true` |
| `department` | Department/group; `public_id`, name, short name or slug | `Portfolio Committee on Health` |
| `ordering` | Sort order; prefix with `-` to reverse | `last_name`, `-date_joined`, `username` |
| `limit` | Page size (default 20, maximum 100) | `limit=50` |
| `offset` | Number of records to skip | `offset=50` |

**Searchable fields for `search_fields`:**
`username`, `first_name`, `middle_name`, `last_name`, `email`, `positiondesc`,
`department`, `constituency`, `party_affiliation`, `phone`.

### Example: active MPs

```bash
curl -sG "https://pwms.example.gov.za/api/v1/users/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "is_active=true" \
  --data-urlencode "is_mp=true" \
  --data-urlencode "ordering=last_name"
```

### Example: search by name

```bash
curl -sG "https://pwms.example.gov.za/api/v1/users/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "search=Alice Molefe" \
  --data-urlencode "search_fields=first_name,last_name"
```

### Example: users in a department

```bash
curl -sG "https://pwms.example.gov.za/api/v1/users/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "department=Portfolio Committee on Health"
```

> Use `curl -G --data-urlencode` (or your HTTP library's query-parameter
> support) for names containing spaces or punctuation.

---

## 5. Read one user

```bash
curl -s "https://pwms.example.gov.za/api/v1/users/0192f3c0-7a1e-7c2b-9d4e-1a2b3c4d5e6f/" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Use the user's `public_id` (a UUID), not an internal database id or username.

---

## 6. Pages and fields

Every list response is a page:

```json
{
  "count": 42,
  "limit": 20,
  "offset": 0,
  "next": "https://…/api/v1/users/?limit=20&offset=20",
  "previous": null,
  "results": [ { … }, { … } ]
}
```

`count` is the total number of matching users. Follow `next` until it is
`null` to read the whole result set; `previous` goes back one page. The links
preserve your search and filters.

A user in `results` includes:

`public_id`, `username`, `title`, `first_name`, `middle_name`, `last_name`,
`display_name`, `email`, `employee_type`, `positiondesc`, `department`,
`supervisor`, `is_active`, `is_mp`, `is_staff_member`, `constituency`,
`party_affiliation` and `phone`.

- `department` is a nested object with `public_id`, `name`, `short_name` and
  `group_type`.
- `supervisor` is a nested object with `public_id`, `username` and
  `display_name`; it is `null` when no manager is recorded. The supervisor's own
  manager is not expanded.
- Identity numbers, date of birth, gender, bio/avatar and the list of direct
  reports are deliberately not exposed.

### Combined search

If you also have access to other registers, search users and the other resources
in one call:

```bash
curl -sG "https://pwms.example.gov.za/api/v1/search/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "q=Molefe" \
  --data-urlencode "resources=users" \
  --data-urlencode "limit=10" \
  --data-urlencode "offset=0"
```

The `users` group in the response is a page with the same `count`, `limit`,
`offset`, `next`, `previous` and `results` shape.

---

## 7. Common responses

| Status | Meaning | What to do |
| --- | --- | --- |
| `200` | Success | — |
| `400` | Invalid parameter (e.g. unknown `search_fields`) | Correct the request |
| `401` | Missing, expired or invalid access token | Refresh, or obtain a new token pair |
| `403` | Your key does not have `users:read` | Contact the PWMS administrator |
| `404` | No user with that `public_id` | Check the id |
| `429` | Rate limit exceeded | Wait for the `Retry-After` time, then retry |

Each key has a requests-per-minute limit (60 by default; your registration may
differ). The token endpoint is separately limited to 20 requests per minute per
IP address.

---

## 8. Practical tips

- Refresh the access token before it expires (for example after 12 minutes)
  rather than waiting for a `401`.
- Send booleans as `true` / `false` (e.g. `is_mp=true`, `is_active=false`).
- Use the `public_id` from the response, not an internal ID or a username, as
  the stable identifier.
- Cache a response only while it is still useful; user records change over time.
- Use `limit` values you actually need — 20–50 keeps responses fast.
- Contact the PWMS administrator if a person appears in PWMS but not through the
  API, or if you need an additional scope.

**Support:** PWMS administrator — see the contact details supplied with your
client credentials.
