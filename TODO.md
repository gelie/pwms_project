# Engineering backlog

Current-state backlog for PWMS. This reconciles an earlier assessment against the
repository as it actually stands (2026-10-30) and folds in the external API work.

Verified facts used below: 48 migrations, 13 test modules, 656 tests, `ruff check`
reports 104 findings (27 auto-fixable), `views.py` 2 643 lines,
`models/workflows.py` 2 910, `forms.py` 1 068.

---

## Recently done

- [x] **CI exists.** `.github/workflows/ci.yml` runs three gates — `ruff check` +
      `ruff format --check`, `makemigrations --check --dry-run` then `migrate`,
      and the full suite under `coverage`. The earlier "no CI at all" note is
      obsolete.
- [x] **pre-commit + Makefile.** `.pre-commit-config.yaml` mirrors the lint job;
      the `Makefile` exposes `make lint / migrate-check / test / coverage / check`.
- [x] **Coverage is measured** in CI (`coverage run --source=pwms`), though not
      yet enforced (no `fail_under`).
- [x] **External Django-Ninja API added; DRF retired.** `pwms/api/`
      registers partner applications in the admin (name, description, contact,
      scopes, per-key rate limit), issues **15-minute access tokens** and
      **rotating 24-hour refresh tokens**, and exposes read-only **users**,
      **memberships** and **bills** under `/api/v1/`, each gated by a scope.
      The old `/api/` DRF viewsets, token endpoints, `authtoken` tables,
      `purge_tokens` command, DRF middleware exemption and drf-spectacular schema
      are removed; so are the `djangorestframework` / `drf-spectacular`
      dependencies. See `docs/API Reference.md`.
- [x] **API usage logging and per-key throttling.** Every `/api/v1/` request
      appends an `ApiUsageLog` row (client, method/path, status, duration, IP,
      user agent, throttle flag) and counts against the client's
      `rate_limit_per_minute`; over-limit requests get `429` with `Retry-After`.
      The token endpoints carry an additional IP-scoped throttle
      (`EXTERNAL_API_TOKEN_THROTTLE_RATE`, default `20/min`).
- [x] **Initial partner registrations seeded.** Parking System (`users:read`,
      `memberships:read`), Main Website (`bills:read`) and eNominations
      (`memberships:read`); secrets are issued by rotating them in the admin or
      with `manage.py rotate_api_secret <client_id>`.
- [x] **Redis-backed shared cache.** `CACHES` selects Django's Redis backend
      whenever `REDIS_URL` is set (default blank → `LocMemCache` for bare
      checkouts/tests). External API throttling now uses atomic fixed-window
      counters (`cache.add` + `cache.incr`) keyed by client id, so the limit is
      exact across gunicorn workers; both throttles fail open with a logged error
      if Redis is unreachable. CI runs a `redis:7` service.

---

## 1. Engineering infrastructure

- [ ] **Turn the reporting gates into hard gates.** `ruff check` and
      `makemigrations --check` currently run with `continue-on-error: true`
      because both flag *pre-existing* conditions. A gate that reports without
      failing trains people to ignore it — clear the underlying condition, then
      flip the flag to `false`.
- [ ] **Enforce coverage.** Add `fail_under` to `[tool.coverage.report]` once the
      number is trusted, so the RBAC and transition guards (the security-critical
      paths) cannot silently regress.
- [ ] **Dependency audit + lock verification.** `pip-audit` for known CVEs and a
      `uv lock --check` / `uv sync --frozen` gate. `uv.lock` exists but nothing in
      CI verifies it is in sync.
- [ ] **Clear the lint debt.** 104 findings: 31 blind `except`, 29 bare/vanilla
      `raise`, 23 unsorted imports. The blind `except Exception` blocks can swallow
      RBAC failures and are the ones worth fixing first; 27 findings are
      auto-fixable.

## 2. Production / security hardening

- [ ] **`ALLOWED_HOSTS` is hard-coded** to dev hosts in `settings.py`
      (`workflows-dev.parliament.gov.za`, `172.16.0.114`, …). `DEBUG`/`SECRET_KEY`
      are env-driven, so this is the odd one out and will bite on deployment.
