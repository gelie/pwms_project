# Roadmap & Planned Integrations

This page captures where PWMS is heading and the **groundwork already in place**
for each item. It is a living plan — update it as features land.

Status legend: ✅ shipped · 🧩 groundwork ready · 🔜 planned

---

## What ships today (✅)

- Workflow engine (`WorkflowType`/`State`/`Transition`) driving four instruments
  (`DelegationReport`, `InternationalResolution`, `InternationalAgreement`,
  `Bill`)
- ContentType RBAC (`WorkflowGroupAccess`, `WorkflowRolePermission`,
  `WorkflowStatePermission`) with `instance.can(user, action)`
- Full auditing (`auditlog` CRUD + `TransitionLog`) with API access
- Append-only domain event log (`EventType` registry + `WorkflowEvent`) with
  declarative transition guards (`Transition.required_event_types`)
- In-app alerts + logged email dispatch (`Notification`, `pwms/notifications/`),
  raised from the workflow domain methods and the create views
- SharePoint document attachments with on-demand version history and a document
  audit trail (`pwms.Attachment` / `AttachmentVersion`)
- Permission-scoped reporting: report builder with an HTMX preview, xlsx / pdf /
  html / csv exports, per-instrument formal documents, token link/email sharing
  and scheduled delivery (`pwms.reporting`, `ReportShare`)
- Active Directory sign-in with a local-database fallback, and SharePoint tenant
  sync (`populate_sites`) into local mirror tables
- DRF API + OpenAPI (Swagger/ReDoc), admin, migrations, tests
- django-ninja evaluation spike (`/ninja/`)

---

## Email & notifications (✅ shipped)

**Goal:** notify the right people when things happen or are due.

**Shipped**

- `Notification` (`pwms/models/notifications.py`, migration `0026_notification`)
  — one row per (recipient, channel, event); the table is deliberately also the
  delivery log (see [Data Model](./Data%20Model.md)).
- `pwms/notifications/`: `dispatch.py` (audience rules + delivery), `context.py`
  (the bell's context processor), re-exported from `__init__.py`.
- Hooked on explicit domain methods, never `save()`:
  `AbstractLegislativeWorkflow.perform_transition()` (`notify_transition`) and
  `.refer()` (`notify_referral_created`); `WorkflowReferral.respond()` /
  `recall()` / `mark_expired()` (`notify_referral_closed`); and the four create
  views in `pwms/views.py` (`notify_workflow_created`). Fixtures, imports and
  admin writes notify nobody.
- Each dispatch writes an `in_app` row (sent immediately) and — when the
  recipient has an email address — an `email` row (`pending` → `sent`/`failed`).
  Delivery is **queued**: the row's task is written in the *same transaction* as
  the row itself (`pwms/tasks.py`, django-background-tasks), so a workflow change
  that rolls back queues nothing, and a crash straight after COMMIT cannot lose
  the send. Dispatching never talks to a mail server, so it cannot raise.
- The worker (`manage.py process_tasks`) sends from the queue. A transient
  failure leaves the row `pending` with the reason in `error` and raises, so the
  task is retried (django-background-tasks' own `MAX_ATTEMPTS` and backoff — 3
  attempts by default); the row is marked `failed` once the attempts are spent.
  `attempts` and `error` are therefore the log of a mail that never went out.
  `queue_email()` hands a row to the queue; `deliver_email()` makes one attempt.
- Audience: the type's officers (active members of `WorkflowType.group` holding
  one of its `create_roles`), the roles a transition names
  (`Transition.notify_roles` plus the roles allowed to act next) and the people
  the record names (`owner`, `assigned_to`) — minus the actor and anyone who
  cannot VIEW the instance (`pwms.services.permissions.resolve`). Referrals use
  `referral_audience()`: the referred group's active members plus the people the
  record names (`owner`, `assigned_to`), deliberately not RBAC-filtered (a
  referred committee may hold no `WorkflowGroupAccess` row — the referral is the
  entitlement) and carrying only the referral's own facts.
- `check_referral_deadlines` dispatches through `pwms.notifications` (logged as
  `referral-deadline` alerts); `mark_expired()` raises the expiry alert itself
  (see [Management Commands](./Management%20Commands.md)).
- Dev mail: `MAILERS["default"]` uses the console backend; `DEFAULT_FROM_EMAIL`
  and `PWMS_BASE_URL` set the sender and the absolute links.
