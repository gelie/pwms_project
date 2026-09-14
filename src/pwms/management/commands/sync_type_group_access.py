"""Backfill type-level group access onto existing workflow instances.

Configuring a ``WorkflowType`` — its owning ``group`` and its ``viewer_groups``
— materialises ``WorkflowGroupAccess`` rows on **new** instances only (see
``AbstractLegislativeWorkflow.materialize_group_access``). This command applies
the current configuration to instances that predate it, creating any missing
rows: the owning group flagged ``is_primary`` and each viewer group read-only.

It is idempotent and non-destructive — an existing row for a group, including
one an administrator has since edited, is left untouched — so it is safe to
re-run. Use ``--dry-run`` to preview and ``--workflow-type`` to scope to one
type.

Supersedes the legacy ``fix_missing_group_access`` command, which still imports
the removed ``workflows.models`` module.

Usage:
    python manage.py sync_type_group_access
    python manage.py sync_type_group_access --workflow-type "International Agreement"
    python manage.py sync_type_group_access --dry-run
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from pwms.models import AbstractLegislativeWorkflow, WorkflowType


def concrete_workflow_models():
    """Concrete (non-abstract) subclasses of the legislative workflow base."""
    models = []
    for model in AbstractLegislativeWorkflow.__subclasses__():
        if model._meta.abstract or model in models:
            continue
        models.append(model)
    return models


def missing_group_ids(instance):
    """Group ids the instance's type wants granted but which have no row yet."""
    workflow_type = instance.workflow_type
    wanted = set()
    if workflow_type.group_id is not None:
        wanted.add(workflow_type.group_id)
    wanted.update(workflow_type.viewer_groups.values_list("pk", flat=True))
    if not wanted:
        return set()
    existing = set(instance.group_accesses().values_list("group_id", flat=True))
    return wanted - existing


class Command(BaseCommand):
    help = (
        "Create missing WorkflowGroupAccess rows (owning group + viewer groups) "
        "on existing workflow instances, from their workflow type."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workflow-type",
            type=str,
            default=None,
            help="Only backfill instances of this WorkflowType (name or slug).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing anything.",
        )

    def _workflow_types(self, ref):
        if ref is None:
            return WorkflowType.objects.all()
        matching = WorkflowType.objects.filter(
            Q(name__iexact=ref) | Q(slug__iexact=ref)
        )
        if not matching.exists():
            raise CommandError(f"WorkflowType '{ref}' not found.")
        return matching

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        scope = options["workflow_type"] or "all workflow types"
        type_ids = list(
            self._workflow_types(options["workflow_type"]).values_list("pk", flat=True)
        )

        checked = 0
        updated = 0
        created_rows = 0
        for model in concrete_workflow_models():
            instances = model.objects.filter(
                workflow_type_id__in=type_ids
            ).select_related("workflow_type")
            for instance in instances:
                checked += 1
                missing = missing_group_ids(instance)
                if not missing:
                    continue
                if dry_run:
                    updated += 1
                    created_rows += len(missing)
                    self.stdout.write(
                        f"  would add {len(missing)} row(s) to {instance}"
                    )
                    continue
                created = instance.materialize_group_access()
                if created:
                    updated += 1
                    created_rows += len(created)
                    self.stdout.write(
                        self.style.SUCCESS(f"  {instance}: added {len(created)} row(s)")
                    )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN: {updated} of {checked} instance(s) ({scope}) would "
                    f"gain {created_rows} access row(s)."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done: added {created_rows} row(s) across {updated} of {checked} "
                    f"instance(s) ({scope})."
                )
            )
