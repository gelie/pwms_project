# Engineering backlog

Current-state backlog for PWMS. This reconciles an earlier assessment against the
repository as it actually stands (2026-10-12) and folds in the DRF API work.

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
- [x] **REST API fleshed out.** Token authentication (`rest_framework.authtoken`:
      obtain / refresh / revoke under `/api/auth/token/`) and resource endpoints
      for users, groups, roles, memberships, the workflow registry
      (types / states / transitions) and every workflow instance, plus per-instance
      `transitions` / `transition` / `events` / `audit` actions. RBAC reuses
      `pwms.services.permissions`; see `docs/API Reference.md`.
      **Note:** this added the `authtoken` tables, so a one-off
      `python manage.py migrate` is required on deploy (migration count now 48).
- [x] **django-ninja spike removed.** The `/ninja/` prototype, its test module,
      its URL, its middleware exemption and the `django-ninja` dependency
      (pyproject + lock) are gone; DRF is the single API stack.
- [x] **API tokens expire.** `pwms.api.authentication.ExpiringTokenAuthentication`
      enforces an absolute TTL (`TOKEN_TTL_DAYS`, default 30; `0` disables) and
      `manage.py purge_tokens` deletes the expired rows — closing the "a leaked
      token is a permanent credential" gap.
- [x] **Credential endpoint rate-limited.** `POST /api/auth/token/` carries an
      IP-scoped anonymous throttle (`pwms.api.throttles.TokenEndpointThrottle`,
      `TOKEN_THROTTLE_RATE` default `20/min`) → `429` with `Retry-After`.

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
- [ ] **Throttling is partial and per-process** — only the token endpoint is
      throttled (see above); there is no `DEFAULT_THROTTLE_CLASSES`, so the rest
      of the API is unthrottled. The counters use Django's cache, so behind
      several gunicorn workers the `LocMemCache` counts per worker — point
      `CACHES` at Redis/Memcached for an exact limit, and consider a per-user
      throttle on the resource API.
- [ ] **Reconsider `BasicAuthentication`** — session + token only. Combined with no
      throttle it is credential-stuffing exposure, and the token endpoint now makes
      Basic largely redundant for clients.
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
- [ ] **Calendar view** — the legacy `update_event_statuses` calendar concept was
      dropped; meetings/sessions would be a new model.
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
2. Harden settings — env-driven `ALLOWED_HOSTS`, secure cookies/HSTS/nosniff, CSP,
   and a shared cache so the token-endpoint throttle counts across workers.
3. Supervise the task worker and alert on a stalled queue — otherwise whole
   shipped features are dead in production.
4. SharePoint version pinning/restore — the one domain feature with real
   legislative-accuracy value.
5. Split `views.py` / `models/workflows.py` and fix the 104 lint findings.

The standout observation holds: the documentation and domain modelling are well
ahead of the operational engineering. The API is now broad, which raises the value
of the hardening items above rather than lowering it.
