# Management Commands

All commands live in `pwms/management/commands/` and run as:

```bash
.venv/bin/python manage.py <command> --help
```

> ⚠️ **Legacy note.** PWMS was migrated from an earlier app named `workflows`.
> Several commands still `import … from workflows.models` — that module no
> longer exists in this project, so **those commands fail until repointed** to
> `pwms.models` (e.g. `Group`, `Role`, `WorkflowType`, `State`, `Transition`).
> They are marked **legacy** below. Two of them (`notify_deadlines`,
> `send_overdue_alerts`) import `workflows.tasks` *inside a function body*, so
> the command still loads and only fails when that code path runs.
>
> The reworked Oracle sync suite (`sync_groups_oracle`, `sync_roles_oracle`,
> `sync_users_oracle`), `scrape_parliament_committees`, `generate_diagrams`,
> `populate_sites`, `load_places`, `import_groups`, `import_bill_versions`,
> `check_referral_deadlines`, `send_scheduled_report_shares`,
> `sync_type_group_access` and `show_workflow_hierarchy` import `pwms.models`
> directly and run cleanly; the `legacy` / `pending repoint` statuses below apply
> to the rest.

## Synchronisation (Oracle / legacy source)

| Command | Purpose | Status |
| --- | --- | --- |
| `sync_base` | shared base for the Oracle suite (`OracleSyncBase`) — a helper module, **not** an invokable command | helper |
| `sync_groups_oracle` | build the Parliament group hierarchy from Oracle (org tree under `Administration`) | ✅ live |
| `sync_roles_oracle` | create/update standard parliamentary roles (deduped, case/acronym normalised) | ✅ live |
| `sync_users_oracle` | import users & group memberships from Oracle via `pwms/membership/sync_service.py` | ✅ live |

> **Identity ownership.** `sync_users_oracle` owns only users whose
> `identity_source` is `erp`: locally managed users (`identity_source="local"`,
> e.g. Ministers appointed from outside the ERP) are skipped with a warning
> rather than overwritten, and memberships in executive groups (`executive` /
> `presidency` / `ministry`) are never ended by the sync — losing an ERP payroll
> record does not end a political appointment.

## SharePoint (Graph) sync

| Command | Purpose | Status |
| --- | --- | --- |
| `populate_sites` | mirror SharePoint sites, drives (and best-effort site members) into the local `Sharepoint*` tables | ✅ live — see [SharePoint Sync](./SharePoint%20Sync.md) |

Key options: `--failures-file PATH` writes the sites whose member list could not
be read to a CSV report for the SharePoint admin (default:
`logs/site_member_failures_<timestamp>.csv`).

> Site-**member** sync is implemented but is currently skipped at runtime: it
> needs Graph application permissions (`Sites.Manage.All` /
> `Sites.FullControl.All`) that the configured app token does not yet have.
> Sites and drives sync fine with the current token. Skipped sites are written
> to the CSV failure report described above.

## Reference data (GeoNames)

