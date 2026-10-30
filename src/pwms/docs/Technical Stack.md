# Technical Stack

## Runtime

| Concern | Choice | Version / note |
| --- | --- | --- |
| Language | Python | ≥ 3.14 |
| Web framework | Django | ≥ 6.1.1 |
| Database | PostgreSQL | accessed via `psycopg2-binary` |
| Settings | `python-decouple` | values read from a local `.env` |
| Package manager | uv | `uv.lock` pinned; `uv sync` to install |
| Project layout | `src` layout | single top-level package `pwms` in `src/pwms/` (incl. its `templates/` + `static/`); project assets (`manage.py`, `.env`, `logs/`) at the repo root |

## Django apps / libraries

| Package | Purpose |
| --- | --- |
| `django.contrib.*` | admin, auth, sessions, messages, contenttypes, staticfiles |
| `django-auth-ldap` (+ `python-ldap`) | Active Directory sign-in; `pwms.backends.GracefulLDAPBackend` maps an authenticated AD account onto its existing ERP user |
| `django-auditlog` | automatic CRUD audit trail (`LogEntry`); middleware captures the acting user |
| `django-background-tasks` | async background task queue; `pwms/tasks.py` registers alert-email delivery and the scheduled-report drain, served by `manage.py process_tasks` |
| `django-mptt` | hierarchical `Group` trees (Houses → Committees → …) |
| `lucide` | server-rendered UI icons; Bootstrap 5 itself is **vendored** under `static/` (`bootstrap.min.css` + `bootstrap.bundle.min.js`), not loaded via the `django-bootstrap5` template tags |
| `django-chartjs` | (declared/installed, but not used) charting; the detail page's progress ring is inline SVG + CSS, so no chart library is shipped to the browser yet |
| `django-htmx` | progressive enhancement / partial-page updates |
| `django-flatpickr` | date-time pickers in templates/forms |
| `django-ninja` | external (partner) REST API: typed endpoints, OpenAPI + Swagger docs |
| `redis` | Redis client for Django's cache backend; backs external-API throttling across gunicorn workers |
| `django-filter` | (declared) query filtering helper; the Ninja API filters with ORM queries directly |
| `django-extensions` | (declared; not currently enabled) dev tooling |

## Documents, data & integrations

| Package | Purpose |
| --- | --- |
| `weasyprint`, `openpyxl`, `reportlab` | reporting exports and per-instrument documents (pdf / xlsx; reportlab declared) |
| `markdown` | (declared) rendering note fields as HTML |
| `python-multipart` | multipart form/file uploads (SharePoint attachment upload) |
| `httpx` + `asyncio` | async calls to the Microsoft Graph API (`pwms/utils/sharepoint.py`) |
| `graphviz` | `generate_diagrams` renders workflow-type state machines |
| `cryptography` | field-level encryption (ID numbers stored as encrypted + HMAC) |
| `oracledb` | legacy Oracle source for data synchronisation commands |
| `pillow` | image handling (user avatars) |
| `bs4`, `selenium` | committee scraping (`scrape_parliament_committees`) |
| `djlint` | template linting / formatting |

## Configuration highlights (`src/pwms/settings.py`)

- `INSTALLED_APPS`: core Django + `auditlog`, `background_task`, `chartjs`,
  `django_flatpickr`, `lucide`, `mptt`, `django_htmx`, `pwms` and
  `pwms.api`.
- `AUTH_USER_MODEL = "pwms.User"`.
- `AUTHENTICATION_BACKENDS`: `pwms.backends.GracefulLDAPBackend` (Active
  Directory) then `pwms.backends.FallbackModelBackend` (local database). The
  directory is authoritative for credentials; the local backend covers
  identities that have no AD account and a break-glass superuser during an
  outage, and it is also what answers Django's `is_superuser`/staff permission
  checks. LDAP stays inert unless `AUTH_LDAP_SERVER_URI` and
  `AUTH_LDAP_BASE_DN` are set, so a checkout without a directory boots and signs
  in locally. Authorisation is *not* taken from AD: it remains
  `Role`/`GroupMembership` (see `pwms.models.permissions`).
