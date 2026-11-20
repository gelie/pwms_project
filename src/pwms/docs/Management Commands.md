# Management Commands

All commands live in `pwms/management/commands/` and run as:

```bash
.venv/bin/python manage.py <command> --help
```

> **Current status.** Every command in this package loads and runs against the
> current `pwms` models. The earlier `workflows` app is gone: the commands that
> targeted it were either repointed
> (`export_workflow_type`, `import_workflow_type`, `import_states`,
> `validate_memberships`) or retired (`update_event_statuses`,
> `check_delegation_expirations`, `notify_deadlines`, `send_overdue_alerts`).
> `fix_missing_group_access` remains only as a deprecated alias that delegates to
> `sync_type_group_access`.
>
> Workflow deadline / overdue email is **not** part of the current event-based
> notification model: overdue records remain visible on the dashboard, and
> referral deadlines are handled by `check_referral_deadlines`. Reintroducing
> proactive deadline mail would be a new `NotificationRule` event, not a repoint
> of the retired commands.

## Synchronisation (Oracle ERP)

| Command | Purpose | Status |
| --- | --- | --- |
| `sync_base` | shared base for the Oracle suite (`OracleSyncBase`) — a helper module, **not** an invokable command | helper |
| `sync_groups_oracle` | **retired** — the tree is curated by `seed_structure`; the command exits with guidance | ⛔ retired |
| `sync_roles_oracle` | create/update standard parliamentary roles (deduped, case/acronym normalised) | ✅ live |
| `sync_users_oracle` | import users, departments and memberships from Oracle, resolved against the seeded tree | ✅ live |

