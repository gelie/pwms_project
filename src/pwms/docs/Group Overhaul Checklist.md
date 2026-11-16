# Group Overhaul Checklist

Moving an **existing** PWMS server onto the curated group tree (`seed_structure`)
and the resolver-backed Oracle user sync.

This is a maintenance-window runbook, not a zero-downtime migration. It is
written for the operator who will run it, with the queries needed to prove each
step worked. Read Phase 0–2 completely before touching the server.

Commands are shown as `python manage.py …`; substitute your environment's
interpreter (`.venv/bin/python`, or `uv run python`). Run everything from the
project root so `.env` (database and Oracle credentials) is picked up.

---

## What changes

| Before | After |
| --- | --- |
| Group tree built from Oracle by `sync_groups_oracle` (flat under `Administration`) | Curated tree created by `seed_structure` (`Parliament Administration` → `Secretary to Parliament` → branches/divisions/units, and the per-term legislative tree) |
| Department and memberships looked up by stripped Oracle name | Resolved by `pwms/membership/resolver.py` through `pwms/membership/group_aliases.csv` |
| `sync_users_oracle --seed-groups` opt-in | Seeded tree is the **only** mode; the flag is gone |
| `sync_groups_oracle` rebuilds groups | **Retired**; the command exits with guidance |
| `User.party_affiliation` column | Dropped by migration `0063`; party is derived from the active `party` membership |
| Staff memberships = parent + child Oracle groups | Staff memberships = resolved unit + the level above it (division/office) |

New commands used below: `seed_structure`, `audit_group_mapping`,
`reset_groups`. New migrations: `0060`–`0063`.

> **There is no audit trail for this.** `Group` and `GroupMembership` are not
> registered with `auditlog`, so the database backup is the only undo.

> **Not automated.** Committee membership scraping, local users' party and
> department, manual `WorkflowGroupAccess` overrides, and referral history are
> not reconstructed by `seed_structure` or `sync_users_oracle`. Phase 1 exports
> what exists; Phases 5 and 7 restore what you still need.

---

## Phase 0 — Pre-flight

- [ ] Schedule a maintenance window; stop application writes (and the scheduled
      `sync_users_oracle` job) for the duration.
- [ ] Confirm the deployment artefact contains this commit, including
      `pwms/membership/resolver.py` and `pwms/membership/group_aliases.csv`.
- [ ] Take a database backup and verify it restores to a scratch database:

  ```bash
  pg_dump --format=custom --file=pwms-before-overhaul.dump "$DATABASE_URL"
  pg_restore --list pwms-before-overhaul.dump >/dev/null && echo BACKUP_OK
  ```

- [ ] If `populate_sites`, attachments or anything else is running, stop it too;
      the window is about the group tree, not just users.
- [ ] Confirm Oracle connectivity from the target host (`audit_group_mapping`
      and `sync_users_oracle` need it; `seed_structure` does not):

  ```bash
  python manage.py audit_group_mapping --limit 1
  ```

- [ ] Check out the new code in a separate release directory and run
      `python manage.py check` there; keep the running deployment on the old
      revision until Phase 1 is exported.

---

## Phase 1 — Export what the overhaul cannot reconstruct

Run this **on the current (old) deployment, before deploying the new code and
before `migrate`**: migration `0063` drops `User.party_affiliation`, and
`reset_groups` deletes every membership (including executive appointments) and
every referral.

If the new code is already deployed but migrations have not run, `User` no
longer declares `party_affiliation`; export it with raw SQL instead:

```python
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute(
        "SELECT username, party_affiliation, identity_source, employee_id "
        "FROM pwms_user WHERE party_affiliation <> ''"
    )
    rows = cursor.fetchall()
```

Save the script below as `/tmp/export_overhaul_snapshot.py` and run:

```bash
mkdir -p /var/backups/pwms-overhaul
python manage.py shell < /tmp/export_overhaul_snapshot.py
```

