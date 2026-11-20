"""
Django management command to export a WorkflowType definition (including its
states and transitions) to a JSON backup file.

The export uses natural keys (slugs/names) rather than database primary keys so
that the file can be imported into another environment or after objects have
been recreated. The matching import command is ``import_workflow_type``.

The payload carries everything the current ``pwms`` definition models hold:
the type's owning group, create roles and viewer groups, its owner-capability
policy, alert audience settings and parent type; each state's public status;
and each transition's allowed roles and required event types.

Usage:
    python manage.py export_workflow_type --workflow-type Petition
    python manage.py export_workflow_type --workflow-type Petition --output /tmp/petition.json
    python manage.py export_workflow_type --workflow-type "International Resolution"
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from pwms.models import WorkflowType

#: Bumped from the pre-``pwms`` format: the payload now names the public status,
#: required event types, viewer groups, owner capabilities and audience settings.
FORMAT_VERSION = 2


class Command(BaseCommand):
    help = "Export a WorkflowType with its states and transitions to a JSON file"

    def add_arguments(self, parser):
        parser.add_argument(
            "--workflow-type",
            type=str,
            required=True,
            help="WorkflowType name or slug to export (e.g. 'Petition')",
        )
        parser.add_argument(
            "--output",
            type=str,
            default="workflow_backups",
            help=(
                "Output file path. If this is a directory, the file is written "
                "inside it as '<slug>.json' (default: 'workflow_backups')."
            ),
        )

    def handle(self, *args, **options):
        workflow_type_ref = options["workflow_type"]
        output = options["output"]

        workflow_type = self._get_workflow_type(workflow_type_ref)

        payload = self._build_payload(workflow_type)

        output_path = self._resolve_output_path(output, workflow_type.slug)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")

        self.stdout.write(
            self.style.SUCCESS(f"✅ Exported '{workflow_type.name}' to: {output_path}")
        )
        self.stdout.write(
            f"   📊 {len(payload['states'])} state(s), "
            f"{len(payload['transitions'])} transition(s)"
        )

    def _get_workflow_type(self, ref: str) -> WorkflowType:
        try:
            return WorkflowType.objects.get(Q(name__iexact=ref) | Q(slug__iexact=ref))
        except WorkflowType.DoesNotExist:
            raise CommandError(f"WorkflowType '{ref}' not found.")
        except WorkflowType.MultipleObjectsReturned:
            raise CommandError(
                f"Multiple WorkflowTypes match '{ref}'. Use a unique name or slug."
            )

    def _build_payload(self, workflow_type: WorkflowType) -> dict:
        states = list(
            workflow_type.states.select_related("public_status").order_by(
                "order", "name"
            )
        )
        transitions = list(
            workflow_type.transitions.all()
            .order_by("order", "name")
            .select_related("from_state", "to_state")
            .prefetch_related("allowed_roles", "required_event_types")
        )

        return {
            "format_version": FORMAT_VERSION,
            "exported_at": timezone.now().isoformat(),
            "workflow_type": {
                "name": workflow_type.name,
                "slug": workflow_type.slug,
                "description": workflow_type.description,
                "enabled": workflow_type.enabled,
                "group_slug": (
                    workflow_type.group.slug if workflow_type.group_id else None
                ),
                "create_roles_slugs": self._roles_slugs(workflow_type.create_roles),
                "viewer_groups_slugs": sorted(
                    workflow_type.viewer_groups.values_list("slug", flat=True)
                ),
                "parent_type_slug": (
                    workflow_type.parent_type.slug
                    if workflow_type.parent_type_id
                    else None
                ),
                "owner_can_edit": workflow_type.owner_can_edit,
                "owner_can_transition": workflow_type.owner_can_transition,
                "owner_can_delete": workflow_type.owner_can_delete,
                "owner_can_share": workflow_type.owner_can_share,
                "owner_can_comment": workflow_type.owner_can_comment,
                "owner_can_manage": workflow_type.owner_can_manage,
                "oversight_channel": workflow_type.oversight_channel,
                "notify_next_actors": workflow_type.notify_next_actors,
                "notify_named_people": workflow_type.notify_named_people,
            },
            "states": [
                {
                    "slug": state.slug,
                    "name": state.name,
                    "public_status_slug": state.public_status.slug,
                    "public_status_name": state.public_status.name,
                    "description": state.description,
                    "is_initial": state.is_initial,
                    "is_terminal": state.is_terminal,
                    "allows_referrals": state.allows_referrals,
                    "order": state.order,
                    "color": state.color,
                }
                for state in states
            ],
            "transitions": [
                {
                    "slug": transition.slug,
                    "name": transition.name,
                    "from_state_slug": transition.from_state.slug,
                    "to_state_slug": transition.to_state.slug,
                    "allowed_roles_slugs": self._roles_slugs(
                        transition.allowed_roles
                    ),
                    "required_event_type_slugs": sorted(
                        transition.required_event_types.values_list(
                            "slug", flat=True
                        )
                    ),
                    "requires_comment": transition.requires_comment,
                    "order": transition.order,
                }
                for transition in transitions
            ],
        }

    @staticmethod
    def _roles_slugs(roles) -> list[str]:
        """Return a stable, sorted list of role slugs for a related manager."""
        return sorted(roles.values_list("slug", flat=True))

    @staticmethod
    def _resolve_output_path(output: str, slug: str) -> Path:
        path = Path(output)
        if path.suffix == "" or path.is_dir():
            return path / f"{slug}.json"
        return path
