"""Delete API tokens whose absolute TTL has expired.

Mirrors ``pwms.api.authentication.ExpiringTokenAuthentication``: a token dies when
it is older than ``settings.TOKEN_TTL`` (measured from its ``created`` column). An
expired token already fails authentication, so this only reclaims rows — run it
from a systemd timer or cron, or ahead of a security review. ``--dry-run`` lists
what would go; ``--days`` overrides the configured TTL for a one-off.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from rest_framework.authtoken.models import Token

from pwms.api.authentication import resolve_token_ttl


class Command(BaseCommand):
    help = "Delete API tokens that have passed their TTL."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help=(
                "Override settings.TOKEN_TTL_DAYS for this run "
                "(0 disables expiry, so nothing is deleted)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the tokens that would be deleted without deleting them.",
        )

    def handle(self, *args, **options):
        ttl = resolve_token_ttl(options["days"])
        if ttl is None:
            self.stdout.write(
                self.style.WARNING(
                    "Token expiry is disabled (TOKEN_TTL_DAYS=0); nothing to purge."
                )
            )
            return

        cutoff = timezone.now() - ttl
        expired = Token.objects.filter(created__lt=cutoff).select_related("user")
        count = expired.count()

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(f"DRY RUN - {count} token(s) would be deleted")
            )
            for token in expired.order_by("created"):
                self.stdout.write(
                    f"  {token.user.get_username()} "
                    f"(created {token.created:%Y-%m-%d %H:%M})"
                )
            return

        expired.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} expired token(s)"))
