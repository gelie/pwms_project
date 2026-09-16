# API Reference

PWMS currently exposes a small **audit-history** API. Two stacks coexist while
the team evaluates django-ninja:

| Stack | Base path | Status |
| --- | --- | --- |
| Django REST Framework (DRF) | `/api/` | current |
| django-ninja | `/ninja/` | **evaluation spike** — see [System Design §6](./System%20Design.md#6-api-layer) |

OpenAPI schema and interactive docs are generated from the **DRF** stack via
`drf-spectacular`.

---

## 1. Authentication

- **Session** (default) — log in via the browsable API
  `POST /api/auth/login/` (or the web login) so the session cookie is used.
- **Basic** — `Authorization: Basic base64(user:pass)`.
- Anonymous requests to protected endpoints:
  - DRF → `403 Forbidden`
  - django-ninja → `401 Unauthorized`

---

## 2. Endpoints

### DRF (`/api/`)

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/` | ✓ | API root: lists endpoints + login/logout + documentation links |
| GET | `/api/resolutions/{public_id}/audit/` | ✓ | auditlog CRUD trail for one `InternationalResolution` |
| GET | `/api/agreements/{public_id}/audit/` | ✓ | auditlog CRUD trail for one `InternationalAgreement` |
| GET | `/api/bills/{public_id}/audit/` | ✓ | auditlog CRUD trail for one `Bill` |
| – | `/api/auth/login/` `/api/auth/logout/` | – | browsable-API auth |

### OpenAPI / docs (public)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/schema/` | OpenAPI 3 schema (YAML; add `?format=json` for JSON) |
| GET | `/api/docs/` | Swagger UI (interactive, “Try it out”) |
| GET | `/api/redoc/` | ReDoc |

### django-ninja spike (`/ninja/`, session auth)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/ninja/` | root |
| GET | `/ninja/resolutions/{public_id}/audit/` | same audit trail, typed via Pydantic |
| GET | `/ninja/openapi.json`, `/ninja/docs` | ninja auto schema / Swagger |

### HTMX search fragments (HTML, not JSON)

The form search pickers call plain Django views that return HTML rows, not API
responses, so they are deliberately absent from the schema above:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/pwms/user-search/` | users matching `?search=` |
| GET | `/pwms/group-search/` | groups matching `?search=` |
| GET | `/pwms/country-search/` | countries matching `?search=` (name or ISO code) |
| GET | `/pwms/city-search/` | cities matching `?search=`, scoped by `?country=<pk>` |

They are session-authenticated like every other site route. See
[Search Lookups](./Search%20Lookups.md) for the contract they follow.

The app's other dynamic pages — the dashboard, the workflow detail pages with
their tabs, the SharePoint attachment picker, the alerts page, the report builder
/ exports, the per-instrument documents and the state-machine diagrams — are
likewise server-rendered HTML (or images) under `/pwms/`, not JSON API endpoints
(see [Functional Design](./Functional%20Design.md) and
[System Design → URL map](./System%20Design.md#2-url-map)).

---

## 3. Response shape — audit history

`GET /api/resolutions/{public_id}/audit/` (and the equivalents
`GET /api/agreements/{public_id}/audit/` and
`GET /api/bills/{public_id}/audit/`) returns a JSON array of
`auditlog.LogEntry` entries (oldest→newest order is newest-first by default):

```json
[
  {
    "id": 1,
    "action": 0,
    "action_display": "create",
    "actor_email": "alice@example.com",
    "timestamp": "2026-09-09T14:44:33.706015Z",
    "changes": {
      "title": ["None", "A test resolution"],
      "current_state": ["None", "1"],
      "workflow_type": ["None", "1"],
      "resolution_number": ["None", "IR-1"]
    },
    "remote_addr": null
  }
]
```

| Field | Meaning |
| --- | --- |
| `action` / `action_display` | `0/1/2` = create / update / delete (auditlog enums) |
| `actor_email` | who made the change (`null` for anonymous/system) |
| `changes` | dict of `field: [before, after]`; M2M entries have `{"type": "m2m", "objects": […], "operation": "add"}` |
| `timestamp` | when it happened |
| `remote_addr` | client IP captured by the auditlog middleware |

Missing `public_id` → `404`. Anonymous → `403` (DRF) / `401` (ninja).

### Examples

```bash
# API root
curl -u alice:pw http://127.0.0.1:8000/api/

# Audit history for a resolution (session auth via login first)
curl -b cookies.txt -c cookies.txt \
  -X POST -d "username=alice&password=pw" \
  http://127.0.0.1:8000/api/auth/login/
curl -b cookies.txt http://127.0.0.1:8000/api/resolutions/<public_id>/audit/
```

---

## 4. Serialization

- `AuditLogEntrySerializer` (DRF, `pwms/api/serializers.py`) renders
  `auditlog.LogEntry` rows: `id, action, action_display, actor_email, timestamp,
  changes, remote_addr`.
- `AuditEntrySchema` (ninja, `pwms/api/ninja.py`) is the Pydantic equivalent.
- Both read data through `pwms/utils/audit_helpers.py`
  (`get_audit_trail_by_public_id`), which filters `LogEntry` by
  ContentType + internal PK.

---

## 5. Extending

To expose a new concrete workflow model (the four shipped instruments all have
one; this is the pattern for a future `Motion`):

```python
# pwms/api/views.py
class MotionAuditHistoryView(WorkflowAuditHistoryView):
    model_class = Motion


# pwms/api/urls.py — add a route, e.g.
# path("motions/<uuid:public_id>/audit/", views.MotionAuditHistoryView.as_view(), name="motion-audit-history"),
```

Remember to register the model with `auditlog` in `PwmsConfig.ready()` and add it
to the API root’s `endpoints` listing.
