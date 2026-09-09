from django.core.management.base import BaseCommand
from django.utils import timezone
from workflows.models import Event


class Command(BaseCommand):
    help = "Automatically update event statuses based on scheduled times"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        now = timezone.now()

        updated_count = 0
        to_in_progress = []
        to_completed = []

        # Find events that should transition to "in_progress"
        # Only transition if status is "scheduled" and start time has passed
        events_to_start = Event.objects.filter(
            status="scheduled", start_datetime__lte=now
        )

        for event in events_to_start:
            to_in_progress.append(event)
            if not dry_run:
                event.status = "in_progress"
                event.save()
                updated_count += 1

        # Find events that should transition to "completed"
        # Only transition if status is "in_progress" and end time has passed
        events_to_complete = Event.objects.filter(
            status="in_progress", end_datetime__lte=now
        )

        for event in events_to_complete:
            to_completed.append(event)
            if not dry_run:
                event.status = "completed"
                event.save()
                updated_count += 1

        # Output results
        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN - No changes will be made")
            )

        if to_in_progress:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nEvents to transition to 'In Progress' ({len(to_in_progress)}):"
                )
            )
            for event in to_in_progress:
                self.stdout.write(
                    f"  - [{event.pk}] {event.title} (started at {event.start_datetime})"
                )

        if to_completed:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nEvents to transition to 'Completed' ({len(to_completed)}):"
                )
            )
            for event in to_completed:
                self.stdout.write(
                    f"  - [{event.pk}] {event.title} (ended at {event.end_datetime})"
                )

        if not to_in_progress and not to_completed:
            self.stdout.write(
                self.style.SUCCESS("No events need status updates at this time.")
            )
        elif not dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nSuccessfully updated {updated_count} event(s)."
                )
            )
