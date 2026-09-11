# Roadmap & Planned Integrations

This page captures where PWMS is heading and the **groundwork already in place**
for each item. It is a living plan — update it as features land.

Status legend: ✅ shipped · 🧩 groundwork ready · 🔜 planned

---

## What ships today (✅)

- Workflow engine (`WorkflowType`/`State`/`Transition`) + `InternationalResolution`
- ContentType RBAC (`WorkflowGroupAccess`, `WorkflowRolePermission`,
  `WorkflowStatePermission`) with `instance.can(user, action)`
- Full auditing (`auditlog` CRUD + `TransitionLog`) with API access
- Append-only domain event log (`EventType` registry + `WorkflowEvent`) with
  declarative transition guards (`Transition.required_event_types`)
- DRF API + OpenAPI (Swagger/ReDoc), admin, migrations, tests
- django-ninja evaluation spike (`/ninja/`)

---

## Email & notifications (🔜 — 🧩 groundwork)

**Goal:** notify the right people when things happen or are due.

**Groundwork already present**

- `Transition.notify_roles` — which roles should be alerted when a transition fires.
- `check_referral_deadlines`, `notify_deadlines`, `send_overdue_alerts`,
  `check_delegation_expirations` commands (to be repointed to `pwms.models`).
- `django-background-tasks` installed → ideal queue for async email dispatch.
- `transition.requires_comment` and `TransitionLog` give the context for a
  message (who, from → to, note).
- `User.email` + role/group lookups via `GroupMembership` for addressing.

**Design sketch**

1. Add an SMTP/mailer configuration block (env-driven: host/user/pass/from —
   use `python-decouple` like other settings).
2. Create a `Notification` model (recipient user, channel [email/in-app], type,
   template context, delivered_at, read_at) OR reuse a mail queue table.
3. On `perform_transition()` and on referral, enqueue notification jobs instead
   of sending inline.
4. Trigger deadline/overdue scans from the existing management commands as
   background tasks (scheduled via cron / a beat-like loop).

**Acceptance:** role members receive email when a transition they are subscribed
to (`notify_roles`) fires; referral-deadline reminders are sent; all sends are
recorded.

---

## SharePoint integration (🧩 → 🔜)

**Goal:** store/share instrument documents (drafts, gazettes, supporting files)
with a parliamentary document library.

**Already shipped (✅)**

- Offline tenant sync via `manage.py populate_sites`: local `SharepointSite` +
  `SharepointDrive` tables, a DB-cached app token, and pagination-aware Graph
  calls.
- Best-effort `SharepointSiteMember` sync is implemented but currently skipped
  — the app token lacks `Sites.Manage.All` / `Sites.FullControl.All`. Full
  details: [SharePoint Sync](./SharePoint%20Sync.md).

**Groundwork / considerations**

- File upload scaffolding exists (`python-multipart`, `media/` uploads for
  avatars; models can add document attachments).
- Document generation libs declared: `weasyprint`, `reportlab`, `openpyxl`,
  `markdown` (exports/PDFs before upload).
- Access model maps well to SharePoint permission scopes (committee/house →
  SharePoint site/librarb).

**Options to evaluate**

| Option | Notes |
| --- | --- |
| Microsoft Graph API (files + sites) | OAuth2 app registration; good for metadata + permissions |
| SharePoint REST/SOAP + on-premise auth | if against an on-prem farm |
| Local storage → sync worker | store locally, sync to SharePoint asynchronously |

**Acceptance:** attach documents to a workflow instance, version them, and keep
an audit trail of upload/download; permissions mirror PWMS group access.

---

## Notifications channel & email templating (🔜)

- HTML email templates in `src/pwms/templates/emails/`, rendered with
  request-free `render_to_string`.
- Respect RBAC when composing (never leak content a recipient may not view).
- Log every dispatch in the audit layer for accountability.

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

## Web UI expansion (🔜)

- Workflow instance detail pages: timeline (TransitionLog + auditlog), current
  state, available actions gated by `instance.can(...)`, referrals.
- Create/edit forms with date pickers (`django-flatpickr`), HTMX partials.
- Committee/House dashboards and a search/filter layer (`django-filter` declared).
- Admin improvements for managing workflow definitions and RBAC.

---

## Exports & reporting (🔜)

- PDF exports via `weasyprint`/`reportlab` (formal instruments).
- Spreadsheet exports via `openpyxl` (lists, tracking, stats).
- Markdown→HTML rendering for note fields (`markdown` declared).

---

## Domain events & calendar

✅ **Domain event log shipped** — `EventType` (data-driven registry) +
append-only `WorkflowEvent`, with transition guards
(`Transition.required_event_types`); see [Data Model §5](./Data%20Model.md).
Seeded types: report-document-attached, atc-update-published,
implementation-reported, referral-created, referral-responded,
referral-recalled, referral-expired. Seeded guard:
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

🔜 Still to do: repoint the legacy `check_referral_deadlines` command
(reminders + auto-recall) at `WorkflowReferral` (`due_date`,
`deadline_notified_at`, `mark_expired()`).

---

## Non-functional roadmap

- Scheduled execution for background jobs (cron/systemd or a beat-style runner).
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