```python
# /tmp/export_overhaul_snapshot.py — run on the old deployment.
import csv
from pathlib import Path

from pwms.models import (
    Bill,
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Meeting,
    User,
    WorkflowReferral,
    WorkflowType,
)

out = Path("/var/backups/pwms-overhaul")
out.mkdir(parents=True, exist_ok=True)


def write_csv(name, header, rows):
    with (out / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


# 1. Legacy party values (only while the column still exists).
field_names = {field.name for field in User._meta.get_fields()}
if "party_affiliation" in field_names:
    write_csv(
        "legacy-party.csv",
        ["username", "party_affiliation", "identity_source", "employee_id"],
        User.objects.exclude(party_affiliation="").values_list(
            "username", "party_affiliation", "identity_source", "employee_id"
        ),
    )

# 2. Executive appointments — reset_groups deletes these too.
write_csv(
    "executive-memberships.csv",
    ["username", "identity_source", "employee_id", "group", "group_type",
     "role", "start_date", "end_date", "is_active"],
    GroupMembership.objects.filter(
        group__group_type__in=["executive", "presidency", "ministry"]
    ).values_list(
        "user__username", "user__identity_source", "user__employee_id",
        "group__name", "group__group_type", "role__name",
        "start_date", "end_date", "is_active",
    ),
)

# 3. Every membership, for reconciliation after the sync.
write_csv(
    "memberships.csv",
    ["username", "group", "group_type", "role", "start_date", "end_date",
     "is_active"],
    GroupMembership.objects.select_related("user", "group", "role").values_list(
        "user__username", "group__name", "group__group_type", "role__name",
        "start_date", "end_date", "is_active",
    ),
)

# 4. Group inventory — the map for re-pointing workflow types and records.
write_csv(
    "groups.csv",
    ["name", "group_type", "parent", "external_id", "is_active"],
    Group.objects.values_list(
        "name", "group_type", "parent__name", "external_id", "is_active"
    ),
)

# 5. Workflow type ownership and viewers — reset_groups sets group to NULL.
write_csv(
    "workflow-types.csv",
    ["workflow_type", "owning_group", "viewer_groups"],
    [
        (
            workflow_type.name,
            workflow_type.group.name if workflow_type.group_id else "",
            ";".join(workflow_type.viewer_groups.values_list("name", flat=True)),
        )
        for workflow_type in WorkflowType.objects.select_related("group")
    ],
)

# 6. Referrals — reset_groups deletes every WorkflowReferral row.
write_csv(
    "referrals.csv",
    ["content_type", "object_id", "referred_to", "referred_by", "due_date",
     "status", "notes"],
    WorkflowReferral.objects.select_related("content_type", "referred_to").values_list(
        "content_type__model", "object_id", "referred_to__name",
        "referred_by__username", "due_date", "status", "notes",
    ),
)

# 7. Workflow records whose group FK the reset will blank.
write_csv(
    "bill-committees.csv",
    ["bill_id", "reference", "responsible_committee"],
    Bill.objects.exclude(responsible_committee=None).values_list(
        "pk", "bill_number", "responsible_committee__name"
    ),
)
write_csv(
    "meeting-host-groups.csv",
    ["meeting_id", "title", "host_group"],
    Meeting.objects.exclude(host_group=None).values_list(
        "pk", "title", "host_group__name"
    ),
)
write_csv(
    "resolution-groups.csv",
    ["resolution_id", "resolution_number", "responsible_group"],
    InternationalResolution.objects.exclude(responsible_group=None).values_list(
        "pk", "resolution_number", "responsible_group__name"
    ),
)

# 8. Committee M2M links on agreements.
write_csv(
    "agreement-committees.csv",
    ["agreement_id", "committee"],
    [
        (agreement.pk, group.name)
        for agreement in InternationalAgreement.objects.prefetch_related(
            "referral_committees"
        )
        for group in agreement.referral_committees.all()
    ],
)

print(f"Snapshot written to {out}")
```

- [ ] Check `legacy-party.csv` covers every **locally managed** user who has a
      party. ERP users will be re-derived by the sync; local users will not.