> **No parallel tree.** `sync_users_oracle` resolves each ERP
> `(CHILD_ORG_NAME, PARENT_ORG_NAME)` pair onto the curated tree (see [Group &
> organisation data](#group--organisation-data)) through
> `pwms/membership/resolver.py` and `group_aliases.csv`. The Oracle-built
> `Administration` tree is gone, and `sync_groups_oracle` no longer creates one.

> **Identity ownership.** `sync_users_oracle` owns only users whose
> `identity_source` is `erp`: locally managed users (`identity_source="local"`,
> e.g. Ministers appointed from outside the ERP) are skipped with a warning
> rather than overwritten, and memberships in executive groups (`executive` /
> `presidency` / `ministry`) are never ended by the sync — losing an ERP payroll
> record does not end a political appointment.

> **Reporting line.** `User.supervisor` is populated from the ERP
> `EMPLOYEEID` / `SUPERVISORID` chain: `EMPLOYEEID` is stored on
> `User.employee_id`, and a second pass — after every user has been upserted,
> since a report is often processed before its manager exists — links each user
> to the manager whose `EMPLOYEEID` matches their `SUPERVISORID`. The link is
> resolved from the users handled in the same run (falling back to the database
> for a manager synced on an earlier run), so a `--dry-run` previews the links
> even though it writes nothing. A manager
> absent from the feed, or whose id is not stored, leaves the line **untouched**
> rather than clearing it. Disable with `--skip-supervisors`.
>
> **Profile and memberships.** `User.positiondesc` is written from the ERP
> `POSITIONDESC`. `User.department` and the staff memberships (the resolved unit
> plus the level above it) come from the same resolver, so the profile and
> access cannot disagree. A pair that does not resolve leaves the existing
> department and memberships **untouched** and is listed at the end of the run.
> MPs get a membership in `NA Members` / `NCOP Members` and in their party group
> under `NA Parties` / `NCOP Parties`; a party change ends the previous
> membership instead of deleting it, so `User.party_affiliation` follows Oracle.
> Roles remain on `GroupMembership` — the sync resolves the post from
> `positiondesc` there, so `User` does not need a separate role foreign key.

## SharePoint (Graph) sync

| Command | Purpose | Status |
| --- | --- | --- |
| `populate_sites` | mirror SharePoint sites and drives into the local `Sharepoint*` tables | ✅ live — see [SharePoint Sync](./SharePoint%20Sync.md) |
| `reset_site_members` | bulk-delete the manually curated `SharepointSiteMember` rows | ✅ live |

Key options (`populate_sites`): none — it takes no arguments.

Key options (`reset_site_members`): `--site SELECTOR` (repeatable) narrows the
reset to sites matched case-insensitively on **site ID, name or URL**;
`--inactive-only` deletes just the inactive rows, so current memberships keep
working; `--dry-run` reports exact counts and writes nothing; `--noinput` skips
the confirmation prompt.

> Site **membership is not synced**. `populate_sites` only mirrors sites and
> drives; `SharepointSiteMember` rows are curated by hand in the admin (inline on
> the site page and a standalone **Sharepoint site members** list), because they
> govern who may browse a site in the attachment picker. `reset_site_members`
> is the bulk counterpart of that manual editing: it empties the table (all of
> it, one or more sites, or only inactive rows) so access can be rebuilt
> deliberately.

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
| `seed_structure` | create/update the curated Parliament tree (legislative per term + permanent administration) | ✅ live — see below |
| `seed_executive` | create/update `Government of RSA`, its Presidency, ministries, national departments, executive roles, and current office holders | ✅ live — see below |
| `audit_group_mapping` | report how Oracle org pairs resolve onto the seeded tree, and write the alias table | ✅ live — see below |
| `reset_groups` | empty `Group` / `GroupMembership` for a rebuild while keeping every other table (`--dry-run`, transactional) | ✅ live |
| `import_groups` | import group names from a CSV/text list as `Group` records under a parent group | ✅ live |
| `generate_group_tree` | render the group MPTT hierarchy as a Graphviz/Mermaid diagram, optionally rooted at one group | ✅ live — see below |
| `compare_org_hierarchy` | read-only diff of Oracle's reporting tree against the stored hierarchy | ⚠️ reference only — see below |

Key options: `--parent NAME_OR_SLUG` (required) is the group the names hang
under; `--type GROUP_TYPE` (required) must be one of `Group.Type.choices`
(e.g. `ministry`). The file is parsed as blank-line-separated sections — each
section's first line is a heading and the rest are names — and `--section NAME`
imports a single section only. Names are whitespace-normalised and matched on
the `(name, parent)` natural key, so re-running is safe. `--dry-run` previews the
changes.

### The seeded tree

`seed_structure` is the source of truth for the hierarchy. It is idempotent and
creates:

* the legislative tree, once per `ParliamentTerm` — Parliament → House →
  `Members` / `Parties` containers, committees and provincial delegations;
* the permanent administration tree — `Parliament Administration` → `Secretary
  to Parliament` → `Offices and Divisions`, `Core Business Branch` and `Support
  Services Branch`.

`--dry-run` runs the same seeding inside the transaction and rolls it back, so
the reported created/updated counts are exact. Identity is `external_id` for
committees (their parliament.gov.za id), `short_name` for the coded nodes, and
`(name, parent)` otherwise, so a rename or a re-parent updates the row instead
of duplicating it.

### The executive branch

`seed_executive` builds the executive tree and populates it from the dated
snapshot in `management/commands/data/executive.json`:

* the `Government of RSA` root (`executive`) with The Presidency (`presidency`)
  and one child per Cabinet ministry (`ministry`);
* one `department` child under its ministry for each national department and
  agency with department status — the source follows the gov.za national
  departments list, with the Wikipedia list supplying the split between the
  Departments of Cooperative Governance and Traditional Affairs and placing
  Statistics South Africa under The Presidency where the two sources differ.
  The Presidency itself is represented by the existing `presidency` group rather
  than a duplicate department row;
* the `President`, `Deputy President`, `Minister` and `Deputy Minister` roles —
  office labels with no workflow powers, because what an office holder may *do*
  is granted by the RBAC layer;
* one `GroupMembership` per current office holder, matched by the external
  Parliament/ERP username. The snapshot records its sources (Wikipedia,
  Parliament's Ministers and Deputy Ministers pages, the government contact
  directory and the national departments pages) and an `as_at` date, so a
  reshuffle is a data change. The same snapshot carries the national departments
  and their ministry mapping.

New accounts are created with `identity_source='local'` by default: an office
holder may not be on the Oracle ERP, and a locally managed identity is not
overwritten or deactivated by `sync_users_oracle`. Use
`--identity-source erp` where the ERP owns those identities. Existing users are
never renamed or overwritten; only their blank profile fields are filled.

The command is idempotent and `--dry-run` reports exact counts without writing.
`--prune` ends active appointments this command previously seeded that are no
longer in the snapshot, leaving manually created appointments untouched.

```bash
python manage.py seed_executive --dry-run
python manage.py seed_executive
python manage.py seed_executive --prune
```

### Mapping Oracle onto the seeded tree

Oracle's labels differ from the curated ones, so the ERP is mapped onto the tree
by an alias table instead of by name. `audit_group_mapping` fetches each
distinct `(CHILD_ORG_NAME, PARENT_ORG_NAME)` pair and reports how it resolves,
ordered by the number of active people behind it:

```bash
python manage.py audit_group_mapping --csv /tmp/org-map.csv
# fill seeded_path for the unresolved rows, then
python manage.py audit_group_mapping --pairs-csv /tmp/org-map.csv \
    --alias-file /tmp/org-map.csv
```

`seeded_path` must point inside the administration subtree; `/` or `>` separate
the levels, matching is case-insensitive, the `Parliament Administration` prefix
is optional, and a bare name is accepted when it is unique. Rows may be left
blank — they simply stay unresolved. Alias keys are the **raw** Oracle names
(case/whitespace-insensitive), so unrelated units whose names collapse to the
same leaf cannot share an alias.

The packaged table is `pwms/membership/group_aliases.csv`, which
`sync_users_oracle` reads by default; `--org-map PATH` overrides it, and `--term
N` scopes Houses to a `ParliamentTerm` (default: the term in force today).

> Deploying this to a server with existing data is a maintenance-window job:
> see the [Group Overhaul Checklist](./Group%20Overhaul%20Checklist.md) before
> running `reset_groups` or `seed_structure`.

### Comparing with Oracle's reporting tree

`compare_org_hierarchy` remains available as a read-only analysis: it derives
the containment the `EMPLOYEEID` / `SUPERVISORID` chain implies and diffs it
against the stored tree. Reporting lines are not org lines, so this is evidence,
not an instruction — and because the stored tree is curated, treat the diff as a
check on naming and coverage. There is no automatic apply step any more;
corrections belong in `seed_structure` or `group_aliases.csv`.

```bash
python manage.py compare_org_hierarchy --prefix IRP
```

| Option | Effect |
| --- | --- |
| `--prefix TEXT` | only units whose name starts with `TEXT` |
| `--all` | list every unit in scope, not just the differences |
| `--json` | machine-readable output |
| `--strict` | exit non-zero when any unit differs or is missing |
| `--verbose` | log each inferred edge |

### Group hierarchy diagrams

`Group` is an MPTT model, so the whole tree already lives in every row's
`tree_id` / `lft` / `rght` / `level` columns. `generate_group_tree` reads those
columns and renders the hierarchy with Graphviz — one node per group, coloured
by a family derived from `group_type`, under a title banner with a legend and a
provenance footer. By default it draws **every** root group and its descendants;
`--group` roots the drawing at a single group and renders only its subtree.

```bash
python manage.py generate_group_tree --group "IRPD"
python manage.py generate_group_tree --group-type committee --active-only
python manage.py generate_group_tree --group "IRPD" --format png --text
```

| Option | Effect |
| --- | --- |
| `--group NAME_OR_SLUG` | root the tree at one group; only its descendants are drawn |
| `--format {dot,png,svg,pdf}` | image format (default `svg`); PNG is rendered at 200 dpi |
| `--output-dir DIR` | where images are written (default `GROUP_TREE_OUTPUT_DIR`) |
| `--group-type TYPE` | only include these types; repeat for several |
| `--active-only` | drop groups with `is_active = False` |
| `--max-depth N` | stop `N` levels below each drawn root (`0` = roots only) |
| `--rankdir {TB,LR,BT,RL}` | graph direction (default `TB`) |
| `--show-members` | annotate each node with its distinct active member count |
| `--text` | also print an indented text tree to stdout |
| `--filename STEM` | override the output filename stem |
| `--mermaid-dir DIR` | where Mermaid `.md` is written (default `--output-dir`) |
| `--no-mermaid` | skip the Mermaid Markdown export |

Filtering re-parents around excluded nodes: a kept group whose ancestors are
filtered out is promoted to a root rather than dropped, so `--group-type section`
still shows every section even when its parent division is left out. The
rendered file is an operator/inspection artefact — nothing in the site serves it
yet.

## Committee scraping

| Command | Purpose | Status |
| --- | --- | --- |
| `scrape_parliament_committees` | scrape committee chairpersons & members from parliament.gov.za, import members | ✅ live |

## Workflow definitions & tooling

| Command | Purpose | Status |
| --- | --- | --- |
| `export_workflow_type` | export a `WorkflowType` (states, transitions and current RBAC/audience fields) to JSON | ✅ live — format 2 |
| `import_workflow_type` | upsert a `WorkflowType` from JSON; accepts format 1 and 2 | ✅ live |
| `import_states` | import workflow states from CSV for a workflow type, mapping each state's `public_status` | ✅ live |
| `generate_diagrams` | render workflow-type diagrams (states/transitions) | ✅ live |

### Definition backups and state CSVs

`export_workflow_type` writes a natural-key JSON backup (format 2) containing
the type's owning group, create roles, viewer groups, parent type, owner
capability policy (`owner_can_*`), oversight and notification settings; every
state's slug, name, `public_status`, flags, order and colour; and every
transition's states, allowed roles, required event types, comment rule and
order. `import_workflow_type` upserts from that file — referenced groups,
roles, public statuses, event types and parent types must already exist — and
still accepts a format-1 file, defaulting states without a public status to
`New` (or keeping the one an existing state already has) and ignoring the
obsolete `notify_roles_slugs`. Both directions support `--dry-run` on import.

`import_states` reads a CSV for one workflow type, upserting on
`(workflow type, name)`:

```csv
name,description,is_initial,is_terminal,allows_referrals,order,color,public_status
Draft,Being prepared,true,false,true,0,,New
```

`public_status` may be a `PublicStatus` slug or name; without it, the row uses
`--default-public-status` (default `New`). A blank colour gets the standard
colour for the state's role.

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

## Meetings

| Command | Purpose | Status |
| --- | --- | --- |
| `import_meetings` | bulk-import meetings (the Events workflow) from a CSV/XLSX file | ✅ live |
| `seed_meetings` | create demo meetings and attendance registers for walkthroughs | ✅ live — see below |

Bulk-imports meetings from the calendar's own import format — the same columns
and behaviour as the **Import** page on `/pwms/meetings/`, because it calls the
same function (`pwms.services.meetings_io.import_meetings`). Rows **upsert on**
`Reference`: an existing meeting with that reference is updated, otherwise one is
created. Blank cells leave fields untouched; a row that cannot be read is
reported and skipped while the rest still imports; **nothing is deleted or
emailed**. The full column reference is
[Meetings Import & Export](./Meetings%20Import%20%26%20Export.md).

```bash
python manage.py import_meetings meetings.csv
python manage.py import_meetings meetings.xlsx --dry-run
python manage.py import_meetings meetings.csv --actor jsmith
```

| Option | Effect |
| --- | --- |
| `--dry-run` | Report what would change without writing anything |
| `--actor USERNAME` | The user new meetings are owned by and attributed to (default: the first superuser) |

The command ends with a summary (created / updated / skipped / errors) and lists
every row it could not import, with its line number.

### `seed_meetings`

Creates ~100 presentation-ready meetings and their attendance registers: mostly
portfolio/select committee business, with House plenaries, joint sittings and a
smaller set of staff engagements, spread over the last ~18 months plus a few
upcoming sittings. Past meetings are walked through the real workflow machine, so
they carry genuine transition history; only the timestamps are back-dated.
Automatic alert fan-out is suppressed.

```bash
python manage.py seed_meetings
python manage.py seed_meetings --count 120 --seed 7
python manage.py seed_meetings --reset --force
```

| Option | Effect |
| --- | --- |
| `--count N` | How many meetings to create (default `100`) |
| `--seed N` | Random seed, so a demo database is reproducible (default `20261118`) |
| `--actor USERNAME` | Who the seeded meetings are owned by (default: a member of the Meeting workflow's owning group, else a superuser) |
| `--reset` | Delete every existing meeting first (asks for confirmation) |
| `--force` | Skip the `--reset` confirmation prompt |
| `--no-demo-users` | Never create fallback accounts; fail instead when there are too few accounts to build a register |

The command reuses the group tree (`seed_structure` must have run) and existing
accounts. `is_mp` / `is_staff_member` are not required: anyone with an active
committee/House/party membership is treated as a member, and as a final fallback
a small cast of `demo-member-*` / `demo-staff-*` accounts (with unusable
passwords) is created so a scratch database still produces a convincing
register; `--no-demo-users` disables that. Every member, staff and superuser
account is guaranteed at least one place on a register, so a per-user lookup in
the API always finds something. Additive by default — use `--reset` to replace a
previous batch (re-run it after upgrading to pick up the coverage pass).

## Notifications & deadlines

| Command | Purpose | Status |
| --- | --- | --- |
| `check_referral_deadlines` | warn when a referral deadline is approaching, and expire referrals past it | ✅ live — see below |
| `send_scheduled_report_shares` | email due scheduled report shares and advance their schedules | ✅ live — see below |

`check_delegation_expirations`, `notify_deadlines`, `send_overdue_alerts` and
`update_event_statuses` were retired: their models (`UserDelegation`, `Event`)
and `workflows.tasks` no longer exist. Overdue records are surfaced on the
dashboard, and referral deadlines are the live deadline workflow. Workflow
deadline/overdue mail would need a new notification event (see the note at the
top of this page).

### Referral deadline reminders

`check_referral_deadlines` dispatches reminders for open `WorkflowReferral` rows
and closes the ones nobody answered:

| Window | Action |
| --- | --- |
| due within 24 hours, not yet reminded | alert the referred group's active members (reason `referred-group`) and the people the record names (owner, assignee), plus any `referral-deadline` `NotificationRule` — in-app to everyone, email to staff of the referred group — then stamp `deadline_notified_at` |
| due within 1 hour, already reminded | send the final reminder (no second stamp) |
| due date passed | `mark_expired()` the referral (emits `referral-expired`) and alert the parties |

Only `status="open"` referrals are considered, so an answered or recalled one is
left alone. Reminders go through `pwms.notifications` and are logged as
`referral-deadline` alerts (an `in_app` row per recipient, plus an `email` row
for each staff recipient in the referred group); a deadline reminder is
**mandatory**, so a user's alert preferences never suppress it. The expiry alert is raised by
`WorkflowReferral.mark_expired()` and is not sent by the command. Mail is one
message per recipient, so no recipient sees another's address. `--dry-run`
reports what would be sent or expired without sending mail or writing anything.

### Scheduled report shares

`send_scheduled_report_shares` drains the shares a reader made repeat from the
reports page (daily, weekly or monthly). It emails each due share — the link,
plus the rendered document when the share names one — and moves its
`next_send_at` on by one interval. Each addressed recipient gets a
`report-share` row in the `Notification` log (marked `sent` or `failed`, with the
failure reason), so a scheduled send is as auditable as an immediate one.

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
| `fix_missing_group_access` | deprecated alias that delegates to `sync_type_group_access` | deprecated |
| `sync_type_group_access` | backfill `WorkflowGroupAccess` (owning group + viewer groups) from each instance's `WorkflowType` | ✅ live |
| `audit_workflow_access` | explain why a user can (or cannot) create or open a workflow type, and flag create roles no active member of the group holds | ✅ live — see below |
| `show_workflow_hierarchy` | print the parent/child tree of workflow instances | ✅ live — see below |
| `seed_demo_data` | empty the four workflow registers and reseed them with presentation-ready mock records | ✅ live — see below |
| `rotate_api_secret` | issue a new secret for a registered PWMS API client and print it once | ✅ live — see below |
| `validate_memberships` | integrity-check `GroupMembership`, group and user data | ✅ live — see below |

### PWMS API client secrets

Partner applications authenticate with a `client_id` / `client_secret` pair;
PWMS stores only a hash of the secret. A registration created in the admin gets
its first secret from the **Rotate client secret** action, or from:

```bash
python manage.py rotate_api_secret parking-system
```

The new secret is printed once. Distribute it through the partner's own secure
channel; it cannot be recovered from the database afterwards. Rotating a secret
does not invalidate existing 60-minute access tokens, but the old secret can no
longer mint new ones.

### Type-level group access

Configuring a `WorkflowType` — its owning `group`, its `viewer_groups` and the
`owner_can_*` capabilities it grants that group — materialises
`WorkflowGroupAccess` rows on **new** instances only
(`AbstractLegislativeWorkflow.materialize_group_access()`).
`sync_type_group_access` applies the current configuration to instances that
predate it: it creates missing rows (the owning group flagged `is_primary` with the
type's capabilities, each viewer group read-only) and, with `--update-existing`,
also re-applies the type's capability policy to the owning group's existing row.
Without that flag it never overwrites an existing row, so it is safe to re-run — a
grant an administrator has narrowed stays narrowed (migrations `0037`, `0040` and
`0041` are how the policy itself arrived: edit, then transition, then the type
fields that state it). `--update-existing` touches only the owning group's own
flags: never the per-role `WorkflowRolePermission` or per-state
`WorkflowStatePermission` children, and never a viewer group's row. Use `--dry-run`
to preview either behaviour.

| Option | Effect |
| --- | --- |
| `--workflow-type NAME_OR_SLUG` | restrict the backfill to one workflow type |
| `--update-existing` | also re-apply the type's `owner_can_*` policy to the owning group's existing row, instead of only creating missing rows |
| `--dry-run` | report what would be created or changed without writing anything |

### Membership validation

`validate_memberships` checks the current `GroupMembership`, `Group` and `User`
rows rather than the removed `workflows.membership` validators. It reports
memberships whose end date has passed while still active, inactive rows with no
end date, an end before the start, active rows that start in the future, active
rows on an inactive group or user, several active rows for the same
user/group/role, non-container groups with no active members, and active users
with no membership. Errors fail the command; warnings do not unless `--strict`
is given.

| Option | Effect |
| --- | --- |
| `--report-file PATH` | write the full report as JSON |
| `--fix-duplicates` | keep the newest active row in each duplicated user/group/role set and end the older ones (setting `end_date` to today) |
| `--strict` | treat warnings as a failed validation too |

```bash
python manage.py validate_memberships --report-file=/tmp/memberships.json
```

### Access diagnostics

`audit_workflow_access` explains the three gates that decide access to a
`WorkflowType` in one report: whether the type is **enabled** and has an **owning
group**; whether the user holds an **active** `GroupMembership` in that *exact*
group with one of its `create_roles` (`WorkflowType.can_create`); and whether the
user's group has a `WorkflowGroupAccess` row on the **instance**
(`AbstractLegislativeWorkflow.can`). It is **read-only**.

A `create_role` grants only the second gate, and only to people who hold that role
in the type's own group — roles are global records, so a role that "belongs to the
group" (what the admin form checks) is not the same as *this user* holding it
there. The command flags the two configurations that silently deny everyone: an
enabled type with no owning group, and a `create_role` that no active member of
the group holds (reporting whether other groups hold it instead).

| Option | Effect |
| --- | --- |
| `--user USERNAME` | diagnose one user: `can_create` per type with the reason, the groups they are active in, and whether their group sees the existing records |
| `--workflow-type NAME_OR_SLUG` | audit one workflow type |
| `--json` | emit the report as JSON |
| `--strict` | exit non-zero on warnings as well as errors |

Exit status is non-zero when structural errors are found, so it can gate a
deployment or a pre-flight check; `--strict` widens that to warnings (a user who
cannot create a type that declares create roles, or who can create but cannot see
its existing records). `sync_type_group_access` is the repair for the third gate.

```bash
python manage.py audit_workflow_access --user sbrown
```

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

### Acceptance-test workbook

`build_uat_workbook` builds the results workbook the section fills in when it
accepts the system — **from the printable UAT form, not from a second handwritten
list**. `src/pwms/docs/UAT Form.html` is the single source of truth for the
scenarios: the command reads its scenario tables, severity scale, result key,
expected-behaviour list and acceptance criteria, and writes `UAT Form.xlsx`
beside it. The scenario rows are never edited in the workbook; change the form
and re-run the command.

The workbook is laid out for a section rather than one person. `Results` is a
matrix — one row per scenario, one column per tester (eight of them) — so several
testers work in the same file, and each row carries *Failed by*, *Blocked by*,
*Tested* and a triage *Status* as live formulas. `Summary` counts that matrix and
the defect log, and states whether the form's own acceptance criteria (§15) are
met, so the section can see whether it is ready to sign without counting by hand.

| Option | Effect |
| --- | --- |
| `--form PATH` | the form to read (default: `<docs>/UAT Form.html`) |
| `--output PATH` | the workbook to write (default: `<docs>/UAT Form.xlsx`) |
| `--docs-dir PATH` | the directory holding the form, and receiving the workbook |

```bash
python manage.py build_uat_workbook
```

## Background tasks (the queue worker)

Two jobs run through `django-background-tasks` rather than inline, so
**`manage.py process_tasks` has to be running** for them to happen:

| Task | Queued by | What it does |
| --- | --- | --- |
| `pwms.tasks.deliver_notification_email` | `pwms.notifications.dispatch.queue_email()`, inside the transaction that wrote the alert row | delivers one alert email, retrying a transient failure (`MAX_ATTEMPTS` + backoff) |
| `pwms.tasks.deliver_scheduled_report_shares` | `send_scheduled_report_shares --queue`, once | repeating drain for scheduled report shares |

Explicit report and record shares send **inline** rather than through the queue
(the person who clicked *Send* sees the outcome immediately), but each addressed
recipient still gets a row in the `Notification` outbound-email log.

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