- UI: the navbar bell (`templates/navbar.html`, `static/css/style.css`), the
  alerts page (`templates/pwms/notifications.html`) and the views/URLs
  `pwms:notifications`, `pwms:notification_open` and
  `pwms:notifications_read_all`. Body template:
  `templates/emails/notification.txt`.
- Tests: `pwms/tests_notifications.py` covers the audience rules, RBAC
  filtering, the two channel rows, queued (not inline) delivery, the
  same-transaction rollback guarantee, per-recipient delivery, the retry policy
  and terminal failure, and the bell/page/read views.

> `Transition.notify_roles` exists but the RBAC seeds leave it unpopulated, so
> the working audience for a transition is the type's `create_roles` officers
> (plus the other sources above) until an administrator fills it in per
> transition.

**Remaining (🔜)**

- HTML alert templates — the *alert* email is still plain-text
  (`emails/notification.txt`); report-share mail already sends HTML + text
  (`emails/report_share.html` / `.txt`).

**Acceptance:** role members receive email when a transition they are subscribed
to (`notify_roles`) fires; referral-deadline reminders are sent; all sends are
recorded.

---

## SharePoint integration (✅ shipped · 🔜 restore & version pinning)

**Goal:** store/share instrument documents (drafts, gazettes, supporting files)
with a parliamentary document library.

**Already shipped (✅)**

- Offline tenant sync via `manage.py populate_sites`: local `SharepointSite` +
  `SharepointDrive` tables, a DB-cached app token, and pagination-aware Graph
  calls.
- Best-effort `SharepointSiteMember` sync is implemented but currently skipped
  — the app token lacks `Sites.Manage.All` / `Sites.FullControl.All`. Full
  details: [SharePoint Sync](./SharePoint%20Sync.md).
- **Document attachments (2026-09-15)** — every workflow detail page has an
  **Attachments** section backed by an HTMX SharePoint picker: browse the user's
  sites as an expandable tree, link an existing document, or upload a local file
  into the selected folder and attach it in one step. Attachments are
  `pwms.Attachment` rows (generic FK to the instance, so one table serves every
  subclass); the file itself stays in SharePoint and detaching removes only the
  link. Code: `pwms/models/attachments.py`, `pwms/services/attachments.py`, the
  `attachment_*` views, and `pwms/templates/pwms/partials/attachment_*.html`.
- **Document versioning & audit trail (2026-09-15)** — each attachment's row
  opens a version-history panel listing SharePoint's own versions (label, size,
  author, timestamp), mirrored into `pwms.AttachmentVersion` on open and each
  downloadable by resolving Graph's pre-authenticated redirect (the file is never
  streamed through the app). Re-uploading a revision refreshes the attachment in
  place instead of failing. Attach, detach and revise are recorded explicitly as
  `document-*` `WorkflowEvent`s on the record's append-only event log, and shown
  as the section's “Document activity” list.
  Tests: `pwms/tests_attachments.py` (63 tests).

**Groundwork / considerations**

- File upload scaffolding (`python-multipart`, `media/` for avatars) is in place;
  attachment uploads go straight to SharePoint instead.
- Document generation libs (`weasyprint`, `openpyxl`, `reportlab`) now power the
  reporting exports and per-instrument documents; `markdown` is still declared.
- Access model maps well to SharePoint permission scopes (committee/house →
  SharePoint site/library).

**Options to evaluate**

| Option | Notes |
| --- | --- |
| Microsoft Graph API (files + sites) | OAuth2 app registration; good for metadata + permissions |
| SharePoint REST/SOAP + on-premise auth | if against an on-prem farm |
| Local storage → sync worker | store locally, sync to SharePoint asynchronously |

**Acceptance:** attach documents to a workflow instance, version them, and keep
an audit trail of upload/download; permissions mirror PWMS group access.

**Remaining (🔜)** — *restoring* a superseded version (Graph's
`versions/{id}/restore` needs a write-scoped app permission the registration does
not hold yet; the version list and per-version download ship today) and *pinning*
a version to a record, so “the version tabled” survives later revisions.

---

## Notifications channel & email templating (🔜)

- HTML alert templates alongside the plain-text
  `src/pwms/templates/emails/notification.txt`; rendering is already
  request-free (`render_to_string`), so this is the outstanding piece.
  (Report-share mail already sends HTML + text — `emails/report_share.html` /
  `.txt` — but the alert path itself is text-only.)

The rest originally sketched here has shipped: composition is RBAC-aware (a
recipient who cannot VIEW the instance is dropped before the alert is written),
and every dispatch is logged in the `Notification` log (see *Email &
notifications* above).

