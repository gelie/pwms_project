"""
Email referral reminders and close referrals whose deadline has passed.

Repointed from the legacy ``workflows`` app to the current ``pwms`` models: a
:class:`WorkflowReferral` carries ``due_date`` (not ``deadline``) and
``deadline_notified_at`` (not a boolean ``deadline_notified``), and an overdue
referral is closed with :meth:`WorkflowReferral.mark_expired` so the status
machine and its ``referral-expired`` event stay authoritative.

Mail goes out through the notification log (``pwms.notifications``), so a
reminder is recorded like every other alert — recipient, channel and delivery
outcome included. This command keeps the wording and the 24-hour / final-hour
windows; the dispatch layer decides who receives it and writes the record. An
expiry raises its own alert from ``mark_expired()``, so it is not sent twice.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.timesince import timeuntil

from pwms.models import WorkflowReferral
from pwms.notifications import notify_referral_deadline, referral_audience

#: How long before the due date each reminder goes out.
FIRST_REMINDER = timedelta(hours=24)
FINAL_REMINDER = timedelta(hours=1)


class Command(BaseCommand):
    help = (
        "Check referral deadlines: email approaching-deadline reminders and "
        "expire referrals whose deadline has passed"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be emailed or expired without changing anything",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN - no mail is sent and nothing is saved")
            )

        now = timezone.now()
        open_referrals = WorkflowReferral.objects.filter(
            status="open", due_date__isnull=False
        ).select_related("referred_to", "referred_by")

        # First reminder: inside the final day, not yet warned.
        first_warning = [
            referral
            for referral in open_referrals
            if now < referral.due_date <= now + FIRST_REMINDER
            and referral.deadline_notified_at is None
        ]
        # Second reminder: already warned once, now inside the final hour.
        final_warning = [
            referral
            for referral in open_referrals
            if now < referral.due_date <= now + FINAL_REMINDER
            and referral.deadline_notified_at is not None
        ]
        expired = [referral for referral in open_referrals if referral.due_date <= now]

        self.stdout.write(f"Found {len(first_warning)} referral(s) due within 24 hours")
        self.stdout.write(f"Found {len(final_warning)} referral(s) due within 1 hour")
        self.stdout.write(f"Found {len(expired)} expired referral(s)")

        for referral in first_warning:
            self.send_deadline_warning(referral, dry_run=dry_run)
            if not dry_run:
                referral.deadline_notified_at = now
                referral.save(update_fields=["deadline_notified_at"])

        for referral in final_warning:
            self.send_deadline_warning(referral, dry_run=dry_run)

        for referral in expired:
            self.expire_referral(referral, dry_run=dry_run)

        self.stdout.write(self.style.SUCCESS("Deadline check completed successfully"))

    def send_deadline_warning(self, referral, *, dry_run=False):
        """Email the referred group and the workflow owner about a due referral."""
        workflow = referral.content_object
        if workflow is None:
            self.stderr.write(
                self.style.WARNING(
                    f"Skipping referral {referral.public_id}: its workflow is gone."
                )
            )
            return

        # Derived from the due date rather than the tier that selected this
        # referral, so a first run inside the final hour reads "59 minutes"
        # instead of the "24 hours" the outer window would have implied.
        time_remaining = timeuntil(referral.due_date)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"  [DRY RUN] Would warn about '{workflow.title}' "
                    f"(due in {time_remaining})"
                )
            )
            return

        if not referral_audience(referral):
            self.stderr.write(
                self.style.WARNING(
                    f"Skipping referral {referral.public_id}: no recipients."
                )
            )
            return

        notify_referral_deadline(
            referral,
            subject=f"Referral Deadline Warning: {workflow.title}",
            message=self.deadline_warning_body(referral, time_remaining),
        )

    def expire_referral(self, referral, *, dry_run=False):
        """Close an overdue referral and tell the parties it expired."""
        workflow = referral.content_object
        if workflow is None:
            self.stderr.write(
                self.style.WARNING(
                    f"Skipping referral {referral.public_id}: its workflow is gone."
                )
            )
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"  [DRY RUN] Would expire the referral on '{workflow.title}'"
                )
            )
            return

        # mark_expired() owns the status change, emits the ``referral-expired``
        # event, and raises the alert to the parties (see pwms.notifications), so
        # the model — not this command — stays the source of truth for what an
        # expiry means.
        referral.mark_expired()

    def deadline_warning_body(self, referral, time_remaining):
        return (
            f"Dear {referral.referred_to.name} member,\n\n"
            f"The referral of '{referral.content_object.title}' expires in "
            f"{time_remaining}.\n\n"
            f"Deadline: {referral.due_date:%Y-%m-%d %H:%M}\n"
            f"Referred by: {referred_by_label(referral)}\n"
            f"Referred to: {referral.referred_to.name}\n\n"
            "Please respond before the deadline expires.\n\n"
            "Best regards,\n"
            "PWMS"
        )


def referred_by_label(referral):
    """Name of whoever raised the referral, tolerating the nullable FK."""
    referrer = referral.referred_by
    if referrer is None:
        return "PWMS"
    return referrer.get_full_name() or referrer.get_username()