- [ ] Check `executive-memberships.csv` is non-empty if there are Ministers or
      Deputy Ministers. `reset_groups` does **not** preserve executive rows.
- [ ] Keep the snapshot until the overhaul is signed off.

---

## Phase 2 — Deploy code and migrate

- [ ] Stop the app workers.
- [ ] Deploy the new code.
- [ ] Review the plan before applying:

  ```bash
  python manage.py migrate --plan
  ```

  Expect `0060`–`0063` (plus anything else already pending). `0060` folds legacy
  committee group types into `committee` before the column narrows to 20
  characters; `0063` drops `User.party_affiliation`.

- [ ] Apply:

  ```bash
  python manage.py migrate
  ```

- [ ] If any group row still holds a legacy type after migrating, stop and
      investigate — do not proceed to Phase 3 with a mixed vocabulary:

  ```bash
  python manage.py shell -c "from pwms.models import Group; from collections import Counter; print(Counter(Group.objects.values_list('group_type', flat=True)))"
  ```

- [ ] Ignore `sync_groups_oracle` from here on. It is retired and exits with a
      message pointing at `seed_structure`.

---

## Phase 3 — Reset and seed the tree

Choose **one** path:

### A. The server still has the old Oracle-built tree

`seed_structure` is additive and matches by identity, so the old groups would
remain as a second tree. Reset first.

- [ ] Preview the reset (it runs in a transaction and rolls back):

  ```bash
  python manage.py reset_groups --dry-run
  ```

  Confirm the reported `WorkflowReferral` deletions, `WorkflowType.group`
  unlinks and cascade-deleted `WorkflowGroupAccess` grants are acceptable — you
  reconnect them in Phase 7.

- [ ] Run it:

  ```bash
  python manage.py reset_groups --noinput
  ```

- [ ] If groups were already seeded and only memberships have drifted, use
      `reset_groups --memberships-only` instead; it leaves groups and every FK
      alone.

### B. The server already has a seeded tree

- [ ] Do **not** reset. `seed_structure` is idempotent: it updates by
      `external_id` / `short_name` / `(name, parent)`.

### Seed

- [ ] Preview:

  ```bash
  python manage.py seed_structure --dry-run
  python manage.py seed_structure
  ```

  The command creates **term 7** (`7th Parliament`, dates hard-coded in the
  command). If this server is on a different term, adjust `seed_structure.py`
  first or create the term separately — the user sync scopes Houses by
  `--term` / the active term.

- [ ] Confirm exactly two roots:

  ```bash
  python manage.py shell -c "from pwms.models import Group; print(list(Group.objects.filter(parent__isnull=True).values_list('name','group_type')))"
  ```

  Expected: `7th Parliament (parliament)` and
  `Parliament Administration (administration)`. Anything else is a stray tree.

---

## Phase 4 — Build and verify the alias table for this server

The packaged `pwms/membership/group_aliases.csv` was built from one ERP extract.
Another server may see different cost centres, spellings or new units.

- [ ] Export this server's pairs (needs Oracle). Use a per-server filename if
      you keep several extracts around; `/tmp/org-map.csv` is used below:

  ```bash
  python manage.py audit_group_mapping --csv /tmp/org-map.csv
  ```

- [ ] Diff against the packaged file:

  ```bash
  python - <<'PY'
  import csv
  packaged = {r["oracle_child_org"] for r in csv.DictReader(open("src/pwms/membership/group_aliases.csv", encoding="utf-8"))}
  target = {r["oracle_child_org"] for r in csv.DictReader(open("/tmp/org-map.csv", encoding="utf-8"))}
  print("new on this server:", sorted(target - packaged))
  print("not seen here:", len(packaged - target))
  PY
  ```

- [ ] Fill `seeded_path` for the new unresolved rows. Valid targets are inside
      `Parliament Administration`; use `/` or `>`, the root prefix is optional,
      and a bare name works when unique. Use this to list candidates:

  ```bash
  python manage.py shell -c "from pwms.models import Group; root=Group.objects.get(name='Parliament Administration', parent__isnull=True); print('\n'.join(g.get_full_path().replace(' > ','/') for g in root.get_descendants(include_self=True)))"
  ```

