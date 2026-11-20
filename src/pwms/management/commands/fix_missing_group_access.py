"""
Deprecated alias for ``sync_type_group_access``.

The old command imported the removed ``workflows.models.Workflow`` and tried to
repair a single ``Workflow`` table. The current registers are separate concrete
models, so the repair now reads each instance's ``WorkflowType`` and creates the
owning group's and viewer groups' ``WorkflowGroupAccess`` rows. This command
keeps existing cron/systemd invocations working by delegating to that command.

Prefer:

    python manage.py sync_type_group_access
    python manage.py sync_type_group_access --workflow-type "Bill" --dry-run
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Deprecated: delegates to sync_type_group_access "
        "(creates missing WorkflowGroupAccess rows from each instance's type)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be fixed without making changes",
        )
        parser.add_argument(
            "--workflow-type",
            type=str,
            default=None,
            help="Restrict the backfill to one WorkflowType (name or slug)",
        )

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.WARNING(
                "fix_missing_group_access is deprecated; delegating to "
                "sync_type_group_access."
            )
        )
        arguments = []
        if options["dry_run"]:
            arguments.append("--dry-run")
        if options["workflow_type"]:
            arguments.extend(["--workflow-type", options["workflow_type"]])
        call_command(
            "sync_type_group_access",
            *arguments,
            stdout=self.stdout,
            stderr=self.stderr,
        )