- `ROOT_URLCONF = "pwms.root_urls"` — see [System Design → URL map](#) and
  [API Reference](./API%20Reference.md).
- `MIDDLEWARE`: `pwms.middleware.SiteLoginRequiredMiddleware` makes every site
  route login-required by default; public pages opt out with the
  `login_not_required` decorator (home, about, contact). The admin and the
  `api` namespace manage their own authentication.
  `pwms.api.middleware.ExternalApiUsageLogMiddleware` appends an
  `ApiUsageLog` row for every `/api/v1/` request.
- SharePoint Graph: `SHAREPOINT_CLIENT_ID` / `SHAREPOINT_CLIENT_SECRET` /
  `SHAREPOINT_TENANT_ID` (from `CLIENT_ID` / `CLIENT_SECRET` / `TENANT_ID`), with
  the token URL and `.default` scope derived — see
  [SharePoint Sync](./SharePoint%20Sync.md).
- Cache (`CACHES`): when `REDIS_URL` is set (e.g.
  `redis://127.0.0.1:6379/1`) the stock
  `django.core.cache.backends.redis.RedisCache` is used, with
  `CACHE_KEY_PREFIX` (default `pwms`), `CACHE_TIMEOUT` (300s) and
  `REDIS_SOCKET_TIMEOUT` / `REDIS_CONNECT_TIMEOUT` (2s) — this is what makes the
  external API's per-key throttle exact across workers. `REDIS_URL` blank falls
  back to `LocMemCache` for bare checkouts/tests. Token throttling also uses this
  cache. See [API Reference §5](./API%20Reference.md#5-throttling-and-usage-logs).
- External API (`pwms/api/`): `EXTERNAL_API_PREFIX` (`/api/v1/`),
  `EXTERNAL_API_ACCESS_TOKEN_TTL_MINUTES` (default 15) and
  `EXTERNAL_API_REFRESH_TOKEN_TTL_MINUTES` (default 1440, i.e. 24 hours) set
  token lifetimes;
  `EXTERNAL_API_SIGNING_KEY` defaults to `SECRET_KEY`;
  `EXTERNAL_API_TOKEN_THROTTLE_RATE` (default `20/min`, IP-scoped) protects the
  token endpoints, while each `ApiClient.rate_limit_per_minute` (new clients
  default to `EXTERNAL_API_DEFAULT_RATE_LIMIT_PER_MINUTE`, 60) is enforced per
  key across every resource. `EXTERNAL_API_MAX_PAGE_SIZE` caps list pages
  (default 100).
- OpenAPI title/version for the Ninja API live in
  `pwms/api/api.py`; docs are at `/api/v1/docs` and the schema at
  `/api/v1/openapi.json`.
- `auditlog.middleware.AuditlogMiddleware` runs after auth so changes are attributed to the logged-in user.
- Logging: console/file handler writing to the configured `LOG_DIR` (`pwms.log`).
- Alerting email (`src/pwms/notifications/`): `MAILERS["default"]` uses the
  console backend, so outbound alerts print to the console in development (swap
  in SMTP to deliver them); `DEFAULT_FROM_EMAIL` sets the sender and
  `PWMS_BASE_URL` makes the links in alert mail absolute. The
  `pwms.notifications.alerts` context processor feeds the navbar bell
  (`unread_alert_count` + the five newest `in_app` alerts).
- `MEETING_CALENDAR_WEEK_START`: the day the Meetings calendar's weeks begin on
  — a weekday name (`"monday"` … `"sunday"`) or an index (`0` = Monday … `6` =
  Sunday); anything unrecognised falls back to Monday. Readers can override it
  per session with the calendar's "Week starts" control (a `week_start` query
  parameter).
- Environment variables (`python-decouple`): `SECRET_KEY`, `DEBUG`, `PG_DB`,
  `PG_USERNAME`, `PG_PASSWORD`, `PG_HOST`, `PG_PORT`, `IDNO_HMAC_KEY`,
  `IDNO_ENC_KEY` (both required - they key the at-rest identity numbers, see
  `User.set_idno`), the optional LDAP set (`AUTH_LDAP_SERVER_URI`,
  `AUTH_LDAP_BIND_DN`, `AUTH_LDAP_BIND_PASSWORD`, `AUTH_LDAP_BASE_DN`,
  `AUTH_LDAP_START_TLS`), the optional alerting pair `DEFAULT_FROM_EMAIL`
  and `PWMS_BASE_URL`, and the external API settings
  (`EXTERNAL_API_*`) plus `REDIS_URL` / cache settings — see above.

> The app is enabled via a single `AppConfig` (`pwms.apps.PwmsConfig`) whose
> `ready()` registers the concrete workflow model with `auditlog`. It is the only
> `AppConfig` in the app so Django selects it automatically.

## Environment / tooling notes

- **Run everything from the repo root** — that is where `manage.py`, `.env` and
  `logs/` live. `templates/` and `static/` live inside the app package
  (`src/pwms/`) and are auto-discovered (`APP_DIRS = True`;
  `STATICFILES_DIRS` empty).
- The package installs (editable) as `pwms` with the module at `src/pwms/`, so
  there is exactly **one** import identity: `pwms.*`. (The previous
  `src/pwms_project` layout, which also made `pwms_project.pwms.*` importable,
  no longer exists — that dual-import hazard is gone.)
- `DJANGO_SETTINGS_MODULE=pwms.settings`, WSGI/ASGI = `pwms.wsgi.application`,
  root URLconf = `pwms.root_urls` (which includes the app URLs at `/pwms/`).

## Testing

- Test modules: `pwms/tests.py` (auditing, workflow CRUD, lookups),
  `pwms/tests_api.py` (app keys, tokens, scopes, throttling, usage logs),
  `pwms/tests_notifications.py` (alerts + email dispatch),
  `pwms/tests_attachments.py` (SharePoint attachments & versioning),
  `pwms/tests_reports.py` (report builder, exports, sharing, scheduling),
  `pwms/tests_detail.py` (the tabbed detail page, the merged timeline, progress,
  diagrams),
  `pwms/tests_authentication.py` (AD/LDAP sign-in + fallback) and
  `pwms/tests_executive.py` (executive-branch identity rules).
- Run from the repo root; `pwms.*` test modules resolve through the editable
  install (single import identity):

```bash
.venv/bin/python manage.py test pwms.tests pwms.tests_api pwms.tests_attachments \
  pwms.tests_authentication pwms.tests_detail pwms.tests_executive \
  pwms.tests_notifications pwms.tests_reports
```

- Tests run on a throwaway test database (PostgreSQL test DB). CI also starts a
  `redis:7` service and sets `REDIS_URL`, so throttling is exercised against the
  shared cache; locally, leave `REDIS_URL` blank to use the LocMem fallback (or
  run Redis and set it to exercise the same path).

## Database reset (development)

The dev schema can be fully reset (destructive — all data is lost). Run from the
repo root:

```bash
.venv/bin/python manage.py shell -c "from django.db import connection; c=connection.cursor(); c.execute('DROP SCHEMA IF EXISTS public CASCADE'); c.execute('CREATE SCHEMA public')"
.venv/bin/python manage.py migrate
```

If `pwms` has no migrations (the folder was emptied), run
`.venv/bin/python manage.py makemigrations pwms` **before** migrating —
otherwise Django cannot find the custom-User migration and `admin` migrations fail
with `relation "pwms_user" does not exist`.