- [ ] Verify offline (no Oracle needed):

  ```bash
  python manage.py audit_group_mapping --pairs-csv /tmp/org-map.csv \
      --alias-file /tmp/org-map.csv
  ```

  Expect the two MP `Members` pairs to remain unresolved — that is correct; MPs
  get their House membership from `POSITIONDESC`, not an org alias. Do not gate
  the deployment on `--fail-on-unresolved` while those two rows exist.

- [ ] Decide where the table lives:
  - same pairs as packaged → ship nothing extra;
  - server-specific additions → keep a file on the host and pass
    `--org-map /etc/pwms/group_aliases-<server>.csv` to the sync, **or** merge
    the rows into the packaged file and redeploy.

- [ ] Record the chosen path in the server runbook / systemd unit.

---

## Phase 5 — Committees (only if this server uses the scraper)

`seed_structure` already creates the committees with their
parliament.gov.za `external_id`, so do **not** run `import_groups` for them.

- [ ] If committee membership data is used, run the scraper after seeding:

  ```bash
  python manage.py scrape_parliament_committees --dry-run
  python manage.py scrape_parliament_committees
  ```

  It matches committees **by name** (`name__iexact`), so seeded committees are
  reused. Verify no duplicates were created:

  ```bash
  python manage.py shell -c "from django.db.models import Count; from pwms.models import Group; print(list(Group.objects.filter(group_type='committee').values('name').annotate(n=Count('id')).filter(n__gt=1)))"
  ```

- [ ] The scraper looks for a root group literally named `Parliament` for joint
      committees, while the seed nests `Joint Committees` under the term. After
      a scraper run, check for a stray root `Joint Committees` and re-parent it
      under the term (or delete it if nothing references it).

---

## Phase 6 — Sync roles and users

- [ ] Roles (optional — the membership service creates `Member of Parliament`
      and `Staff Member` on demand, but the standard post roles come from here):

  ```bash
  python manage.py sync_roles_oracle
  ```

- [ ] Dry-run the sync and read the report:

  ```bash
  python manage.py sync_users_oracle --dry-run
  ```

  For reference, a full 7th-Parliament extract resolves roughly 1,250 staff and
  leaves ~376 MPs unresolved (by design). The membership projection also omits
  MP party memberships on a dry run, because the party groups do not exist yet;
  the real run adds them.

- [ ] First real run — consider `--skip-deactivation` if the Oracle feed on this
      server is known to be partial:

  ```bash
  python manage.py sync_users_oracle --skip-deactivation
  ```

  Flags: `--org-map PATH` (Phase 4), `--term N` (scope Houses),
  `--skip-supervisors`, `--dry-run`.

- [ ] The run must end with **no errors**. Unresolved org pairs are expected and
      listed at the end; each one leaves the existing department/membership
      untouched.

- [ ] Locally managed users (`identity_source != "erp"`) were skipped by the
      sync. If the reset ran, restore their `department` and memberships in
      Phase 7.

---

## Phase 7 — Reconnect what the reset cleared

Work top to bottom; later steps depend on the tree being seeded.

- [ ] **Workflow types.** Re-point each type's owning group and viewer groups to
      the seeded equivalents (use `workflow-types.csv` and `groups.csv`). Until
      this is done `WorkflowType.can_create` closes creation to superusers.

  ```bash
  python manage.py shell
  # >>> from pwms.models import WorkflowType, Group
  # >>> wt = WorkflowType.objects.get(name="International Agreement")
  # >>> wt.group = Group.objects.get(name="International Relations & Protocol Division", parent__name="Core Business Branch")
  # >>> wt.save(update_fields=["group"])
  ```

- [ ] Rebuild the grants the cascade deleted:

  ```bash
  python manage.py sync_type_group_access --dry-run
  python manage.py sync_type_group_access
  ```

  Use `--update-existing` only if the type's capability policy changed and an
  existing grant should be re-applied.

