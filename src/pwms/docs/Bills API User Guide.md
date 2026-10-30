# Bills API — User Guide

**For:** approved PWMS partner applications (for example, the Main Website)
**Scope:** read-only access to the parliamentary bills register
**API version:** `/api/v1/`

> **Printable one-page version:** [Bills API User Guide (PDF)](./Bills%20API%20User%20Guide.pdf).
> This Markdown file is the longer editable version; the one-page handout is
> generated from the HTML source.

This guide shows how to obtain a token and retrieve bills. You cannot create,
edit or delete bills through this API.

---

## 1. What you need

Your PWMS administrator will give you:

- the PWMS base address, e.g. `https://workflows-dev.parliament.gov.za`
- a **client ID**
- a **client secret**
- the **`bills:read`** scope

Keep the client secret private. If it is exposed, ask the administrator to
rotate it.

The interactive reference — including every field — is at
`/api/v1/docs` on the PWMS host.

---

## 2. Get an access token

```bash
curl -s -X POST "https://workflows-dev.parliament.gov.za/api/v1/auth/token/" \
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
  "scope": "bills:read"
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
| `GET` | `/api/v1/bills/` | List and search bills |
| `GET` | `/api/v1/bills/{public_id}/` | One bill by its `public_id` |
| `GET` | `/api/v1/search/` | Search bills together with any other resources your key may read |
| `GET` | `/api/v1/me/` | Check your client ID and scopes |

Every request needs:

```http
Authorization: Bearer <access_token>
```

---

## 4. List and filter bills

```bash
curl -sG "https://pwms.example.gov.za/api/v1/bills/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "limit=20" \
  --data-urlencode "offset=0"
```

| Filter | What it does | Examples |
| --- | --- | --- |
| `search` | Searches all text fields below (case-insensitive substring) | `search=appropriation` |
| `search_fields` | Limits `search` to chosen fields; comma-separated | `search_fields=title,short_title` |
| `bill_type` | Bill classification | `section-74`, `section-75`, `section-76`, `section-77` |
| `house_of_origin` | House of introduction | `na`, `ncop` |
| `public_status` | Citizen-facing status; slug or exact name | `introduced`, `Under Parliamentary Consideration` |
| `responsible_committee` | Committee; `public_id`, name, short name or slug | `Portfolio Committee on Health` |
| `ordering` | Sort order; prefix with `-` to reverse | `-introduced_date`, `bill_number`, `title` |
| `limit` | Page size (default 20, maximum 100) | `limit=50` |
| `offset` | Number of records to skip | `offset=50` |

**Searchable fields for `search_fields`:**
`bill_number`, `title`, `short_title`, `description`, `sponsor_name`,
`responsible_committee`, `atc_reference`, `order_paper_reference`,
`public_status`.

### Example: bills of a type and status

```bash
curl -sG "https://pwms.example.gov.za/api/v1/bills/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "bill_type=section-76" \
  --data-urlencode "public_status=Under Parliamentary Consideration" \
  --data-urlencode "ordering=-introduced_date"
```

### Example: bills for a committee

```bash
curl -sG "https://pwms.example.gov.za/api/v1/bills/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "responsible_committee=Portfolio Committee on Health"
```

> Use `curl -G --data-urlencode` (or your HTTP library's query-parameter
> support) for names containing spaces or punctuation.

---

## 5. Read one bill

```bash
curl -s "https://pwms.example.gov.za/api/v1/bills/0192f3c0-7a1e-7c2b-9d4e-1a2b3c4d5e6f/" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Use the bill's `public_id` (a UUID), not an internal database id.

---

## 6. Pages and results

Every list response is a page:

```json
{
  "count": 42,
  "limit": 20,
  "offset": 0,
  "next": "https://…/api/v1/bills/?limit=20&offset=20",
  "previous": null,
  "results": [ { … }, { … } ]
}
```

`count` is the total number of matching bills. Follow `next` until it is `null`
to read the whole result set; `previous` goes back one page. The links preserve
your search and filters.

A bill in `results` includes:

`public_id`, `bill_number`, `title`, `short_title`, `description`, `bill_type`,
`house_of_origin`, `sponsor` / `sponsor_name`, `introduced_date`,
`responsible_committee`, `atc_reference`, `order_paper_reference`,
`bill_document_url`, `public_status`, `current_version`, `identifier`,
`is_overdue`, `deadline`, `priority`, `created_at` and `updated_at`.

`public_status` is the citizen-facing status. `current_version` is the label of
the current bill version. Dates are ISO 8601; fields that have no value are
`null` or an empty string.

### Combined search

If you also have access to other registers, search bills and the other
resources in one call:

```bash
curl -sG "https://pwms.example.gov.za/api/v1/search/" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "q=appropriation" \
  --data-urlencode "resources=bills" \
  --data-urlencode "limit=10" \
  --data-urlencode "offset=0"
```

The `bills` group in the response is a page with the same `count`, `limit`,
`offset`, `next`, `previous` and `results` shape.

---

## 7. Common responses

| Status | Meaning | What to do |
| --- | --- | --- |
| `200` | Success | — |
| `400` | Invalid parameter (e.g. unknown `search_fields`, `q` too short) | Correct the request |
| `401` | Missing, expired or invalid access token | Refresh, or obtain a new token pair |
| `403` | Your key does not have `bills:read` | Contact the PWMS administrator |
| `404` | No bill with that `public_id` | Check the id |
| `429` | Rate limit exceeded | Wait for the `Retry-After` time, then retry |

Each key has a requests-per-minute limit (60 by default; your registration may
differ). The token endpoint is separately limited to 20 requests per minute per
IP address.

---

## 8. Practical tips

- Refresh the access token before it expires (for example after 12 minutes)
  rather than waiting for a `401`.
- Cache a response only while it is still useful; bills change over time.
- Use `limit` values you actually need — 20–50 keeps responses fast.
- Send `public_id` values exactly as returned; they are case-sensitive.
- Contact the PWMS administrator if a bill appears in PWMS but not through the
  API, or if you need an additional scope.

**Support:** PWMS administrator — see the contact details supplied with your
client credentials.
