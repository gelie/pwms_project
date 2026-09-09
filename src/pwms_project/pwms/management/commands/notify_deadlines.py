from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send notifications for workflows with pending and overdue deadlines, and auto-transition overdue workflows to follow-up"

    def add_arguments(self, parser):
        parser.add_argument(
            "--workflow-id",
            type=str,
            help="Process only a specific workflow UUID",
        )
        parser.add_argument(
            "--days-before",
            type=int,
            default=3,
            help="Number of days before deadline to send pending notifications (default: 3)",
        )
        parser.add_argument(
            "--skip-pending",
            action="store_true",
            help="Skip pending deadline notifications",
        )
        parser.add_argument(
            "--skip-overdue",
            action="store_true",
            help="Skip overdue notifications",
        )
        parser.add_argument(
            "--skip-transition",
            action="store_true",
            help="Skip automatic transitions to follow-up state",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force notifications even if already sent today",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )

    def handle(self, *args, **options):
        workflow_id = options.get("workflow_id")
        days_before = options.get("days_before", 3)
        skip_pending = options.get("skip_pending", False)
        skip_overdue = options.get("skip_overdue", False)
        skip_transition = options.get("skip_transition", False)
        force = options.get("force", False)
        dry_run = options.get("dry_run", False)

        # Get site URL for email links
        site_url = getattr(settings, "SITE_URL", "")

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )

        # Process pending deadlines (within N days)
        if not skip_pending:
            self.stdout.write("\n" + "=" * 60)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Processing workflows with deadlines within {days_before} days..."
                )
            )
            self.stdout.write("=" * 60)

            if not dry_run:
                from workflows.tasks import _notify_pending_deadlines_sync

                pending_result = _notify_pending_deadlines_sync(
                    site_url=site_url,
                    workflow_id=workflow_id,
                    days_before=days_before,
                    force=force,
                )

                self.stdout.write(
                    f"\n✓ Workflows processed: {pending_result['workflows_processed']}"
                )
                self.stdout.write(
                    f"✓ Notifications created: {pending_result['notifications_created']}"
                )
                self.stdout.write(f"✓ Emails sent: {pending_result['emails_sent']}")
            else:
                self.stdout.write(
                    self.style.WARNING("  [DRY RUN] Would check for pending deadlines")
                )

        # Process overdue workflows
        if not skip_overdue:
            self.stdout.write("\n" + "=" * 60)
            self.stdout.write(self.style.SUCCESS("Processing overdue workflows..."))
            self.stdout.write("=" * 60)

            if not dry_run:
                from workflows.tasks import _notify_overdue_workflows_sync

                overdue_result = _notify_overdue_workflows_sync(
                    site_url=site_url,
                    workflow_id=workflow_id,
                    force=force,
                )

                self.stdout.write(
                    f"\n✓ Workflows processed: {overdue_result['workflows_processed']}"
                )
                self.stdout.write(
                    f"✓ Notifications created: {overdue_result['notifications_created']}"
                )
                self.stdout.write(f"✓ Emails sent: {overdue_result['emails_sent']}")
            else:
                self.stdout.write(
                    self.style.WARNING("  [DRY RUN] Would check for overdue workflows")
                )

        # Auto-transition overdue workflows to follow-up
        if not skip_transition:
            self.stdout.write("\n" + "=" * 60)
            self.stdout.write(
                self.style.SUCCESS(
                    "Auto-transitioning overdue workflows to follow-up state..."
                )
            )
            self.stdout.write("=" * 60)

            from workflows.tasks import _transition_overdue_to_followup_sync

            transition_result = _transition_overdue_to_followup_sync(
                workflow_id=workflow_id,
                auto_transition=not dry_run,
            )

            self.stdout.write(
                f"\n✓ Workflows checked: {transition_result['workflows_checked']}"
            )
            self.stdout.write(
                f"✓ Workflows transitioned: {transition_result['workflows_transitioned']}"
            )
            self.stdout.write(
                f"✗ Transitions failed: {transition_result['transitions_failed']}"
            )

            if transition_result["transitions_failed"] > 0:
                self.stdout.write(
                    self.style.WARNING(
                        f"\nNote: {transition_result['transitions_failed']} workflow(s) could not be transitioned."
                    )
                )
                self.stdout.write(
                    self.style.WARNING(
                        "  This may be because no 'Follow-up' state exists or no valid transition is configured."
                    )
                )

        # Summary
        self.stdout.write("\n" + "=" * 60)
        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN COMPLETE - No changes were made")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS("Deadline notifications and transitions complete!")
            )
        self.stdout.write("=" * 60 + "\n")
