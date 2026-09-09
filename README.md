# PWMS — Parliamentary Workflow Management System

PWMS is a Django-based parliamentary workflow management system that models how
legislative instruments (international resolutions today; Bills, Motions,
Questions planned) move through Parliament — Drafting → Submission → Committee →
Gazetting, etc. — while enforcing **who** may act at each step.

Every workflow instance shares one schema, one reusable *state machine*, and a
ContentType-linked RBAC layer, and everything that happens to an instrument is
**fully audited** (CRUD history + state-transition events).

> **Status:** active early development (`0.1.0`). The workflow/RBAC/audit core,
> DRF audit API, OpenAPI docs, and a django-ninja evaluation spike are in place.
> Email, notifications and SharePoint integration are planned — see
> [Roadmap & Planned Integrations](src/pwms/docs/Roadmap%20&%20Planned%20Integrations.md).

---

## At a glance

- **Custom User model** (`pwms.User`) with MP / staff profiles, encrypted ID numbers, group membership.
- **Hierarchical Groups** (`pwms.Group`, MPTT) representing Houses, Committees, Parties, Departments and administrative units.
- **Roles** (`pwms.Role`) with workflow capability flags; **GroupMembership** links users ↔ groups ↔ roles over time.
- **Reusable workflow engine** — `WorkflowType` owns `State`s and `Transition`s; concrete workflow models extend one abstract base and only store `workflow_type` + `current_state`.
- **ContentType-based RBAC** attached per workflow *instance*:
  - `WorkflowGroupAccess` — which House/Committee may access the instance (plus group-level defaults)
  - `WorkflowRolePermission` — instance-level overrides per role
  - `WorkflowStatePermission` — what may be done *in each state*
- **Audit everywhere**
  - `auditlog` records every create / update / delete of a workflow instance (with actor + field diffs).
  - `TransitionLog` records each semantic state transition (who, from → to, comment, IP).
- **REST API** (DRF) for audit history + **OpenAPI / Swagger / ReDoc** docs.
- **Admin, background jobs, management commands** for sync (Oracle/legacy), scraping, notifications and diagram generation.

---

## Project layout

```
pwms_project/                     # repo root — run manage.py from here
├── pyproject.toml / uv.lock      # uv-managed dependencies (distribution: pwms)
├── manage.py                     # Django entry point
├── .env                          # python-decouple config (secrets)
├── logs/                         # runtime logs
├── docs/                         # project-level docs
└── src/pwms/                     # single top-level package `pwms` (project + app)
    ├── settings.py               # Django settings (pwms.settings)
    ├── root_urls.py              # root URLconf (pwms.root_urls, includes /pwms/)
    ├── asgi.py  wsgi.py
    ├── templates/                # server-rendered UI (Bootstrap 5 + HTMX) — auto-discovered
    ├── static/                   # CSS / JS / images — auto-discovered
    ├── models/                   # User, Group, Role, workflows, RBAC, SharePoint
    ├── api/                      # DRF + django-ninja endpoints
    ├── membership/               # sync services (e.g. MembershipSyncService)
    ├── management/commands/      # sync, notifications, diagrams, ...
    ├── utils/                    # audit helpers, sharepoint client, etc.
    ├── tests*.py                 # unit/integration tests
    └── docs/                     # technical documentation
```

Full annotated tree and responsibilities live in
[System Design.md](src/pwms/docs/System%20Design.md).

---

## Documentation

The self-documentation lives in [`pwms/docs/`](src/pwms/docs/):

| Document | Contents |
| --- | --- |
| [Docs index](src/pwms/docs/README.md) | How the documentation is organised |
| [System Design](src/pwms/docs/System%20Design.md) | Architecture, layers, workflows & RBAC, module map, sequence diagram |
| [Data Model](src/pwms/docs/Data%20Model.md) | Every model, its fields, relations & ERD |
| [Functional Design](src/pwms/docs/Functional%20Design.md) | What the system does, feature-by-feature |
| [Technical Stack](src/pwms/docs/Technical%20Stack.md) | Frameworks, libraries, config, environment |
| [API Reference](src/pwms/docs/API%20Reference.md) | Endpoints, auth, schema/docs URLs |
| [Management Commands](src/pwms/docs/Management%20Commands.md) | Every `manage.py` command and its purpose |
| [Roadmap & Planned Integrations](src/pwms/docs/Roadmap%20&%20Planned%20Integrations.md) | Email, notifications, SharePoint, exports |

---

## Quickstart

Requirements: Python ≥ 3.14, [uv](https://docs.astral.sh/uv/), PostgreSQL.

```bash
# 1. Install dependencies (editable install adds src/ to the Python path)
uv sync

# 2. Configure environment — python-decouple reads .env from the repo root
cat > .env <<'EOF'
SECRET_KEY=change-me
DEBUG=True
PG_DB=PWMS
PG_USERNAME=gelie
PG_PASSWORD=
PG_HOST=localhost
PG_PORT=5432
IDNO_HMAC_KEY=change-me-too
EOF

# 3. Create the schema
python manage.py migrate

# 4. First admin user
python manage.py createsuperuser

# 5. Run (manage.py and everything else live at the repo root)
python manage.py runserver
```

Then open:

- Web app → <http://127.0.0.1:8000/pwms/>
- Admin → <http://127.0.0.1:8000/admin/>
- Swagger UI → <http://127.0.0.1:8000/api/docs/>

> **Database reset:** the schema can be dropped and recreated safely in
> development, see [System Design](src/pwms/docs/System%20Design.md#database-reset).

---

## Running tests

```bash
.venv/bin/python manage.py test pwms.tests pwms.tests_api pwms.tests_ninja
```

Run from the repo root; `pwms.*` test modules are importable via the editable
install. See
[Technical Stack](src/pwms/docs/Technical%20Stack.md#testing).

---

## Roadmap

Email, notifications and SharePoint integration are next on the roadmap. See
[Roadmap & Planned Integrations](src/pwms/docs/Roadmap%20&%20Planned%20Integrations.md)
for detail.

---

## License & Authors

- Author: **Gavin Elie**
- Project: `pwms-project` (uv-managed, `src` layout)

_Internal parliamentary system — documentation is intentionally developer-facing._
