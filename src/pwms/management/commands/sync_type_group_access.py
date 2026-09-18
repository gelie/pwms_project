"""Apply a workflow type's group access to existing workflow instances.

Configuring a ``WorkflowType`` — its owning ``group``, its ``viewer_groups`` and the
capabilities it grants that group (``owner_can_*``) — materialises
``WorkflowGroupAccess`` rows on **new** instances only (see
``AbstractLegislativeWorkflow.materialize_group_access``). This command applies the
current configuration to instances that predate it:

* by default it only creates **missing** rows — the owning group flagged
  ``is_primary`` with the type's capabilities, and each viewer group read-only;
* with ``--update-existing`` it also re-applies the type's capability policy
  (including ``is_primary``) to the owning group's existing row, so a change to the
  type reaches records that already carried a grant.

By default it is idempotent and non-destructive — an existing row for a group,
including one an administrator has since edited, is left untouched — so it is safe
to re-run. ``--update-existing`` deliberately overwrites the owning group's base
flags; it never touches that access row's per-role ``WorkflowRolePermission`` or
per-state ``WorkflowStatePermission`` children, and never a viewer group's row.
Use ``--dry-run`` to preview and ``--workflow-type`` to scope to one type.

Supersedes the legacy ``fix_missing_group_access`` command, which still imports
the removed ``workflows.models`` module.

Usage:
    python manage.py sync_type_group_access
    python manage.py sync_type_group_access --workflow-type "International Agreement"
    python manage.py sync_type_group_access --update-existing
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


def reconcile_primary_row(instance, commit=True):
    """
    Re-apply the type's capability policy to the owning group's existing row.

    Returns the ``WorkflowGroupAccess`` fields that need changing (sorted), which
    is empty when the row is missing — the create path handles that case — or
    already matches the policy. With ``commit=False`` nothing is written, so
    ``--dry-run`` can report the same fields it would set.
    """
    workflow_type = instance.workflow_type
    if workflow_type.group_id is None:
        return []
    access = instance.group_accesses().filter(group_id=workflow_type.group_id).first()
    if access is None:
        return []
    policy = workflow_type.owner_access_defaults()
    changed = sorted(
        field for field, value in policy.items() if getattr(access, field) != value
    )
    if changed and commit:
        for field in changed:
            setattr(access, field, policy[field])
        access.save(update_fields=changed)
    return changed


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
            help="Report what would be created or changed without writing anything.",
        )
        parser.add_argument(
            "--update-existing",
            action="store_true",
            help=(
                "Also re-apply the type's capability policy (owner_can_*) to the "
                "owning group's existing row. Without this, an edited row is never "
                "overwritten."
            ),
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
        update_existing = options["update_existing"]
        scope = options["workflow_type"] or "all workflow types"
        type_ids = list(
            self._workflow_types(options["workflow_type"]).values_list("pk", flat=True)
        )

        checked = 0
        updated = 0
        created_rows = 0
        refreshed = 0
        for model in concrete_workflow_models():
            instances = model.objects.filter(
                workflow_type_id__in=type_ids
            ).select_related("workflow_type")
            for instance in instances:
                checked += 1
                touched = False
                missing = missing_group_ids(instance)
                if missing:
                    if dry_run:
                        touched = True
                        created_rows += len(missing)
                        self.stdout.write(
                            f"  would add {len(missing)} row(s) to {instance}"
                        )
                    else:
                        created = instance.materialize_group_access()
                        if created:
                            touched = True
                            created_rows += len(created)
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"  {instance}: added {len(created)} row(s)"
                                )
                            )
                if update_existing:
                    changed = reconcile_primary_row(instance, commit=not dry_run)
                    if changed:
                        touched = True
                        refreshed += 1
                        fields = ", ".join(changed)
                        if dry_run:
                            self.stdout.write(
                                f"  would refresh {instance}'s owning-group grant: "
                                f"{fields}"
                            )
                        else:
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"  {instance}: refreshed owning-group grant "
                                    f"({fields})"
                                )
                            )
                if touched:
                    updated += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN: {updated} of {checked} instance(s) ({scope}) would gain "
                    f"{created_rows} row(s) and refresh {refreshed} grant(s)."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done: added {created_rows} row(s) across {updated} of {checked} "
                    f"instance(s) ({scope}), refreshing {refreshed} existing grant(s)."
                )
            )
