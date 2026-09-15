"""Email every scheduled report share that has fallen due.

A share made from the reports page can repeat (daily, weekly or monthly). This
command is the drain: it emails each due share — the link, plus the rendered
document when the share names a format — and moves its ``next_send_at`` on by one
interval.

Two ways to run it:

* from cron / systemd / a beat-style runner, like the other periodic commands;
* queue it once as a repeating background task with ``--queue``, and let
  ``manage.py process_tasks`` keep it going.

Both paths call the same service (``pwms.reporting.send_scheduled_shares``), so
they cannot drift apart.
"""

from django.core.management.base import BaseCommand

from pwms.reporting import due_scheduled_shares, send_scheduled_shares


class Command(BaseCommand):
    help = "Email due scheduled report shares and advance their schedules"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the shares that are due without emailing or advancing anything",
        )
        parser.add_argument(
            "--queue",
            action="store_true",
            help=(
                "Queue the repeating background task (django-background-tasks) "
                "instead of running once"
            ),
        )

    def handle(self, *args, **options):
        if options["queue"]:
            self.queue_task()
            return
        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING("DRY RUN - no mail is sent and nothing is saved")
            )
            self.preview()
            return
        self.report(send_scheduled_shares())

    def queue_task(self):
        # Imported here so the run-once path does not depend on the background
        # task registry being loaded.
        from pwms.tasks import queue_scheduled_report_shares

        task = queue_scheduled_report_shares()
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued {task!r} to repeat every {task.repeat} seconds "
                f"(task #{task.pk}). Run `manage.py process_tasks` to serve it."
            )
        )

    def preview(self):
        due = list(due_scheduled_shares())
        self.stdout.write(f"Found {len(due)} scheduled share(s) due")
        for share in due:
            self.stdout.write(
                self.style.WARNING(
                    f"  [DRY RUN] Would send {share!r} to "
                    f"{', '.join(share.recipient_list) or '(no recipients)'}"
                )
            )

    def report(self, report):
        summary = report["summary"]
        for result in report["results"]:
            if result.sent:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Sent {result.title!r} to {result.recipients} recipient(s)"
                    )
                )
            else:
                self.stderr.write(
                    self.style.ERROR(f"Failed {result.title!r}: {result.error}")
                )
        self.stdout.write(
            self.style.SUCCESS(
                f"{summary['considered']} due, {summary['sent']} sent, "
                f"{summary['failed']} failed"
            )
        )
