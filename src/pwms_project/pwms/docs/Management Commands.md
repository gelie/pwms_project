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

## Synchronisation (Oracle / legacy source)

| Command | Purpose | Status |
| --- | --- | --- |
| `sync_base` | shared base/helpers for the Oracle sync suite | legacy |
| `sync_groups_oracle` | build the Parliament group hierarchy from Oracle | legacy |
| `sync_roles_oracle` | create/update standard parliamentary roles | legacy |
| `sync_users_oracle` | import users & memberships from Oracle | legacy |
| `sync_users_oracle_modern` | modernised variant of the user sync | legacy |
| `sync_users_oracle-14Mar.py`, `sync_users_oracle.py.backup` | dated/backup variants | legacy, not maintained |

## Committee scraping

| Command | Purpose | Status |
| --- | --- | --- |
| `scrape_parliament_committees` | scrape committee chairpersons & members from parliament.gov.za, import members | legacy |
| `scrape_parliament_committees-14Mar.py` | dated variant | legacy |

## Workflow definitions & tooling

| Command | Purpose | Status |
| --- | --- | --- |
| `export_workflow_type` | export a `WorkflowType` (states + transitions) to JSON | legacy |
| `import_workflow_type` | upsert a `WorkflowType` from a JSON backup | legacy |
| `import_states` | import workflow states from CSV for a workflow type | legacy |
| `generate_diagrams` | render workflow-type diagrams (states/transitions) | legacy |
| `generate_workflow_diagrams` | render workflow-type diagrams (graphviz) | legacy |
| `generate_workflow_diagrams_legacy` | earlier copy of the diagram generator (renamed from `test.py` so it no longer shadows Django’s built-in `test`) | legacy |

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
| `populate_sites` | seed `django.contrib.sites` entries | check before use |
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