- [ ] **Bills** — restore `responsible_committee` from `bill-committees.csv` by
      matching the old committee name to the seeded committee (prefer
      `external_id`/name over name alone; committee names repeat in the seed).
- [ ] **Meetings** — restore `host_group` from `meeting-host-groups.csv`.
- [ ] **International resolutions** — restore `responsible_group` from
      `resolution-groups.csv`.
- [ ] **Referrals** — recreate from `referrals.csv` if the referral history is
      still needed. They are deleted, not archived, by the reset.
- [ ] **International agreements** — restore the `referral_committees` M2M from
      `agreement-committees.csv`.
- [ ] **Executive appointments** — recreate from
      `executive-memberships.csv` with the same role (`Minister` /
      `Deputy Minister`) and dates. Do this before the next sync so the
      deactivation pass sees them; the sync never ends executive memberships,
      but it will not recreate deleted ones.
- [ ] **Local users' party** — for every row in `legacy-party.csv` whose user is
      `identity_source != "erp"`, create the party group under the House's
      `Parties` container and an active `party` membership. The ERP sync cannot
      do this. Example:

  ```bash
  python manage.py shell <<'PY'
  from pwms.models import Group, GroupMembership, Role, User

  party_role, _ = Role.objects.get_or_create(name="Member of Parliament")
  parties = Group.objects.get(name="NA Parties", parent__name="National Assembly")
  user = User.objects.get(username="a.minister")
  party, _ = Group.objects.get_or_create(
      name="ANC", parent=parties,
      defaults={"group_type": "party", "short_name": "ANC"},
  )
  GroupMembership.objects.get_or_create(
      user=user, group=party, role=party_role,
      defaults={"is_active": True},
  )
  print(user.party_affiliation)
  PY
  ```

- [ ] **Local users' department** — set manually if they had one; the sync
      skips these identities entirely.

---

## Phase 8 — Validate

Run these and keep the output with the sign-off.

```bash
python manage.py shell <<'PY'
from collections import Counter
from django.db.models import Count, Q
from pwms.models import (
    Bill, Group, GroupMembership, InternationalResolution, Meeting,
    User, WorkflowType,
)

active = GroupMembership.objects.filter(is_active=True)
print("roots:", list(Group.objects.filter(parent__isnull=True).values_list("name", "group_type")))
print("groups by type:", Counter(Group.objects.values_list("group_type", flat=True)))
print("memberships by type:", Counter(active.values_list("group__group_type", flat=True)))
print("users with department:", User.objects.exclude(department=None).count())
print("users with department outside admin:",
      User.objects.exclude(department=None)
      .exclude(department__group_type__in=["administration", "branch", "division",
                                           "office", "section", "unit"]).count())
print("staff without active membership:",
      User.objects.filter(employee_type="staff", is_active=True)
      .exclude(id__in=active.values_list("user_id", flat=True)).count())
print("MPs without a members-group membership:",
      User.objects.filter(is_mp=True, is_active=True)
      .exclude(id__in=active.filter(group__group_type="members")
               .values_list("user_id", flat=True)).count())
print("MPs without a party membership:",
      User.objects.filter(is_mp=True, is_active=True)
      .exclude(id__in=active.filter(group__group_type="party")
               .values_list("user_id", flat=True)).count())
print("executive memberships:",
      active.filter(group__group_type__in=Group.EXECUTIVE_GROUP_TYPES).count())
print("workflow types without a group:",
      WorkflowType.objects.filter(group=None).count())
print("bills without a committee:", Bill.objects.filter(responsible_committee=None).count())
print("meetings without a host group:", Meeting.objects.filter(host_group=None).count())
print("resolutions without a responsible group:",
      InternationalResolution.objects.filter(responsible_group=None).count())
PY
```

Acceptance for a 7th-Parliament server, modulo local variations:

| Check | Expected |
| --- | --- |
| roots | exactly `<term> Parliament` + `Parliament Administration` |
| `groups by type` | no `portfolio_committee` / `select_committee` / `business_unit` / `legislature` |
| users with department outside admin | `0` |
| staff without membership | only rows Oracle carries no organisation for (normally 0–1) |
| MPs without members membership | `0` |
| MPs without party membership | only MPs with a blank Oracle `PARTYCODE` |
| executive memberships | equal to the restored count from Phase 7 |
| workflow types without a group | `0` |
| bills / meetings / resolutions without a group | `0` unless they were already blank before the overhaul |

Also spot-check two users:

```bash
python manage.py shell -c "from pwms.models import User; u=User.objects.filter(department__name='Household Services').first(); print(u.username, sorted(u.memberships.filter(is_active=True).values_list('group__name','group__group_type')))"
python manage.py shell -c "from pwms.models import User; u=User.objects.filter(is_mp=True, is_active=True).first(); print(u.username, repr(u.party_affiliation), sorted(u.memberships.filter(is_active=True).values_list('group__name','group__group_type')))"
```

---

## Phase 9 — Cut over

- [ ] Every scheduled invocation of `sync_users_oracle` must **drop
      `--seed-groups`**; leaving it in fails with `unrecognized arguments`.
- [ ] `sync_groups_oracle` must be removed from any cron/systemd unit; it is
      retired and exits with guidance.
- [ ] Start the app workers and the scheduled sync.
- [ ] Watch the first real sync report: `Departments resolved / unresolved`,
      `New memberships`, and `WARNINGS`.
- [ ] Commit the final `group_aliases.csv` (or the server-specific
      `--org-map` file) and note its path in the server documentation.
- [ ] Keep the maintenance-window snapshot until after the first successful
      scheduled sync.

---

## Rollback

The tree and memberships can be re-seeded, but these cannot be reconstructed by
a command:

- `User.party_affiliation` (dropped by `0063`),
- the exact `WorkflowGroupAccess` rows and any manual overrides,
- deleted `WorkflowReferral` rows,
- executive appointments (unless you exported them in Phase 1).

If the deployment must be reverted, restore the database backup and redeploy the
previous code. Do not rely on reversing `0063` for the party values.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `No 'Parliament Administration' group found` | Tree not seeded on this server | Phase 3 |
| `The seeded tree is incomplete: missing Secretary to Parliament` | Partial/old seed | Re-run `seed_structure`; check the term |
| `unrecognized arguments: --seed-groups` | Old cron/script | Remove the flag (Phase 9) |
| `sync_groups_oracle is retired…` | You ran the retired command | Expected; use `seed_structure` |
| `value too long for type character varying(20)` during migrate | Legacy group type not remapped | Stop; check `0060` completed; fix the row, then migrate |
| `Departments resolved: 0` | Alias file missing/empty, or `seeded_path` outside the admin subtree | Phase 4 |
| Departments resolve, memberships are `0` | Same cause; the service uses the same resolver | Phase 4 |
| Staff have 1 membership instead of 2 | Resolved group is a division/office (no unit beneath) | Expected |
| Duplicate committees after scraper | Scraper matches by name; names differ from the seed | Merge/re-point, or extend the alias/import |
| Stray root `Joint Committees` | Scraper's `Parliament` root assumption | Re-parent under the term |
| Workflow creation closes to superusers | `WorkflowType.group` still `NULL` | Phase 7 + `sync_type_group_access` |
| MP `party_affiliation` blank | No active `party` membership | Next sync creates it; local users restore manually |
| `--fail-on-unresolved` exits non-zero with two `Members` pairs | Expected; MPs are resolved from `POSITIONDESC` | Do not gate on it, or alias them out of the check |

---

## Sign-off

| Item | Value |
| --- | --- |
| Date / window | |
| Deployed commit | |
| Database backup file | |
| Snapshot directory | |
| Alias file used (`--org-map`) | |
| Departments resolved / unresolved | |
| Memberships created / ended | |
| Executive memberships restored | |
| Workflow types re-pointed | |
| Operator | |
| Reviewer | |