| Command | Purpose | Status |
| --- | --- | --- |
| `load_places` | fill `Country` / `City` from the bundled GeoNames extract (`src/pwms/data`) for the engagement location pickers | ✅ live — see [Data Model](./Data%20Model.md#7-reference-data--countries--cities) |

Key options: `--data-dir DIR` reads `countries.csv` and `cities.csv[.gz]` from
somewhere other than `src/pwms/data`. Re-running is safe: countries are upserted
on their ISO code and cities are matched on
`(country, name, latitude, longitude)`, so existing primary keys (and the reports
pointing at them) survive.

## Group & organisation data

| Command | Purpose | Status |
| --- | --- | --- |
| `import_groups` | import group names from a CSV/text list as `Group` records under a parent group | ✅ live |

Key options: `--parent NAME_OR_SLUG` (required) is the group the names hang
under; `--type GROUP_TYPE` (required) must be one of `Group.GROUP_TYPE_CHOICES`
(e.g. `ministry`). The file is parsed as blank-line-separated sections — each
section's first line is a heading and the rest are names — and `--section NAME`
imports a single section only. Names are whitespace-normalised and matched on
the `(name, parent)` natural key, so re-running is safe. `--dry-run` previews the
changes.

## Committee scraping

| Command | Purpose | Status |
| --- | --- | --- |
| `scrape_parliament_committees` | scrape committee chairpersons & members from parliament.gov.za, import members | ✅ live |

## Workflow definitions & tooling

| Command | Purpose | Status |
| --- | --- | --- |
| `export_workflow_type` | export a `WorkflowType` (states + transitions) to JSON | legacy |
| `import_workflow_type` | upsert a `WorkflowType` from a JSON backup | legacy |
| `import_states` | import workflow states from CSV for a workflow type | legacy |
| `generate_diagrams` | render workflow-type diagrams (states/transitions) | ✅ live |

### Diagram generation

`generate_diagrams` renders every enabled `WorkflowType` as a Graphviz image
and, by default, a matching Mermaid Markdown file.

| Option | Effect |
| --- | --- |
| `--format {dot,png,svg,pdf}` | image format (default `svg`); PNG is rendered at 200 dpi |
| `--output-dir DIR` | where images are written (default `WORKFLOW_DIAGRAM_OUTPUT_DIR`) |
| `--workflow-type NAME` | restrict to a single workflow type |
| `--cluster-states` | group states into Entry / In progress / Closed zones |
| `--show-roles` | append allowed roles to transition labels |
| `--include-descriptions` | include state/transition descriptions |
| `--no-legend` | omit the legend block |
| `--mermaid-dir DIR` | where Mermaid `.md` files are written (default `WORKFLOW_DIAGRAM_DOCS_DIR`) |
| `--no-mermaid` | skip Mermaid export |

State colours come from `State.color`, so each workflow keeps its own curated
palette; transitions that require a comment render as amber dashed arrows.
Mermaid exports are plain ` ```mermaid ` blocks, so GitHub and VS Code render
them inline with no build step.

The rendered image is also what a workflow instance's **Diagram** tab serves
(`GET /pwms/instruments/{public_id}/diagram.svg`), so re-running this command is
what refreshes that tab.

## Bills

| Command | Purpose | Status |
| --- | --- | --- |
| `import_bill_versions` | bulk-import preserved bill versions (BRS §15A) from a CSV into `BillVersion` | ✅ live |

Bulk-imports the version history for bills that already exist in PWMS. Required
columns are `bill_number` and `version_label`; the optional columns are
`version_type` (slug or label), `version_date` (ISO, blank = today),
`document_url`, `notes` and `is_current` (`1`/`true`/`t`/`yes`/`y`):

```csv
bill_number,version_label,version_type,version_date,document_url,notes,is_current
B 12—2026,B 12—2026,introduced,2026-05-01,,Introduced version,no
B 12—2026,B 12—2026 (1st amendment),amended,2026-06-01,https://docs/v2,Amended clause 4,yes
```

Rows are upserted on `(bill, version_label)`, so re-running after fixing the file
is safe — matching rows are skipped, changed rows are updated, and **nothing is
ever deleted** (version integrity). Rows with an unknown bill number, version
type or date are reported and skipped, and the command ends with a summary.
`--dry-run` previews the changes without writing.

## Notifications & deadlines

| Command | Purpose | Status |
| --- | --- | --- |
| `check_delegation_expirations` | detect expiring delegations/memberships | pending repoint |
| `check_referral_deadlines` | warn when a referral deadline is approaching, and expire referrals past it | ✅ live — see below |
| `send_scheduled_report_shares` | email due scheduled report shares and advance their schedules | ✅ live — see below |
| `notify_deadlines` | dispatch deadline notifications (optionally scoped by workflow id) | pending repoint |
| `send_overdue_alerts` | send overdue-workflow alerts / emails | pending repoint |
| `update_event_statuses` | recompute/refresh statuses of events or instances | pending repoint |

### Referral deadline reminders

`check_referral_deadlines` dispatches reminders for open `WorkflowReferral` rows
and closes the ones nobody answered:

| Window | Action |
| --- | --- |
| due within 24 hours, not yet reminded | alert the referred group's active members and the people the record names (owner, assignee), then stamp `deadline_notified_at` |
| due within 1 hour, already reminded | send the final reminder (no second stamp) |
| due date passed | `mark_expired()` the referral (emits `referral-expired`) and alert the parties |

Only `status="open"` referrals are considered, so an answered or recalled one is
left alone. Reminders go through `pwms.notifications` and are logged as
`referral-deadline` alerts (an `in_app` row and an `email` row per recipient);
the expiry alert is raised by `WorkflowReferral.mark_expired()` and is not sent
by the command. Mail is one message per recipient, so no recipient sees another's
address. `--dry-run` reports what would be sent or expired without sending mail
or writing anything.

### Scheduled report shares

`send_scheduled_report_shares` drains the shares a reader made repeat from the
reports page (daily, weekly or monthly). It emails each due share — the link,
plus the rendered document when the share names one — and moves its
`next_send_at` on by one interval.

| Option | Effect |
| --- | --- |
| `--dry-run` | list the shares that are due without emailing or advancing anything |
| `--queue` | queue the repeating background task instead of running once (see below) |

Two ways to run it, both calling the same service:

* **periodically** — from cron / systemd / a beat-style runner, like
  `check_referral_deadlines`;
* **as a background task** — `--queue` registers one repeating
  `django-background-tasks` task (`pwms.tasks.deliver_scheduled_report_shares`,
  hourly) that `manage.py process_tasks` then keeps draining.

A send that fails does not stall the batch: the reason is recorded on the share
(`last_error`) and logged, and the schedule still advances, so a broken address
is retried at the next interval rather than on every run. Revoking or expiring a
share takes it out of the queue immediately.

## Maintenance & inspection

| Command | Purpose | Status |
| --- | --- | --- |
| `fix_missing_group_access` | create `WorkflowGroupAccess` for instances missing it — **superseded by `sync_type_group_access`** | legacy |
| `sync_type_group_access` | backfill `WorkflowGroupAccess` (owning group + viewer groups) from each instance's `WorkflowType` | ✅ live |
| `show_workflow_hierarchy` | print the parent/child tree of workflow instances | ✅ live — see below |
| `seed_demo_data` | empty the four workflow registers and reseed them with presentation-ready mock records | ✅ live — see below |
| `validate_memberships` | integrity-check group memberships | pending repoint |

### Type-level group access

Configuring a `WorkflowType` — its owning `group` and its `viewer_groups` —
materialises `WorkflowGroupAccess` rows on **new** instances only
(`AbstractLegislativeWorkflow.materialize_group_access()`).
`sync_type_group_access` applies the current configuration to instances that
predate it: the owning group flagged `is_primary` with view + edit, each viewer
group read-only. It is idempotent and never overwrites an existing row, so it is
safe to re-run — a grant an administrator has narrowed stays narrowed (migration
`0037` widened the primary rows that predated the edit default).

| Option | Effect |
| --- | --- |
| `--workflow-type NAME_OR_SLUG` | restrict the backfill to one workflow type |
| `--dry-run` | report what would be created without writing anything |

### Hierarchy inspection

`show_workflow_hierarchy` prints the parent/child tree that
`WorkflowRelationship` builds across the concrete workflow registers. There is
no single `Workflow` model to query, so the command gathers all four types and
uses each instance's `parent_workflow` / `sub_workflows` helpers.

| Option | Effect |
| --- | --- |
| `--workflow-id UUID` | show one workflow's root-to-leaf path, parent, siblings, children and counts (matched on `public_id`) |
| `--show-all` | every hierarchy, with a descendant count per root |
| `--show-orphans` | workflows with neither parent nor children |
| `--workflow-type NAME` | restrict to one workflow type name |

Rows are colour-coded by status: overdue first, then `urgent` / `high` priority.

### Demo / mockup data

`seed_demo_data` is a **destructive** convenience command for demos and mockup
walkthroughs. It deletes every delegation report, international resolution,
international agreement and bill — together with everything keyed to them
(RBAC rows, `TransitionLog` / `WorkflowEvent` / auditlog history, referrals,
notifications, attachments and bill versions) — and repopulates the four
registers with realistic records spread across each type's states.

Records are walked through their real state machines with
`perform_transition`, so every state change, document, referral and alert on
the detail pages is genuine. Only the timestamps are back-dated, spread over
the preceding months so the history, activity feed and deadline panels read
like a live system. References (`DR-…`, `IA-…`, B-numbers) are numbered in
date order, and a curated set of in-app alerts is written for the bell.

Automatic alert fan-out is suppressed while seeding, so no mail is queued and
no background worker is needed.

| Option | Effect |
| --- | --- |
| `--force` | skip the confirmation prompt (required for unattended runs) |

Users, groups, roles, workflow types, states and transitions are **not**
touched — only the four registers and their dependants — so the command can be
re-run at any time. Sign in as a superuser to see all four registers at once;
a section member sees only the registers their group owns.

```bash
python manage.py seed_demo_data --force
```

## Background tasks (the queue worker)

Two jobs run through `django-background-tasks` rather than inline, so
**`manage.py process_tasks` has to be running** for them to happen:

| Task | Queued by | What it does |
| --- | --- | --- |
| `pwms.tasks.deliver_notification_email` | `pwms.notifications.dispatch.queue_email()`, inside the transaction that wrote the alert row | delivers one alert email, retrying a transient failure (`MAX_ATTEMPTS` + backoff) |
| `pwms.tasks.deliver_scheduled_report_shares` | `send_scheduled_report_shares --queue`, once | repeating drain for scheduled report shares |

`pwms/tasks.py` is the module the worker discovers — `process_tasks` imports
`<installed app>.tasks` — and it is the only place that knows the queue exists:
the work itself lives in the owning module, so the same code runs from a request,
a command or a test. Queueing happens **inside** the caller's transaction, so a
task commits with the row it acts on, and rolls back with it.

### Running the worker under systemd

One long-running service is all it takes. Adjust the paths to your checkout; this
assumes the repo at `/srv/pwms`, so `.env`, `manage.py`, `.venv/`, `logs/` and
`media/` all sit there as the README describes.

`/etc/systemd/system/pwms-worker.service`:

```ini
[Unit]
Description=PWMS background task worker (django-background-tasks)
Documentation=file:///srv/pwms/src/pwms/docs/Management%20Commands.md
# Drop `postgresql.service` if the database is not on this host.
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=pwms
Group=pwms
WorkingDirectory=/srv/pwms

# Unbuffered output, so `journalctl -u pwms-worker -f` is actually live.
Environment=PYTHONUNBUFFERED=1

# `--sleep` is the poll interval used when the queue is empty (default 5s).
ExecStart=/srv/pwms/.venv/bin/python /srv/pwms/manage.py process_tasks --sleep 5

Restart=always
RestartSec=10

# A task may be mid-flight when the service stops.
TimeoutStopSec=90

# Hardening: the worker needs its own checkout and the network, nothing else.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

Install and start it:

```bash
sudo useradd --system --shell /usr/sbin/nologin pwms
sudo chown -R pwms:pwms /srv/pwms/logs /srv/pwms/media
# The worker reads its secrets from the repo's .env (python-decouple), so the
# service account has to be able to read it — and nobody else.
sudo chown pwms:pwms /srv/pwms/.env
sudo chmod 600 /srv/pwms/.env
sudo -u pwms /srv/pwms/.venv/bin/python /srv/pwms/manage.py check   # smoke test

sudo systemctl daemon-reload
sudo systemctl enable --now pwms-worker
systemctl status pwms-worker
journalctl -u pwms-worker -f       # the worker's own output
```

What you need to know before copying that:

- **The service account needs three things:** read access to the checkout (and to
  the `.env` beside it), write access to `logs/` (the `LOGGING` file handler) and
  to `media/` (user uploads). Everything else can stay root-owned and read-only.
- **Do not pass `--dev`** outside development — it turns on the autoreloader.
  `--duration` (default `0`, meaning "run forever") and `--queue` (serve one named
  queue) are the other knobs.
- **One service is enough.** With the `BACKGROUND_TASK_RUN_ASYNC = True` and
  `BACKGROUND_TASK_ASYNC_THREADS = 4` settings this project ships, a single worker
  runs several tasks at once. A second service is *also* safe: a worker claims a
  task with a conditional `UPDATE`, so two of them cannot run the same row.
- **Deploying outside `/srv`** (under `/home`, say) means dropping
  `ProtectHome=true`. Tightening to `ProtectSystem=strict` means adding
  `ReadWritePaths=/srv/pwms/logs /srv/pwms/media`.
- **Stopping it.** django-background-tasks wires its "stop after the current task"
  flag to `SIGTSTP` only — not to `SIGTERM` — so systemd's default signal ends the
  process where it stands. That is safe, not merely tolerable: a task killed
  mid-flight keeps its lock, and the library offers it to the next worker once the
  lock is older than `MAX_RUN_TIME` (an hour by default). The alert row is still
  `pending`, so nothing is lost; `deliver_email()` only acts on a `pending` row,
  so nothing is sent twice. To have the worker finish the task in hand instead,
  opt into the library's flag:

  ```ini
  KillSignal=SIGTSTP
  ```

  (`SIGTSTP` normally *suspends*; the library overrides it with a handler that
  just raises the flag, so the process exits after its current task. Keep
  `TimeoutStopSec` generous enough for the longest task.)

### Periodic commands (systemd timer)

The queue is not the only clock PWMS runs on: `check_referral_deadlines` above is
a cron-style job that must run on a schedule and is **not** queued (scheduled
report shares are — they are a task, so the worker drains those). A systemd timer
beats a crontab here: its output lands in the journal beside everything else, and
`Persistent=true` catches up a run missed while the host was down.

The reminders it raises *are* alert email, so the worker still has to be running to
send them.

`/etc/systemd/system/pwms-referral-check.service`:

```ini
[Unit]
Description=Check PWMS referral deadlines
Documentation=file:///srv/pwms/src/pwms/docs/Management%20Commands.md
After=network-online.target postgresql.service

[Service]
Type=oneshot
User=pwms
Group=pwms
WorkingDirectory=/srv/pwms
ExecStart=/srv/pwms/.venv/bin/python /srv/pwms/manage.py check_referral_deadlines
```

`/etc/systemd/system/pwms-referral-check.timer`:

```ini
[Unit]
Description=Run the PWMS referral deadline check hourly

[Timer]
OnCalendar=hourly
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pwms-referral-check.timer
systemctl list-timers pwms-referral-check.timer
```

## Roadmap hooks

New automation (email/notifications/SharePoint) arrives as **new** commands plus
**background-task** jobs in `pwms/tasks.py`. When you add one:

- name it for the job it does (do **not** call it `test.py` — that shadows
  Django’s built-in `test` command);
- prefer current imports from `pwms.models`;
- document it here.
