"""
Django management command to import a WorkflowType definition (including its
states and transitions) from a JSON backup produced by ``export_workflow_type``.

The import matches objects by natural keys (slugs/names), so it is safe to run
against an environment where database primary keys differ from the backup. It
upserts: existing objects are updated, missing objects are created. It does not
delete states or transitions that are absent from the file.

Roles and the owning group are referenced by slug and must already exist.

Usage:
    python manage.py import_workflow_type workflow_backups/petition.json
    python manage.py import_workflow_type workflow_backups/petition.json --dry-run
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from workflows.models import Group, Role, State, Transition, WorkflowType

FORMAT_VERSION = 1


class Command(BaseCommand):
    help = "Import a WorkflowType with its states and transitions from a JSON file"

    def add_arguments(self, parser):
        parser.add_argument(
            "json_file",
            type=str,
            help="Path to the JSON backup file to import",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate the file and show what would be changed without saving",
        )

    def handle(self, *args, **options):
        json_file = Path(options["json_file"])
        dry_run = options["dry_run"]

        if not json_file.exists():
            raise CommandError(f"JSON file not found: {json_file}")

        payload = self._load_payload(json_file)

        wt_data = payload["workflow_type"]
        states_data = payload.get("states", [])
        transitions_data = payload.get("transitions", [])

        group = self._resolve_group(wt_data.get("group_slug"))
        create_roles = self._resolve_roles(wt_data.get("create_roles_slugs", []))
        notify_roles = self._resolve_roles(wt_data.get("notify_roles_slugs", []))

        counts = {"created": 0, "updated": 0, "skipped": 0}

        workflow_type, action = self._upsert_workflow_type(
            wt_data, group, create_roles, notify_roles, dry_run
        )
        self._record(counts, action)

        states_by_slug = {}
        states_by_name = {}
        for state_data in states_data:
            state, action = self._upsert_state(
                workflow_type, state_data, dry_run
            )
            if state_data.get("slug"):
                states_by_slug[state_data["slug"]] = state
            states_by_name[state_data["name"]] = state
            self._record(counts, action)

        for transition_data in transitions_data:
            action = self._upsert_transition(
                workflow_type, transition_data, states_by_slug, states_by_name, dry_run
            )
            self._record(counts, action)

        self._write_summary(dry_run, counts, workflow_type)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_payload(self, json_file: Path) -> dict:
        with open(json_file, encoding="utf-8") as f:
            payload = json.load(f)

        if not isinstance(payload, dict):
            raise CommandError("Invalid backup file: top-level value must be an object.")

        version = payload.get("format_version")
        if version != FORMAT_VERSION:
            raise CommandError(
                f"Unsupported format_version: {version!r} (expected {FORMAT_VERSION})."
            )

        if "workflow_type" not in payload:
            raise CommandError("Invalid backup file: missing 'workflow_type' section.")

        return payload

    def _resolve_group(self, slug):
        if not slug:
            return None
        try:
            return Group.objects.get(slug=slug)
        except Group.DoesNotExist:
            raise CommandError(
                f"Group with slug '{slug}' not found. Create it first."
            )

    def _resolve_roles(self, slugs) -> list[Role]:
        roles = []
        for slug in slugs:
            try:
                roles.append(Role.objects.get(slug=slug))
            except Role.DoesNotExist:
                raise CommandError(
                    f"Role with slug '{slug}' not found. Create it first."
                )
        return roles

    def _upsert_workflow_type(self, data, group, create_roles, notify_roles, dry_run):
        existing = WorkflowType.objects.filter(
            Q(slug__iexact=data.get("slug", "")) | Q(name__iexact=data["name"])
        ).first()

        fields = {
            "name": data["name"],
            "description": data.get("description", ""),
            "enabled": data.get("enabled", True),
            "group": group,
        }

        if existing:
            scalar_changed = self._fields_changed(existing, fields)
            m2m_changed = self._m2m_changed(existing.create_roles, create_roles) or self._m2m_changed(
                existing.notify_roles, notify_roles
            )

            if scalar_changed or m2m_changed:
                self._apply_fields(existing, fields)
                if not dry_run:
                    existing.save()
                    existing.create_roles.set(create_roles)
                    existing.notify_roles.set(notify_roles)
                self.stdout.write(f"  🔄 Updated workflow type: {existing.name}")
                return existing, "updated"

            self.stdout.write(
                f"  ⏭️  Skipped workflow type (unchanged): {existing.name}"
            )
            return existing, "skipped"

        workflow_type = WorkflowType(**fields)
        if not dry_run:
            workflow_type.save()
            workflow_type.create_roles.set(create_roles)
            workflow_type.notify_roles.set(notify_roles)
        self.stdout.write(f"  ✅ Created workflow type: {workflow_type.name}")
        return workflow_type, "created"

    def _upsert_state(self, workflow_type, data, dry_run):
        slug = data.get("slug") or None
        fields = {
            "name": data["name"],
            "description": data.get("description", ""),
            "is_initial": data.get("is_initial", False),
            "is_terminal": data.get("is_terminal", False),
            "allows_referrals": data.get("allows_referrals", True),
            "order": data.get("order", 0),
            "color": data.get("color", "#5b8f22"),
        }
        if slug:
            fields["slug"] = slug

        existing = None
        if workflow_type.pk is not None:
            if slug:
                existing = State.objects.filter(
                    workflow_type=workflow_type, slug=slug
                ).first()
            else:
                existing = State.objects.filter(
                    workflow_type=workflow_type, name=data["name"]
                ).first()

        if existing:
            if self._fields_changed(existing, fields):
                self._apply_fields(existing, fields)
                if not dry_run:
                    existing.save()
                self.stdout.write(f"  🔄 Updated state: {existing.name}")
                return existing, "updated"

            self.stdout.write(f"  ⏭️  Skipped state (unchanged): {existing.name}")
            return existing, "skipped"

        state = State(workflow_type=workflow_type, **fields)
        if not dry_run:
            state.save()
        self.stdout.write(f"  ✅ Created state: {state.name}")
        return state, "created"

    def _upsert_transition(
        self, workflow_type, data, states_by_slug, states_by_name, dry_run
    ):
        from_slug = data.get("from_state_slug")
        to_slug = data.get("to_state_slug")
        from_name = data.get("from_state_name")
        to_name = data.get("to_state_name")

        from_state = states_by_slug.get(from_slug) if from_slug else states_by_name.get(from_name)
        to_state = states_by_slug.get(to_slug) if to_slug else states_by_name.get(to_name)

        from_ref = from_slug or from_name
        to_ref = to_slug or to_name
        if from_state is None or to_state is None:
            raise CommandError(
                f"Transition '{data.get('name')}' references unknown state(s): "
                f"'{from_ref}' -> '{to_ref}'."
            )

        allowed_roles = self._resolve_roles(data.get("allowed_roles_slugs", []))
        notify_roles = self._resolve_roles(data.get("notify_roles_slugs", []))

        slug = data.get("slug") or None
        fields = {
            "name": data.get("name", ""),
            "requires_comment": data.get("requires_comment", True),
            "order": data.get("order", 0),
        }
        if slug:
            fields["slug"] = slug

        existing = None
        if (
            workflow_type.pk is not None
            and from_state.pk is not None
            and to_state.pk is not None
        ):
            if slug:
                existing = Transition.objects.filter(
                    workflow_type=workflow_type, slug=slug
                ).first()
            else:
                existing = Transition.objects.filter(
                    workflow_type=workflow_type,
                    from_state=from_state,
                    to_state=to_state,
                ).first()

        if existing:
            scalar_changed = self._fields_changed(existing, fields)
            m2m_changed = self._m2m_changed(existing.allowed_roles, allowed_roles) or self._m2m_changed(
                existing.notify_roles, notify_roles
            )

            if scalar_changed or m2m_changed:
                self._apply_fields(existing, fields)
                if not dry_run:
                    existing.save()
                    existing.allowed_roles.set(allowed_roles)
                    existing.notify_roles.set(notify_roles)
                self.stdout.write(
                    f"  🔄 Updated transition: {from_state.name} → {to_state.name}"
                )
                return "updated"

            self.stdout.write(
                f"  ⏭️  Skipped transition (unchanged): "
                f"{from_state.name} → {to_state.name}"
            )
            return "skipped"

        transition = Transition(
            workflow_type=workflow_type,
            from_state=from_state,
            to_state=to_state,
            **fields,
        )
        if not dry_run:
            transition.save()
            transition.allowed_roles.set(allowed_roles)
            transition.notify_roles.set(notify_roles)
        self.stdout.write(
            f"  ✅ Created transition: {from_state.name} → {to_state.name}"
        )
        return "created"

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _fields_changed(obj, fields) -> bool:
        return any(getattr(obj, name) != value for name, value in fields.items())

    @staticmethod
    def _m2m_changed(manager, roles) -> bool:
        """Return True when a related manager's slugs differ from the desired roles."""
        current = set(manager.values_list("slug", flat=True))
        desired = {role.slug for role in roles}
        return current != desired

    @staticmethod
    def _apply_fields(obj, fields):
        for name, value in fields.items():
            setattr(obj, name, value)

    @staticmethod
    def _record(counts, action):
        counts[action] = counts.get(action, 0) + 1

    def _write_summary(self, dry_run, counts, workflow_type):
        self.stdout.write("")
        if dry_run:
            self.stdout.write(
                self.style.WARNING("🏁 DRY RUN — no changes were made. Summary:")
            )
        else:
            self.stdout.write(self.style.SUCCESS("✅ Import complete. Summary:"))

        self.stdout.write(f"   Workflow type: {workflow_type.name}")
        self.stdout.write(f"   ✨ Created:  {counts.get('created', 0)}")
        self.stdout.write(f"   🔄 Updated:  {counts.get('updated', 0)}")
        self.stdout.write(f"   ⏭️  Skipped:  {counts.get('skipped', 0)}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\n💡 Run without --dry-run to apply these changes."
                )
            )