- [ ] **No security headers** — nothing sets `SECURE_SSL_REDIRECT`,
      `SECURE_HSTS_SECONDS`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` or
      `SECURE_CONTENT_TYPE_NOSNIFF`. For a system holding encrypted MP ID numbers,
      this is a real gap.
- [ ] **No CSP** — the app is HTMX/Bootstrap-heavy; a `django-csp` policy would
      meaningfully reduce XSS blast radius.
- [ ] **Secret rotation cadence.** Client secrets are hashed and rotated manually.
      Consider a scheduled reminder / expiry policy for partner credentials.
- [ ] **No secret scanning** — `.env` is git-ignored, but there is no
      gitleaks/detect-secrets net given how many secrets live there (LDAP bind
      password, Oracle creds, Graph client secret, two crypto keys).

## 3. Unfinished domain features

- [ ] **SharePoint version restore** — version list/download work; restoring a
      superseded version needs a write-scoped Graph permission not currently held.
      Users can see history but cannot roll back.
- [ ] **Version pinning** — pin "the version tabled" so it survives later
      revisions. For a legislative record this is accuracy, not polish.
- [ ] **HTML alert email templates** — alert mail is plain text only; report-share
      mail already does HTML + text. Low effort, visible.
- [ ] **Markdown → HTML rendering for note fields** — `markdown` is a declared
      dependency but unused.
- [x] **Calendar view / meetings (Events workflow)** — shipped as a `Meeting`
      workflow instrument: a colour-coded calendar dashboard (month / week / day)
      at `/pwms/meetings/`, agenda / minutes as documents, an auditable attendance
      register, status and document alerts, CSV/XLSX import & export, and REST
      endpoints (list/detail, "my meetings by membership", attendance, minutes).
      The legacy `update_event_statuses` command (which imported the removed
      `workflows.models.Event`) is superseded by it and can be retired.
- [ ] **Motions and Questions instruments** — named as "planned", not started.
- [ ] **Web UI gaps** — Share / Comment / Manage are RBAC-gated but still
      admin-side (the API exposes view/edit/delete and the workflow actions, not
      share/comment/manage); create/edit forms lack the flatpickr date pickers;
      committee/House dashboards and a django-filter search layer are
      declared-but-unbuilt; the admin for managing workflow definitions lags.

## 4. Reliability of background work

- [ ] **Supervise the task worker.** Delivery depends on `django-background-tasks`
      + `manage.py process_tasks`, but `start_pwms.sh` starts only gunicorn — no
      task worker, no systemd timers. Queued email, scheduled reports and deadline
      alerts therefore do nothing unless the worker is separately supervised.
- [ ] **Queue health / dead-letter visibility.** Failures surface only in
      `Notification.error`; nothing alerts on a stalled queue, and there is no
      retry/dead-letter view.

## 5. Code health / maintainability

- [ ] **God modules** — `views.py` 2 643 lines, `models/workflows.py` 2 910,
      `forms.py` 1 068. The workflow base class in particular could split
      (transitions / RBAC / events / progress).
- [ ] **Migration safety** — 48 migrations with data backfills (e.g. `0044`). No
      migration tests or reversibility checks; the roadmap notes the schema "can be
      dropped and recreated safely in development", which suggests rollback is
      never exercised.
- [ ] **Test breadth** — the suite is targeted (heavy on reports/attachments/
      notifications); no performance/load tests, and the API permission matrix
      would benefit from more outright-negative cases.

## 6. Data & integration risks

- [ ] **SharePoint site-member sync** is silently skipped — the app token lacks
      `Sites.Manage.All`, so the designed PWMS-group → SharePoint-site permission
      mapping is not enforced in production and access can drift between systems.
- [ ] **Oracle/legacy sync** — `sync_users_oracle` / `sync_groups_oracle` /
      `sync_roles_oracle` create placeholder users, and membership collision
      handling is "not implemented yet" (`validate_memberships.py`). Identity
      reconciliation is a known soft spot.

---

## Next, in order

1. Flip the CI gates to hard (`ruff`, `makemigrations --check`) and add
   `fail_under` — nothing else matters as much.
2. Harden settings — env-driven `ALLOWED_HOSTS`, secure cookies/HSTS/nosniff and
   CSP. (The shared cache for throttle counters is now Redis-backed.)
3. Supervise the task worker and alert on a stalled queue — otherwise whole
   shipped features are dead in production.
4. SharePoint version pinning/restore — the one domain feature with real
   legislative-accuracy value.
5. Split `views.py` / `models/workflows.py` and fix the 104 lint findings.

The standout observation holds: the documentation and domain modelling are well
ahead of the operational engineering. The API is now broad, which raises the value
of the hardening items above rather than lowering it.
