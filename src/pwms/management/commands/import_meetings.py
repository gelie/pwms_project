"""Bulk-import meetings from a CSV/XLSX file in the calendar's import format.

This is the command-line front end to
:func:`pwms.services.meetings_io.import_meetings`, the same function the
*Import* page uses, so a file behaves identically whichever way it is fed in: the
columns are documented in ``docs/Meetings Import & Export.md``, rows **upsert on**
``Reference``, a bad row is reported and skipped (the rest still imports), a
blank cell leaves a field untouched, and nothing is ever deleted or emailed.

New meetings need an owner. ``--actor`` names the user they are owned by and
attributed to; without it the command uses the first superuser.

Usage:
    python manage.py import_meetings meetings.csv
    python manage.py import_meetings meetings.xlsx --dry-run
    python manage.py import_meetings meetings.csv --actor jsmith
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from pwms.models import User
from pwms.services.meetings_io import ImportFormatError, import_meetings


class Command(BaseCommand):
    help = "Bulk-import meetings from a CSV or XLSX file (upsert on Reference)"

    def add_arguments(self, parser):
        parser.add_argument("file", type=str, help="Path to the .csv or .xlsx file")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be imported without making any changes",
        )
        parser.add_argument(
            "--actor",
            default="",
            help=(
                "Username the new meetings are owned by and attributed to "
                "(default: the first superuser)"
            ),
        )

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        dry_run = options["dry_run"]
        actor = self._resolve_actor(options["actor"])

        self.stdout.write(f"📄 Reading meetings from: {path}")
        self.stdout.write(f"👤 Acting as: {actor.username}")
        if dry_run:
            self.stdout.write(
                self.style.WARNING("🧪 Dry run — nothing will be written.")
            )

        with path.open("rb") as handle:
            try:
                report = import_meetings(
                    handle,
                    actor=actor,
                    filename=str(path),
                    dry_run=dry_run,
                )
            except ImportFormatError as error:
                raise CommandError(str(error)) from None

        self._summarise(report)

    def _resolve_actor(self, username):
        """The user to attribute the import to: ``--actor`` or the first superuser."""
        if username:
            user = User.objects.filter(username__iexact=username).first()
            if user is None:
                raise CommandError(f"No user with username '{username}'.")
            return user
        actor = User.objects.filter(is_superuser=True).order_by("pk").first()
        if actor is None:
            raise CommandError(
                "No superuser to attribute the import to — pass --actor <username>."
            )
        return actor

    def _summarise(self, report):
        self.stdout.write("")
        for error in report.errors:
            self.stdout.write(self.style.WARNING(f"  ⚠️  {error}"))

        if report.dry_run:
            self.stdout.write(self.style.WARNING("🏁 DRY RUN — no changes were made."))
        else:
            self.stdout.write(self.style.SUCCESS("✅ Import complete."))
        self.stdout.write(f"   ✨ Created:  {report.created}")
        self.stdout.write(f"   🔄 Updated:  {report.updated}")
        self.stdout.write(f"   ⏭️  Skipped:  {report.skipped}")
        self.stdout.write(f"   ⚠️  Errors:   {len(report.errors)}")
