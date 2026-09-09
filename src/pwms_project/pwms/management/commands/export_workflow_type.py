"""
Django management command to export a WorkflowType definition (including its
states and transitions) to a JSON backup file.

The export uses natural keys (slugs/names) rather than database primary keys so
that the file can be imported into another environment or after objects have
been recreated. The matching import command is ``import_workflow_type``.

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

from workflows.models import WorkflowType

FORMAT_VERSION = 1


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
            return WorkflowType.objects.get(
                Q(name__iexact=ref) | Q(slug__iexact=ref)
            )
        except WorkflowType.DoesNotExist:
            raise CommandError(f"WorkflowType '{ref}' not found.")
        except WorkflowType.MultipleObjectsReturned:
            raise CommandError(
                f"Multiple WorkflowTypes match '{ref}'. Use a unique name or slug."
            )

    def _build_payload(self, workflow_type: WorkflowType) -> dict:
        states = list(workflow_type.states.all().order_by("order", "name"))
        transitions = list(
            workflow_type.transitions.all()
            .order_by("order", "name")
            .select_related("from_state", "to_state")
            .prefetch_related("allowed_roles", "notify_roles")
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
                "notify_roles_slugs": self._roles_slugs(workflow_type.notify_roles),
            },
            "states": [
                {
                    "slug": state.slug,
                    "name": state.name,
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
                    "notify_roles_slugs": self._roles_slugs(
                        transition.notify_roles
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
        return sorted(role.slug for role in roles.all())

    @staticmethod
    def _resolve_output_path(output: str, slug: str) -> Path:
        path = Path(output)
        if path.suffix == "" or path.is_dir():
            return path / f"{slug}.json"
        return path