---

## Additional instruments — Bill / Motion / Question (🧩 → 🔜)

The architecture already supports any number of concrete workflow models:

```python
class Bill(AbstractLegislativeWorkflow):
    bill_number = models.CharField(max_length=50, unique=True)
    ...
```

Per new model, the checklist is:

1. subclass `AbstractLegislativeWorkflow` and add model-specific fields;
2. `makemigrations` + `migrate`;
3. register with `auditlog` in `pwms.apps.PwmsConfig.ready()`;
4. add an `*AuditHistoryView` + URL + root listing in the API;
5. define its `WorkflowType` states/transitions (via admin or import command).

---

## Web UI expansion

✅ **Workflow instance CRUD shipped (2026-09-11)** — list/detail/create/update/delete
for `DelegationReport` and `InternationalResolution` (`pwms/views.py` + `pwms/forms.py`,
templates in `src/pwms/templates/pwms/`), reached from the navbar *Workflows*
dropdown. Detail pages show metadata, participants, resolutions, BR03 updates,
referrals and the audit trail; list pages have a free-text filter.

Remaining (🔜):

- Extend the group-scoped RBAC (`WorkflowType.group` + `create_roles`) to
  view/edit/delete/transition in the web UI — it currently gates creation only.
- Timeline view merging `TransitionLog` + auditlog; available actions gated by
  `instance.can(...)`; performing transitions from the web UI; referral actions.
- Create/edit forms with date pickers (`django-flatpickr`), HTMX partials.
- Committee/House dashboards and a search/filter layer (`django-filter` declared).
- Admin improvements for managing workflow definitions and RBAC.

---

## Exports & reporting (✅ shipped)

**Goal:** turn the registers into figures people can filter, preview, forward and
file — without ever widening anyone's access.

**Shipped (2026-09-15)** — `pwms.reporting` (`builder` / `exports` / `instruments`
/ `sharing`):

