# Engineering backlog

Current-state backlog for PWMS. This reconciles an earlier assessment against the
repository as it actually stands (2026-11-17) and folds in the PWMS API work.

Verified facts used below: 66 migrations, 19 test modules, 1002 tests, `ruff check`
reports 97 findings (26 auto-fixable), `views.py` 4 819 lines,
`models/workflows.py` 3 029, `forms.py` 1 566.

---

## Recently done

- [x] **Bill update webhook + audit log.** Every Bill create, edit and status
      change records an outbound `BillUpdateLog` row and queues
      `pwms.tasks.post_bill_update`, which posts the snapshot to
      `BILL_UPDATE_ENDPOINT_URL` (default: the in-project testing receiver at
      `POST /api/v1/bill-updates/`, authenticated with
      `BILL_UPDATE_WEBHOOK_TOKEN`). Failures retry under `MAX_ATTEMPTS` and stay
      readable in the read-only admin log; the receiver is idempotent on
      `update_id`. Swap the URL for the partner API later — the log shape does
      not change. A demo page at `/pwms/workflows/bills/tracker/` lists every
      bill with a public-status stepper and a feed of inbound posts, polling
      every 3 seconds. Inbound `NEW` / `STATUS_CHANGE` / `EDITS` posts wait for
      review: the popup diffs them against the last accepted snapshot and
      **Approve and apply** adopts it (recording reviewer and time) without
      touching the source bill. **Reject** records the reason locally and posts
      a `REJECTION` message back to the sender's API (the same configurable URL,
      token and queued task), which stamps the original update with the decision
      — a bi-directional exchange that works unchanged when the two sides are
      separate deployments.
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
      scopes, per-key rate limit), issues **60-minute access tokens** and
      **rotating 24-hour refresh tokens**, and exposes read-only **users**,
      **groups**, **memberships** and **bills** under `/api/v1/`, each gated by
      a scope.
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
- [x] **Group endpoints added.** `GET /api/v1/groups/` and `/groups/{public_id}/`
      expose the group register, multi-field search/filters (`group_type`,
      `parent`, `is_active`, `ordering`) and nested memberships. Guarded by the
      new `groups:read` scope; granted per application in the admin.
- [x] **Redis-backed shared cache.** `CACHES` selects Django's Redis backend
      whenever `REDIS_URL` is set (default blank → `LocMemCache` for bare
      checkouts/tests). PWMS API throttling now uses atomic fixed-window
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
- [ ] **Clear the lint debt.** 97 findings: 31 blind `except`, 29 bare/vanilla
      `raise`, 23 unsorted imports. The blind `except Exception` blocks can swallow
      RBAC failures and are the ones worth fixing first; 26 findings are
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
- [x] **HTML alert email templates** — alert mail is multipart (HTML + text),
      matching report-share mail; the email audience is also limited to staff of
      the affected groups.
- [x] **Durable outbound-email log** — `Notification` is the single log for
      alerts and explicit report/record shares, one row per recipient with a
      `to_email`/`from_email` snapshot, body, status, attempts and error. The
      admin is read-only and undeletable for both `Notification` and
      `ReportShare`.
- [ ] **Markdown → HTML rendering for note fields** — `markdown` is a declared
      dependency but unused.
- [x] **Calendar view / meetings (Events workflow)** — shipped as a `Meeting`
      workflow instrument: a colour-coded calendar dashboard (month / week / day)
      at `/pwms/meetings/`, agenda / minutes as documents, an auditable attendance
      register, status and document alerts, CSV/XLSX import & export, and REST
      endpoints (list/detail, "my meetings by membership", attendance, minutes).
      The legacy `update_event_statuses` command (which imported the removed
      `workflows.models.Event`) has been retired.
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
      `Notification.error` (alerts and share rows) and `ReportShare.last_error`;
      nothing alerts on a stalled queue, and there is no retry/dead-letter view.

## 5. Code health / maintainability

- [ ] **God modules** — `views.py` 4 819 lines, `models/workflows.py` 3 029,
      `forms.py` 1 566. The workflow base class in particular could split
      (transitions / RBAC / events / progress).
- [ ] **Migration safety** — 66 migrations with data backfills (e.g. `0044` and
      the `0066` email-envelope backfill). No migration tests or reversibility
      checks; the roadmap notes the schema "can be dropped and recreated safely
      in development", which suggests rollback is never exercised.
- [ ] **Test breadth** — the suite is targeted (heavy on reports/attachments/
      notifications); no performance/load tests, and the API permission matrix
      would benefit from more outright-negative cases.

## 6. Data & integration risks

- [ ] **SharePoint site membership** is curated by hand (`SharepointSiteMember`)
      rather than synced: the app token lacks `Sites.Manage.All`, so the designed
      PWMS-group → SharePoint-site permission mapping is not enforced in
      production and access can drift between systems.
- [ ] **Oracle sync coverage** — `sync_users_oracle` resolves Oracle org pairs
      onto the curated tree through `group_aliases.csv` and leaves unresolved
      pairs untouched; `seed_structure` / `audit_group_mapping` keep that table
      current. `validate_memberships` now checks the current `GroupMembership`
      rows and can end duplicate active rows with `--fix-duplicates`; group-name
      collisions are prevented by the `(name, parent)` constraint. Identity
      reconciliation remains a known soft spot.

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
5. Split `views.py` / `models/workflows.py` and fix the 97 lint findings.

The standout observation holds: the documentation and domain modelling are well
ahead of the operational engineering. The API is now broad, which raises the value
of the hardening items above rather than lowering it.
