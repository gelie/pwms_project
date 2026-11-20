"""
Django management command to import a WorkflowType definition (including its
states and transitions) from a JSON backup produced by ``export_workflow_type``.

The import matches objects by natural keys (slugs/names), so it is safe to run
against an environment where database primary keys differ from the backup. It
upserts: existing objects are updated, missing objects are created. It does not
delete states or transitions that are absent from the file.

The owning group, roles, public statuses, viewer groups, parent type and
required event types are referenced by slug and must already exist. A format-1
backup (written before the ``pwms`` models gained public statuses and event
guards) still imports: a state without a public status keeps the one it already
has, or the ``New`` status, and its obsolete ``notify_roles_slugs`` are ignored.

Usage:
    python manage.py import_workflow_type workflow_backups/petition.json
    python manage.py import_workflow_type workflow_backups/petition.json --dry-run
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from pwms.models import (
    EventType,
    Group,
    PublicStatus,
    Role,
    State,
    Transition,
    WorkflowType,
)
from pwms.state_colors import state_color

FORMAT_VERSION = 2

#: Format 1 predates the public-status registry, viewer groups and event guards.
SUPPORTED_FORMAT_VERSIONS = {1, FORMAT_VERSION}

#: Type-level booleans carried by a format-2 backup. Absent keys keep the model
#: default (for a new type) or the existing value (for an update).
BOOLEAN_FIELDS = (
    "owner_can_edit",
    "owner_can_transition",
    "owner_can_delete",
    "owner_can_share",
    "owner_can_comment",
    "owner_can_manage",
    "notify_next_actors",
    "notify_named_people",
)


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
        if payload["format_version"] == 1:
            self.stdout.write(
                self.style.WARNING(
                    "Format 1 backup: states without a public status keep theirs "
                    "(or default to 'New'); notify_roles_slugs are ignored."
                )
            )

        wt_data = payload["workflow_type"]
        states_data = payload.get("states", [])
        transitions_data = payload.get("transitions", [])

        group = self._resolve_group(wt_data.get("group_slug"))
        create_roles = self._resolve_roles(wt_data.get("create_roles_slugs", []))
        viewer_groups = self._resolve_groups(wt_data.get("viewer_groups_slugs", []))
        parent_type = self._resolve_parent_type(wt_data.get("parent_type_slug"))

        counts = {"created": 0, "updated": 0, "skipped": 0}

        workflow_type, action = self._upsert_workflow_type(
            wt_data,
            group,
            create_roles,
            viewer_groups,
            parent_type,
            dry_run,
        )
        self._record(counts, action)

        states_by_slug = {}
        states_by_name = {}
        for state_data in states_data:
            state, action = self._upsert_state(workflow_type, state_data, dry_run)
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
        if version not in SUPPORTED_FORMAT_VERSIONS:
            raise CommandError(
                f"Unsupported format_version: {version!r} "
                f"(supported: {sorted(SUPPORTED_FORMAT_VERSIONS)})."
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
        return [self._resolve_role(slug) for slug in slugs or []]

    def _resolve_role(self, slug) -> Role:
        try:
            return Role.objects.get(slug=slug)
        except Role.DoesNotExist:
            raise CommandError(f"Role with slug '{slug}' not found. Create it first.")

    def _resolve_groups(self, slugs) -> list[Group]:
        groups = []
        for slug in slugs or []:
            group = self._resolve_group(slug)
            if group is not None:
                groups.append(group)
        return groups

    def _resolve_parent_type(self, slug):
        if not slug:
            return None
        try:
            return WorkflowType.objects.get(slug=slug)
        except WorkflowType.DoesNotExist:
            raise CommandError(
                f"Parent WorkflowType with slug '{slug}' not found. Create it first."
            )

    def _resolve_public_status(self, data, existing=None) -> PublicStatus:
        ref = (
            data.get("public_status_slug")
            or data.get("public_status_name")
            or data.get("public_status")
            or ""
        ).strip()
        if ref:
            status = PublicStatus.objects.filter(
                Q(slug__iexact=ref) | Q(name__iexact=ref)
            ).first()
            if status is None:
                raise CommandError(
                    f"PublicStatus '{ref}' not found. Create it first."
                )
            return status
        if existing is not None and existing.public_status_id:
            return existing.public_status
        default = PublicStatus.objects.filter(name__iexact="New").first()
        if default is None:
            raise CommandError(
                "A state has no public status and there is no 'New' PublicStatus. "
                "Run migrations or add one in the admin."
            )
        return default

    def _resolve_event_types(self, slugs) -> list[EventType]:
        event_types = []
        for slug in slugs or []:
            try:
                event_types.append(EventType.objects.get(slug=slug))
            except EventType.DoesNotExist:
                raise CommandError(
                    f"EventType with slug '{slug}' not found. Create it first."
                )
        return event_types

    def _upsert_workflow_type(
        self,
        data,
        group,
        create_roles,
        viewer_groups,
        parent_type,
        dry_run,
    ):
        existing = WorkflowType.objects.filter(
            Q(slug__iexact=data.get("slug", "")) | Q(name__iexact=data["name"])
        ).first()

        fields = {
            "name": data["name"],
            "description": data.get("description", ""),
            "enabled": data.get("enabled", True),
            "group": group,
        }
        if "parent_type_slug" in data:
            fields["parent_type"] = parent_type
        for field in BOOLEAN_FIELDS:
            if field in data:
                fields[field] = bool(data[field])
        oversight = data.get("oversight_channel")
        if oversight:
            if oversight in dict(WorkflowType.OVERSIGHT_CHOICES):
                fields["oversight_channel"] = oversight
            else:
                raise CommandError(
                    f"Invalid oversight_channel '{oversight}' for workflow type "
                    f"'{data['name']}'."
                )

        # A format-1 backup predates these M2Ms; leave them untouched rather than
        # clearing what an administrator has since configured.
        sync_create_roles = "create_roles_slugs" in data
        sync_viewer_groups = "viewer_groups_slugs" in data

        if existing:
            scalar_changed = self._fields_changed(existing, fields)
            m2m_changed = (sync_create_roles and self._m2m_changed(
                existing.create_roles, create_roles
            )) or (sync_viewer_groups and self._m2m_changed(
                existing.viewer_groups, viewer_groups
            ))

            if scalar_changed or m2m_changed:
                self._apply_fields(existing, fields)
                if not dry_run:
                    existing.save()
                    if sync_create_roles:
                        existing.create_roles.set(create_roles)
                    if sync_viewer_groups:
                        existing.viewer_groups.set(viewer_groups)
                self.stdout.write(f"  🔄 Updated workflow type: {existing.name}")
                return existing, "updated"

            self.stdout.write(
                f"  ⏭️  Skipped workflow type (unchanged): {existing.name}"
            )
            return existing, "skipped"

        workflow_type = WorkflowType(**fields)
        if not dry_run:
            workflow_type.save()
            if sync_create_roles:
                workflow_type.create_roles.set(create_roles)
            if sync_viewer_groups:
                workflow_type.viewer_groups.set(viewer_groups)
        self.stdout.write(f"  ✅ Created workflow type: {workflow_type.name}")
        return workflow_type, "created"

    def _upsert_state(self, workflow_type, data, dry_run):
        slug = data.get("slug") or None

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

        fields = {
            "name": data["name"],
            "public_status": self._resolve_public_status(data, existing),
            "description": data.get("description", ""),
            "is_initial": data.get("is_initial", False),
            "is_terminal": data.get("is_terminal", False),
            "allows_referrals": data.get("allows_referrals", True),
            "order": data.get("order", 0),
            # A definition without a colour gets the standard one for the role.
            "color": data.get("color")
            or state_color(
                is_initial=data.get("is_initial", False),
                is_terminal=data.get("is_terminal", False),
                name=data["name"],
            ),
        }
        if slug:
            fields["slug"] = slug

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

        from_state = (
            states_by_slug.get(from_slug) if from_slug else states_by_name.get(from_name)
        )
        to_state = (
            states_by_slug.get(to_slug) if to_slug else states_by_name.get(to_name)
        )

        from_ref = from_slug or from_name
        to_ref = to_slug or to_name
        if from_state is None or to_state is None:
            raise CommandError(
                f"Transition '{data.get('name')}' references unknown state(s): "
                f"'{from_ref}' -> '{to_ref}'."
            )

        sync_allowed_roles = "allowed_roles_slugs" in data
        sync_event_guards = "required_event_type_slugs" in data
        allowed_roles = (
            self._resolve_roles(data.get("allowed_roles_slugs", []))
            if sync_allowed_roles
            else []
        )
        required_event_types = (
            self._resolve_event_types(data.get("required_event_type_slugs", []))
            if sync_event_guards
            else []
        )

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
            m2m_changed = (sync_allowed_roles and self._m2m_changed(
                existing.allowed_roles, allowed_roles
            )) or (sync_event_guards and self._m2m_changed(
                existing.required_event_types, required_event_types
            ))

            if scalar_changed or m2m_changed:
                self._apply_fields(existing, fields)
                if not dry_run:
                    existing.save()
                    if sync_allowed_roles:
                        existing.allowed_roles.set(allowed_roles)
                    if sync_event_guards:
                        existing.required_event_types.set(required_event_types)
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
            if sync_allowed_roles:
                transition.allowed_roles.set(allowed_roles)
            if sync_event_guards:
                transition.required_event_types.set(required_event_types)
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
    def _m2m_changed(manager, related) -> bool:
        """Return True when a related manager's slugs differ from the desired rows."""
        current = set(manager.values_list("slug", flat=True))
        desired = {row.slug for row in related}
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