- **Report builder** (`/pwms/reports/`, `views.reports`): one page spanning all
  four workflow registers. Filters: report type, free text (matching the common
  fields *and* each instrument's own — see `SEARCH_FIELDS`), workflow type,
  state, priority, owner, group, period or explicit date range (created /
  updated / deadline), overdue-only, due-soon, unassigned-only; sort by created,
  deadline, priority or title.
- **Permission-scoped**: every figure comes from `visible_instances`, the same
  chain the lists and detail pages use, so a report — and its export — can never
  show a row the reader could not open (`ReportData`).
- **Live preview**: the filter form is an HTMX form that swaps
  `pwms/partials/report_preview.html` (and degrades to a normal GET without JS).
  The preview carries summary stats, breakdowns (type, state, public status,
  priority, owner), the workflow register, the merged activity feed
  (`TransitionLog` + `WorkflowEvent`) and referrals. The on-screen table caps at
  `PREVIEW_ROW_LIMIT`; exports always carry the full set.
- **Exports** (`reports_export`, `?format=`): **xlsx** (multi-sheet workbook via
  `openpyxl`), **pdf** (WeasyPrint over the same document), **html**
  (self-contained page, branding embedded as a data URI) and **csv**. The
  print/export document is `pwms/templates/pwms/pdf/report_export.html`.
- **Sharing** (`reports_share` / `report_shared`, model `ReportShare`, migration
  `0032_reportshare`): mint a token-addressed read-only link (optionally
  expiring), optionally email it to a recipient list (HTML + text) with the
  exported file attached. A share always re-renders **the creator's** figures, so
  a link never widens to the reader's access; opening it is counted
  (`access_count` / `last_accessed_at`), and a revoked or expired link answers
  410 Gone.
- **Per-instrument documents** (`pwms.reporting.instruments`, one route for
  every type: `/pwms/instruments/<public_id>/document/?format=pdf|html`): a
  single delegation report, resolution, agreement or bill as a formal,
  filed-style document. Every detail page has a **Document** menu for it. The
  document is *data-driven* — its facts, notes and tables are shaped in Python
  per type (``_TYPE_SECTIONS`` / ``_TYPE_NOTES`` / ``_TYPE_TABLES``), so
  `templates/pwms/pdf/instrument_export.html` never learns about B-numbers — and
  it reuses the register report's chrome through `templates/pwms/pdf/_formal_base.html`
  (one CSS, one masthead, one footer, logo embedded as a data URI).
- **Scheduled delivery** (`ReportShare.schedule`, migration `0033`): a share can
  repeat daily, weekly or monthly. `send_scheduled_report_shares` drains the due
  shares (see
  [Management Commands](./Management%20Commands.md#scheduled-report-shares)), or
  `--queue` registers a repeating `django-background-tasks` task
  (`pwms/tasks.py`) for `manage.py process_tasks` to serve. Each send advances
  `next_send_at` and records `last_sent_at` / `send_count` / `last_error`;
  failures are recorded rather than raised, so one bad share never stalls the
  batch. Revoking or expiring a share takes it out of the queue at once.
- Tests: `pwms/tests_reports.py` covers filter parsing, permission scoping,
  every export format, the instrument documents (all four types), the share
  lifecycle, the shared/export paths, the scheduling arithmetic, the delivery
  service, the command and the queued background task.

**Remaining (🔜)**

- Markdown→HTML rendering for note fields (`markdown` declared).

---

## Domain events & calendar

✅ **Domain event log shipped** — `EventType` (data-driven registry) +
append-only `WorkflowEvent`, with transition guards
(`Transition.required_event_types`); see [Data Model §5](./Data%20Model.md).
Seeded types: report-document-attached, atc-update-published,
implementation-reported, referral-created, referral-responded,
referral-recalled, referral-expired (migrations `0007` / `0009`), plus the
document types `document-attached`, `document-detached` and
`document-version-added` (migration `0031`). Seeded guard:
*Delegation Report → Close – House approved* requires the ATC update event.

✅ **Condition rules shipped** — `TransitionCondition` (`no_open_referrals`,
`all_children_closed`, `field_set`) evaluated by `unmet_transition_conditions()`
alongside event guards; extend via the `TRANSITION_CONDITION_HANDLERS` registry.

✅ **Typed detail tables shipped** — `DelegationReportUpdate` (BR03 ATC update
history; `report.record_update(...)` emits `atc-update-published` when ATC
details are present) and `WorkflowReferral` (below). The legacy single-set
`atc_*` fields are now read-only "latest" properties.

🔜 **Calendar view** — the legacy `update_event_statuses` command tracked
scheduled/ongoing/completed *calendar* items (title, start/end); if a
meetings/sessions calendar is wanted it should be a separate model from the
workflow event log, reusing `deadline` + flatpickr.

---

## Referrals (✅ shipped · 🔜 background job)

`WorkflowReferral` replaced the `referred_to_groups` M2M as the source of truth:
GFK to the instance, `referred_to` (FK `Group`), `referred_by`, `referred_at`,
`due_date`, `status` (open / responded / recalled / expired / cancelled),
`responded_at/by`, `response_document_url`, recall fields. Creation via
`instance.refer(group, ...)` is gated by `State.allows_referrals`; creation and
lifecycle actions emit `referral-*` events automatically.

Migrations: `0008` (model) → `0009` (backfill one open referral per existing M2M
row + events) → `0010` (drop the M2M and legacy `atc_*` fields); auditlog no
longer registers `m2m_fields`.

✅ **Referral deadline job repointed** — `check_referral_deadlines` now runs
against `WorkflowReferral` (`due_date`, `deadline_notified_at`,
`mark_expired()`): it dispatches the 24-hour and 1-hour reminders through
`pwms.notifications` (logged as `referral-deadline` alerts) and expires
referrals whose deadline has passed (`--dry-run` previews either action). The
expiry alert is raised by `mark_expired()` itself, not by the command.

---

## Non-functional roadmap

- Scheduled execution for background jobs: the queue worker runs as a systemd
  service and the periodic commands run as systemd timers — see
  [Management Commands § Background tasks](./Management%20Commands.md#background-tasks-the-queue-worker)
  and [§ Periodic commands](./Management%20Commands.md#periodic-commands-systemd-timer).
- Production deployment concerns: `DEBUG=False`, `ALLOWED_HOSTS`, secrets in
  environment, TLS. Static/media are gathered/served separately:
  `collectstatic` collects the app static into `staticfiles/`
  (`STATIC_ROOT`, repo root) and user uploads go to `media/` (`MEDIA_ROOT`,
  repo root).
- Broader test coverage and CI; reconcile/remove the django-ninja spike once the
  API decision is made (see [System Design §6](./System%20Design.md#6-api-layer)).

---

## How to contribute changes here

Add a row/phase as features are agreed. Keep pointers to concrete code (fields,
tables, commands) so the next reader can start quickly. Update
[Management Commands](./Management%20Commands.md) when commands land, and
[Data Model](./Data%20Model.md) when models change.
