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
| `fix_missing_group_access` | create `WorkflowGroupAccess` for instances missing it | legacy |
| `show_workflow_hierarchy` | print group/workflow hierarchy trees | legacy |
| `validate_memberships` | integrity-check group memberships | pending repoint |

## Roadmap hooks

New automation (email/notifications/SharePoint) will likely arrive as **new**
commands plus **background-task** jobs (`django-background-tasks` is already
installed). When you add one:

- name it for the job it does (do **not** call it `test.py` — that shadows
  Django’s built-in `test` command);
- prefer current imports from `pwms.models`;
- document it here.
