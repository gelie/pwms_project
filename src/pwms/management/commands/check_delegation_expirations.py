import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from workflows.models import UserDelegation

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Check for expired delegations and expire them with notifications"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be expired without actually expiring",
        )
        parser.add_argument(
            "--delegation-id",
            type=str,
            help="Check specific delegation UUID only",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        delegation_id = options["delegation_id"]

        self.stdout.write("🔍 Checking for expired delegations...")

        # Build query for active delegations that should be expired
        queryset = UserDelegation.objects.filter(status="active")

        if delegation_id:
            queryset = queryset.filter(id=delegation_id)
            self.stdout.write(f"📋 Checking specific delegation ID: {delegation_id}")

        # Find delegations where end_date has passed
        now = timezone.now()
        expired_delegations = queryset.filter(end_date__lte=now)

        if not expired_delegations.exists():
            self.stdout.write(self.style.SUCCESS("✅ No delegations need expiration"))
            return

        self.stdout.write(
            f"📅 Found {expired_delegations.count()} delegation(s) to expire:"
        )

        expired_count = 0
        notifications_created = 0
        emails_sent = 0

        for delegation in expired_delegations:
            delegator_name = (
                delegation.delegator.get_full_name() or delegation.delegator.username
            )
            delegatee_name = (
                delegation.delegatee.get_full_name() or delegation.delegatee.username
            )

            self.stdout.write(
                f"  • {delegator_name} → {delegatee_name} "
                f"(expired: {delegation.end_date.strftime('%Y-%m-%d %H:%M')})"
            )

            if dry_run:
                self.stdout.write("    [DRY RUN] Would expire and send notifications")
                continue

            try:
                # Expire the delegation (this sends notifications)
                delegation.expire_delegation()
                expired_count += 1

                # Count notifications created (2: one for delegatee, one for delegator)
                notifications_created += 2

                # Count emails sent (2: one for delegatee, one for delegator)
                emails_sent += 2

                self.stdout.write(
                    self.style.SUCCESS("    ✓ Expired and notifications sent")
                )

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"    ✗ Failed to expire: {str(e)}"))
                logger.error(f"Failed to expire delegation {delegation.pk}: {e}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"\n[DRY RUN] Would have expired {expired_delegations.count()} delegation(s)"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "\n✅ Delegation expiration check complete:\n"
                    f"   • Delegations expired: {expired_count}\n"
                    f"   • Notifications created: {notifications_created}\n"
                    f"   • Emails sent: {emails_sent}"
                )
            )
