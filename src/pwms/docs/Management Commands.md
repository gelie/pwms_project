# Management Commands

All commands live in `pwms/management/commands/` and run as:

```bash
.venv/bin/python manage.py <command> --help
```

> ⚠️ **Legacy note.** PWMS was migrated from an earlier app named `workflows`.
> Several commands still `import … from workflows.models` — that module no
> longer exists in this project, so **those commands fail until repointed** to
> `pwms.models` (e.g. `Group`, `Role`, `WorkflowType`, `State`, `Transition`).
> They are marked **legacy** below.
>
> The reworked Oracle sync suite (`sync_groups_oracle`, `sync_roles_oracle`,
> `sync_users_oracle`), `populate_sites`, `check_referral_deadlines` and
> `show_workflow_hierarchy` import `pwms.models` directly and run cleanly; the
> `legacy` / `pending repoint` statuses below apply to the rest.

## Synchronisation (Oracle / legacy source)

| Command | Purpose | Status |
| --- | --- | --- |
| `sync_base` | shared base/helpers for the Oracle sync suite | legacy |
| `sync_groups_oracle` | build the Parliament group hierarchy from Oracle (org tree under `Administration`) | legacy |
| `sync_roles_oracle` | create/update standard parliamentary roles (deduped, case/acronym normalised) | legacy |
| `sync_users_oracle` | import users & group memberships from Oracle via `pwms/membership/sync_service.py` | legacy |

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
| `scrape_parliament_committees` | scrape committee chairpersons & members from parliament.gov.za, import members | legacy |

## Workflow definitions & tooling

| Command | Purpose | Status |
| --- | --- | --- |
| `export_workflow_type` | export a `WorkflowType` (states + transitions) to JSON | legacy |
| `import_workflow_type` | upsert a `WorkflowType` from a JSON backup | legacy |
| `import_states` | import workflow states from CSV for a workflow type | legacy |
| `generate_diagrams` | render workflow-type diagrams (states/transitions) | legacy |

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

## Maintenance & inspection

| Command | Purpose | Status |
| --- | --- | --- |
| `fix_missing_group_access` | create `WorkflowGroupAccess` for instances missing it — **superseded by `sync_type_group_access`** | legacy |
| `sync_type_group_access` | backfill `WorkflowGroupAccess` (owning group + viewer groups) from each instance's `WorkflowType` | ✅ live |
| `show_workflow_hierarchy` | print the parent/child tree of workflow instances | ✅ live — see below |
| `validate_memberships` | integrity-check group memberships | pending repoint |

### Type-level group access

Configuring a `WorkflowType` — its owning `group` and its `viewer_groups` —
materialises `WorkflowGroupAccess` rows on **new** instances only
(`AbstractLegislativeWorkflow.materialize_group_access()`).
`sync_type_group_access` applies the current configuration to instances that
predate it: the owning group flagged `is_primary`, each viewer group read-only.
It is idempotent and never overwrites an existing row, so it is safe to re-run.

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

## Roadmap hooks

New automation (email/notifications/SharePoint) will likely arrive as **new**
commands plus **background-task** jobs (`django-background-tasks` is already
installed). When you add one:

- name it for the job it does (do **not** call it `test.py` — that shadows
  Django’s built-in `test` command);
- prefer current imports from `pwms.models`;
- document it here.
