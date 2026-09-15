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
| `django-background-tasks` | async background task queue (deadline/notification jobs) |
| `django-mptt` | hierarchical `Group` trees (Houses → Committees → …) |
| `django-bootstrap5`, `lucide` | server-rendered UI components |
| `django-htmx` | progressive enhancement / partial-page updates |
| `django-flatpickr` | date-time pickers in templates/forms |
| `djangorestframework` (DRF) | REST API layer |
| `drf-spectacular` | OpenAPI 3 schema + Swagger UI + ReDoc |
| `django-ninja` | **evaluation spike only** — prototype endpoints under `/ninja/` (see API Reference / System Design) |
| `django-filter` | (declared) query filtering for DRF-style views |
| `django-extensions` | (declared; not currently enabled) dev tooling |

## Documents, data & integrations (declared / emerging)

| Package | Purpose |
| --- | --- |
| `reportlab`, `weasyprint`, `openpyxl`, `markdown` | document / export generation (planned features) |
| `python-multipart` | form/file uploads |
| `cryptography` | field-level encryption (ID numbers stored as encrypted + HMAC) |
| `oracledb` | legacy Oracle source for data synchronisation commands |
| `pillow` | image handling (user avatars) |
| `djlint` | template linting / formatting |

## Configuration highlights (`src/pwms/settings.py`)

- `INSTALLED_APPS`: core Django + `auditlog`, `background_task`, `rest_framework`,
  `drf_spectacular`, `django_flatpickr`, `lucide`, `mptt`, `django_htmx`,
  `django_bootstrap5`, `pwms`.
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
- `REST_FRAMEWORK`: Session + Basic authentication, `IsAuthenticated` default,
  `drf_spectacular.openapi.AutoSchema`.
- `SPECTACULAR_SETTINGS`: OpenAPI title/version for the generated schema.
- `auditlog.middleware.AuditlogMiddleware` runs after auth so changes are attributed to the logged-in user.
- Logging: console/file handler writing to the configured `LOG_DIR` (`pwms.log`).
- Environment variables (`python-decouple`): `SECRET_KEY`, `DEBUG`, `PG_DB`,
  `PG_USERNAME`, `PG_PASSWORD`, `PG_HOST`, `PG_PORT`, `IDNO_HMAC_KEY`,
  `IDNO_ENC_KEY` (both required - they key the at-rest identity numbers, see
  `User.set_idno`), and the optional LDAP set (`AUTH_LDAP_SERVER_URI`,
  `AUTH_LDAP_BIND_DN`, `AUTH_LDAP_BIND_PASSWORD`, `AUTH_LDAP_BASE_DN`,
  `AUTH_LDAP_START_TLS`).

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

- Test modules: `pwms/tests.py` (auditing), `pwms/tests_api.py` (DRF API),
  `pwms/tests_ninja.py` (ninja spike).
- Run from the repo root; `pwms.*` test modules resolve through the editable
  install (single import identity):

```bash
.venv/bin/python manage.py test pwms.tests pwms.tests_api pwms.tests_ninja
```

- Tests run on a throwaway test database (PostgreSQL test DB).

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
