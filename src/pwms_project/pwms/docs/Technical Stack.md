# Technical Stack

## Runtime

| Concern | Choice | Version / note |
| --- | --- | --- |
| Language | Python | ≥ 3.14 |
| Web framework | Django | ≥ 6.1.1 |
| Database | PostgreSQL | accessed via `psycopg2-binary` |
| Settings | `python-decouple` | values read from a local `.env` |
| Package manager | uv | `uv.lock` pinned; `uv sync` to install |
| Project layout | `src` layout | Django project lives in `src/pwms_project/` |

## Django apps / libraries

| Package | Purpose |
| --- | --- |
| `django.contrib.*` | admin, auth, sessions, messages, contenttypes, staticfiles |
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

## Configuration highlights (`config/settings.py`)

- `INSTALLED_APPS`: core Django + `auditlog`, `background_task`, `rest_framework`,
  `drf_spectacular`, `django_flatpickr`, `lucide`, `mptt`, `django_htmx`,
  `django_bootstrap5`, `pwms`.
- `AUTH_USER_MODEL = "pwms.User"`.
- `ROOT_URLCONF = "config.urls"` — see [System Design → URL map](#) and
  [API Reference](./API%20Reference.md).
- `REST_FRAMEWORK`: Session + Basic authentication, `IsAuthenticated` default,
  `drf_spectacular.openapi.AutoSchema`.
- `SPECTACULAR_SETTINGS`: OpenAPI title/version for the generated schema.
- `auditlog.middleware.AuditlogMiddleware` runs after auth so changes are attributed to the logged-in user.
- Logging: console/file handler writing to the configured `LOG_DIR` (`pwms.log`).
- Environment variables (`python-decouple`): `SECRET_KEY`, `DEBUG`, `PG_DB`,
  `PG_USERNAME`, `PG_PASSWORD`, `PG_HOST`, `PG_PORT`, `IDNO_HMAC_KEY`.

> The app is enabled via a single `AppConfig` (`pwms.apps.PwmsConfig`) whose
> `ready()` registers the concrete workflow model with `auditlog`. It is the only
> `AppConfig` in the app so Django selects it automatically.

## Environment / tooling notes

- **Run everything from `src/pwms_project/`** — that is the Django project root
  (contains `manage.py` and `config/`).
- Because the package is installed as `pwms-project` (editable) with a `src`
  layout, the same files are importable two ways (`pwms.*` and
  `pwms_project.pwms.*`). Use the `pwms.*` identity (matches the installed app
  label). This affects test discovery — see below.

## Testing

- Test modules: `pwms/tests.py` (auditing), `pwms/tests_api.py` (DRF API),
  `pwms/tests_ninja.py` (ninja spike).
- **Always pass the top-level directory** so discovery imports models as
  `pwms.*` (not `pwms_project.pwms.*`, which breaks model registration):

```bash
cd src/pwms_project
.venv/bin/python manage.py test pwms.tests pwms.tests_api pwms.tests_ninja \
  --top-level-directory="$PWD"
```

- Tests run on a throwaway test database (PostgreSQL test DB).

## Database reset (development)

The dev schema can be fully reset (destructive — all data is lost):

```bash
cd src/pwms_project
.venv/bin/python manage.py shell -c "from django.db import connection; c=connection.cursor(); c.execute('DROP SCHEMA IF EXISTS public CASCADE'); c.execute('CREATE SCHEMA public')"
.venv/bin/python manage.py migrate
```

If `pwms` has no migrations (the folder was emptied), run
`.venv/bin/python manage.py makemigrations pwms` **before** migrating —
otherwise Django cannot find the custom-User migration and `admin` migrations fail
with `relation "pwms_user" does not exist`.
