"""Seed the executive branch and its current office holders.

The command builds the ``Government of RSA`` root, the Cabinet ministries and
The Presidency, the national departments under their ministries, the executive
offices (President, Deputy President, Minister, Deputy Minister), and the
corresponding appointments. It is idempotent and matched by natural key, so it
can be re-run after a reshuffle.

The office holders are **external identities**: the ``username`` of each entry
is the person's Parliament / ERP account name, not a PWMS-invented login. Where
Parliament publishes an e-mail address on the member's profile
(``parliament.gov.za/person-details/<id>``), the username is its local part; the
remainder follow the same first-initial-plus-surname convention the Parliament
scraper uses. New rows are created as ``identity_source='local'`` by default so
an office holder who is not in the Oracle ERP is not deactivated by
``sync_users_oracle``; pass ``--identity-source erp`` on an environment where
the ERP owns those identities.

The composition itself lives in ``data/executive.json``. It is a dated snapshot
(``as_at``) with its sources, so a reshuffle or machinery-of-government change
is a data change rather than a code change and the provenance travels with the
rows. The department list follows the gov.za national departments page, with the
Wikipedia list supplying the split between the Departments of Cooperative
Governance and Traditional Affairs and placing Statistics South Africa under
The Presidency where the two sources differ. The Presidency itself is
represented by the existing ``presidency`` group rather than a duplicate
department row. Re-run with ``--prune`` to end appointments this command
previously made that are no longer in the snapshot.

Usage::

    python manage.py seed_executive
    python manage.py seed_executive --dry-run
    python manage.py seed_executive --prune
    python manage.py seed_executive --data path/to/executive.json
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pwms.models import Group, GroupMembership, Role, User

DEFAULT_SNAPSHOT = Path(__file__).resolve().parent / "data" / "executive.json"

#: Root of the executive branch.
GOVERNMENT_ROOT_NAME = "Government of RSA"
GOVERNMENT_ROOT_TYPE = Group.Type.EXECUTIVE

#: The Presidency is executive but not a ministry.
PRESIDENCY_NAME = "The Presidency"
PRESIDENCY_TYPE = Group.Type.PRESIDENCY

#: Executive offices, with the descriptions used by the role sync.
EXECUTIVE_ROLES = {
    "President": "Head of State and head of the national executive",
    "Deputy President": "Deputy to the President and leader of government business",
    "Minister": "Member of the Executive responsible for a national portfolio",
    "Deputy Minister": "Member of the Executive deputising for a Minister",
}

#: Marks memberships this command owns, so ``--prune`` never ends a manual
#: appointment and a re-run never overwrites an administrator's notes.
SEED_NOTE_PREFIX = "Seeded by seed_executive"


def _load_snapshot(path: Path) -> dict:
    """Read and validate the dated executive snapshot."""
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CommandError(f"Snapshot not found: {path}") from error
    except json.JSONDecodeError as error:
        raise CommandError(f"Snapshot is not valid JSON ({path}): {error}") from error

    if not isinstance(snapshot, dict):
        raise CommandError("Snapshot must be a JSON object.")
    for key in (
        "as_at",
        "president",
        "deputy_president",
        "ministers",
        "deputy_ministers",
        "departments",
    ):
        if key not in snapshot:
            raise CommandError(f"Snapshot is missing the {key!r} key.")
    try:
        snapshot["_as_at"] = date.fromisoformat(snapshot["as_at"])
    except (TypeError, ValueError) as error:
        raise CommandError(
            "Snapshot 'as_at' must be an ISO date (YYYY-MM-DD)."
        ) from error

    seen = set()
    for section in ("president", "deputy_president"):
        _validate_person(snapshot[section], section, seen)
    for section in ("ministers", "deputy_ministers"):
        if not isinstance(snapshot[section], list):
            raise CommandError(f"Snapshot {section!r} must be a list.")
        for person in snapshot[section]:
            _validate_person(person, section, seen)

    ministries = {
        snapshot["president"]["group"],
        snapshot["deputy_president"]["group"],
        *(person["group"] for person in snapshot["ministers"]),
        *(person["group"] for person in snapshot["deputy_ministers"]),
    }
    if not isinstance(snapshot["departments"], list):
        raise CommandError("Snapshot 'departments' must be a list.")
    seen_departments = set()
    for department in snapshot["departments"]:
        _validate_department(department, ministries, seen_departments)
    return snapshot


def _validate_person(
    person: dict, section: str, seen: set[tuple[str, str, str]]
) -> None:
    """Require the fields the seed writes, and reject duplicate appointments."""
    if not isinstance(person, dict):
        raise CommandError(f"Every entry in {section!r} must be an object.")
    for field in ("group", "role", "username", "first_name", "last_name"):
        if not str(person.get(field, "")).strip():
            raise CommandError(
                f"Entry in {section!r} is missing a non-empty {field!r}: {person!r}"
            )
    if person["role"] not in EXECUTIVE_ROLES:
        raise CommandError(
            f"Unknown role {person['role']!r} in {section!r}; "
            f"expected one of {', '.join(EXECUTIVE_ROLES)}."
        )
    if person.get("start_date"):
        try:
            date.fromisoformat(person["start_date"])
        except (TypeError, ValueError) as error:
            raise CommandError(
                f"Entry in {section!r} has an invalid start_date "
                f"{person['start_date']!r}; expected YYYY-MM-DD."
            ) from error
    key = (
        person["username"].strip().lower(),
        person["group"].strip(),
        person["role"],
    )
    if key in seen:
        raise CommandError(f"Duplicate appointment in snapshot: {key!r}")
    seen.add(key)


def _validate_department(
    department: dict, ministries: set[str], seen: set[tuple[str, str]]
) -> None:
    """Require a department name and a ministry that the snapshot defines."""
    if not isinstance(department, dict):
        raise CommandError("Every entry in 'departments' must be an object.")
    for field in ("name", "ministry"):
        if not str(department.get(field, "")).strip():
            raise CommandError(
                f"Department entry is missing a non-empty {field!r}: {department!r}"
            )
    if department["ministry"] not in ministries:
        raise CommandError(
            f"Department {department['name']!r} names unknown ministry "
            f"{department['ministry']!r}."
        )
    key = (department["name"].strip(), department["ministry"].strip())
    if key in seen:
        raise CommandError(f"Duplicate department in snapshot: {key!r}")
    seen.add(key)


def _appointment_note(snapshot: dict, person: dict) -> str:
    note = f"{SEED_NOTE_PREFIX} for the {snapshot['as_at']} snapshot."
    if person.get("note"):
        note = f"{note} {person['note']}."
    return note


class Command(BaseCommand):
    help = (
        "Seed the executive branch (Government of RSA, Presidency, ministries, "
        "national departments, executive roles) and its current office holders."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--data",
            type=Path,
            default=DEFAULT_SNAPSHOT,
            help=f"Executive snapshot to load (default: {DEFAULT_SNAPSHOT}).",
        )
        parser.add_argument(
            "--identity-source",
            choices=[choice for choice, _ in User.IDENTITY_SOURCE_CHOICES],
            default="local",
            help=(
                "identity_source for newly created users (default: local, which "
                "keeps office holders outside the Oracle sync)."
            ),
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help=(
                "End active appointments this command previously seeded that are "
                "absent from the snapshot. Manual appointments are never touched."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Run the seed inside the transaction and roll it back, so the "
                "reported counts are exact and nothing is written."
            ),
        )

    def handle(self, *args, **options):
        snapshot = _load_snapshot(options["data"])
        dry_run = options["dry_run"]
        stats = Counter()

        with transaction.atomic():
            self._seed(snapshot, stats, options["identity_source"], options["prune"])
            if dry_run:
                transaction.set_rollback(True)

        self._report(snapshot, stats, dry_run)

    # -- work ---------------------------------------------------------------
    def _seed(self, snapshot, stats, identity_source, prune):
        root = self._ensure_group(
            GOVERNMENT_ROOT_NAME, None, GOVERNMENT_ROOT_TYPE, stats
        )
        desired = {}

        people = [snapshot["president"], snapshot["deputy_president"]]
        people.extend(snapshot["ministers"])
        people.extend(snapshot["deputy_ministers"])

        groups = {}
        for person in people:
            group_type = (
                PRESIDENCY_TYPE
                if person["group"] == PRESIDENCY_NAME
                else Group.Type.MINISTRY
            )
            group = groups.get(person["group"])
            if group is None:
                group = self._ensure_group(person["group"], root, group_type, stats)
                groups[person["group"]] = group
            role = self._ensure_role(person["role"], stats)
            user = self._ensure_user(person, identity_source, stats)
            membership = self._ensure_membership(
                user, group, role, snapshot, person, stats
            )
            desired[membership.pk] = membership

        for department in snapshot["departments"]:
            self._ensure_department(department, groups[department["ministry"]], stats)

        if prune:
            self._prune(root, snapshot, desired, stats)

    def _ensure_group(self, name, parent, group_type, stats, description=""):
        group = Group.objects.filter(name=name, parent=parent).first()
        if group is None:
            group = Group.objects.create(
                name=name,
                parent=parent,
                group_type=group_type,
                description=description,
                is_active=True,
            )
            stats["groups_created"] += 1
            return group

        changed = []
        if group.group_type != group_type:
            group.group_type = group_type
            changed.append("group_type")
        if description and not group.description:
            group.description = description
            changed.append("description")
        if changed:
            group.save(update_fields=changed)
            stats["groups_updated"] += 1
        return group

    def _ensure_department(self, department, parent, stats):
        existed = Group.objects.filter(name=department["name"], parent=parent).exists()
        self._ensure_group(
            department["name"],
            parent,
            Group.Type.DEPARTMENT,
            stats,
            description=department.get("note", ""),
        )
        stats["departments_ensured"] += 1
        if not existed:
            stats["departments_created"] += 1

    def _ensure_role(self, name, stats):
        role = Role.objects.filter(name=name).first()
        if role is None:
            role = Role.objects.create(name=name, description=EXECUTIVE_ROLES[name])
            stats["roles_created"] += 1
        elif not role.description:
            role.description = EXECUTIVE_ROLES[name]
            role.save(update_fields=["description"])
            stats["roles_updated"] += 1
        return role

    def _ensure_user(self, person, identity_source, stats):
        username = person["username"].strip().lower()
        user = User.objects.filter(username__iexact=username).first()
        if user is None:
            user = User(
                username=username,
                title=person.get("title", ""),
                first_name=person["first_name"],
                middle_name=person.get("middle_name", ""),
                last_name=person["last_name"],
                identity_source=identity_source,
                is_active=True,
            )
            user.set_unusable_password()
            user.save()
            stats["users_created"] += 1
            return user

        # The identity already exists - typically an Oracle-owned row. Never
        # overwrite what the ERP wrote; only fill profile fields left blank.
        changed = []
        for field in ("title", "first_name", "middle_name", "last_name"):
            value = person.get(field, "")
            if value and not getattr(user, field):
                setattr(user, field, value)
                changed.append(field)
        if changed:
            user.save(update_fields=changed)
            stats["users_updated"] += 1
        if not user.is_active:
            stats["inactive_users"] += 1
            self.stderr.write(
                self.style.WARNING(
                    f"Office holder {username!r} already exists but is inactive; "
                    "the appointment is still recorded, but User.ministers() will "
                    "not return them until the account is reactivated."
                )
            )
        return user

    def _ensure_membership(self, user, group, role, snapshot, person, stats):
        start = (
            date.fromisoformat(person["start_date"])
            if person.get("start_date")
            else snapshot["_as_at"]
        )
        note = _appointment_note(snapshot, person)

        membership = GroupMembership.objects.filter(
            user=user, group=group, role=role, is_active=True
        ).first()
        if membership is None:
            membership = GroupMembership.objects.filter(
                user=user, group=group, role=role, start_date=start
            ).first()
            if membership is None:
                membership = GroupMembership(
                    user=user,
                    group=group,
                    role=role,
                    start_date=start,
                    notes=note,
                    is_active=True,
                )
                membership.save()
                stats["memberships_created"] += 1
                return membership
            # Reactivate an appointment this snapshot says is current.
            membership.is_active = True
            membership.end_date = None
            membership.save(update_fields=["is_active", "end_date"])
            stats["memberships_reactivated"] += 1

        if (
            not membership.notes or membership.notes.startswith(SEED_NOTE_PREFIX)
        ) and membership.notes != note:
            membership.notes = note
            membership.save(update_fields=["notes"])
            stats["memberships_updated"] += 1
        return membership

    def _prune(self, root, snapshot, desired, stats):
        """End active seeded appointments that are no longer in the snapshot."""
        current = GroupMembership.objects.filter(
            is_active=True,
            notes__startswith=SEED_NOTE_PREFIX,
            group__in=Group.objects.filter(
                parent=root, group_type__in=Group.EXECUTIVE_GROUP_TYPES
            ),
        ).exclude(pk__in=[membership.pk for membership in desired.values()])
        for membership in current:
            membership.is_active = False
            membership.end_date = snapshot["_as_at"]
            membership.notes = (
                f"{membership.notes} Ended by seed_executive on {snapshot['as_at']}."
            )
            membership.save(update_fields=["is_active", "end_date", "notes"])
            stats["memberships_pruned"] += 1

    def _report(self, snapshot, stats, dry_run):
        prefix = "DRY RUN - nothing written. " if dry_run else ""
        style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(
            style(
                f"{prefix}Executive seeded from the {snapshot['as_at']} snapshot: "
                f"{stats['groups_created']} group(s) created, "
                f"{stats['groups_updated']} updated; "
                f"{stats['departments_ensured']} department(s) ensured "
                f"({stats['departments_created']} created); "
                f"{stats['roles_created']} role(s) created, "
                f"{stats['roles_updated']} updated; "
                f"{stats['users_created']} user(s) created, "
                f"{stats['users_updated']} updated; "
                f"{stats['memberships_created']} appointment(s) created, "
                f"{stats['memberships_reactivated']} reactivated, "
                f"{stats['memberships_updated']} updated, "
                f"{stats['memberships_pruned']} pruned."
            )
        )
        if stats["inactive_users"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{stats['inactive_users']} office holder account(s) are "
                    "inactive; see the warnings above."
                )
            )
