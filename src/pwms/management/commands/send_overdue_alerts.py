from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send overdue workflow notifications and email alerts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--site-url",
            default=getattr(settings, "SITE_URL", ""),
            help="Base URL for workflow links in emails (e.g. https://bungeni.example.com)",
        )
        parser.add_argument(
            "--workflow-id",
            type=str,
            default=None,
            help="Only process the workflow with this UUID.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Bypass overdue/terminal checks and today's deduplication. Requires --workflow-id.",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Run the task synchronously instead of enqueuing it.",
        )

    def handle(self, *args, **options):
        from workflows.tasks import notify_overdue_workflows

        site_url = options["site_url"]
        workflow_id = options["workflow_id"]
        force = options["force"]

        if force and workflow_id is None:
            self.stderr.write(self.style.ERROR("--force requires --workflow-id."))
            return

        if options["sync"]:
            from workflows.tasks import _notify_overdue_workflows_sync

            result = _notify_overdue_workflows_sync(
                site_url=site_url, workflow_id=workflow_id, force=force
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. Processed {result['workflows_processed']} overdue workflow(s), "
                    f"created {result['notifications_created']} notification(s), "
                    f"sent {result['emails_sent']} email(s)."
                )
            )
        else:
            from workflows.tasks import notify_overdue_workflows

            notify_overdue_workflows(
                site_url=site_url, workflow_id=workflow_id, force=force
            )
            self.stdout.write(self.style.SUCCESS("Overdue alerts task scheduled."))
