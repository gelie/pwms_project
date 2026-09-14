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
> `sync_users_oracle`) and `populate_sites` import `pwms.models` directly and
> run cleanly; the `legacy` / `pending repoint` statuses below apply to the rest.

## Synchronisation (Oracle / legacy source)

| Command | Purpose | Status |
| --- | --- | --- |
| `sync_base` | shared base/helpers for the Oracle sync suite | legacy |
| `sync_groups_oracle` | build the Parliament group hierarchy from Oracle (org tree under `Administration`) | legacy |
| `sync_roles_oracle` | create/update standard parliamentary roles (deduped, case/acronym normalised) | legacy |
| `sync_users_oracle` | import users & group memberships from Oracle via `pwms/membership/sync_service.py` | legacy |

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

## Notifications & deadlines

| Command | Purpose | Status |
| --- | --- | --- |
| `check_delegation_expirations` | detect expiring delegations/memberships | pending repoint |
| `check_referral_deadlines` | warn when a referral deadline is approaching | pending repoint |
| `notify_deadlines` | dispatch deadline notifications (optionally scoped by workflow id) | pending repoint |
| `send_overdue_alerts` | send overdue-workflow alerts / emails | pending repoint |
| `update_event_statuses` | recompute/refresh statuses of events or instances | pending repoint |

## Maintenance & inspection

| Command | Purpose | Status |
| --- | --- | --- |
| `fix_missing_group_access` | create `WorkflowGroupAccess` for instances missing it — **superseded by `sync_type_group_access`** | legacy |
| `sync_type_group_access` | backfill `WorkflowGroupAccess` (owning group + viewer groups) from each instance's `WorkflowType` | ✅ live |
| `show_workflow_hierarchy` | print group/workflow hierarchy trees | legacy |
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

## Roadmap hooks

New automation (email/notifications/SharePoint) will likely arrive as **new**
commands plus **background-task** jobs (`django-background-tasks` is already
installed). When you add one:

- name it for the job it does (do **not** call it `test.py` — that shadows
  Django’s built-in `test` command);
- prefer current imports from `pwms.models`;
- document it here.
