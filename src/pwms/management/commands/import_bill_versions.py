"""Bulk-import preserved bill versions from a CSV file into ``pwms.BillVersion``.

The CSV needs a header row. ``bill_number`` and ``version_label`` are the
natural key and are the only mandatory columns:

    bill_number,version_label,version_type,version_date,document_url,notes,is_current

* ``version_type`` accepts the slug or its label — ``introduced``, ``amended``,
  ``amendment_schedule`` / "Amendment schedule"; blank defaults to the
  introduced version.
* ``version_date`` is ISO ``YYYY-MM-DD``; blank defaults to today.
* ``is_current`` accepts ``1``/``true``/``t``/``yes``/``y``; anything else
  (including blank) means "not current".

Rows are upserted on ``(bill, version_label)``, so re-running after correcting
the file is safe: matching rows are skipped, changed rows are updated and new
rows are created. **Nothing is ever deleted** — historical versions are
preserved, per the BRS version-integrity rule (BRS §15A).

Rows that cannot be imported (unknown ``bill_number``, unknown version type,
unparseable date) are reported and skipped, and the command finishes with a
summary.

Usage:
    python manage.py import_bill_versions versions.csv
    python manage.py import_bill_versions versions.csv --dry-run
"""

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from pwms.models import Bill, BillVersion

#: Columns the file must provide; the rest are optional.
REQUIRED_COLUMNS = ("bill_number", "version_label")

#: Whole header expected in the file, for error messages.
EXPECTED_COLUMNS = (
    "bill_number",
    "version_label",
    "version_type",
    "version_date",
    "document_url",
    "notes",
    "is_current",
)

TRUTHY = {"1", "true", "t", "yes", "y"}


def _truthy(value: str) -> bool:
    return (value or "").strip().casefold() in TRUTHY


def _version_type(raw: str) -> str:
    """Map a CSV value (slug or label, any case) to a version-type slug."""
    cleaned = (raw or "").strip().casefold()
    if not cleaned:
        return BillVersion.INTRODUCED
    for slug, label in BillVersion.VERSION_TYPE_CHOICES:
        if cleaned in {slug.casefold(), str(label).casefold()}:
            return slug
    valid = ", ".join(slug for slug, _ in BillVersion.VERSION_TYPE_CHOICES)
    raise ValueError(f"unknown version type '{raw}' (expected one of: {valid})")


class Command(BaseCommand):
    help = "Bulk-import preserved bill versions from a CSV file"

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=str, help="Path to the CSV file")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be imported without making any changes",
        )

    def handle(self, *args, **options):
        path = Path(options["csv_file"])
        dry_run = options["dry_run"]

        if not path.exists():
            raise CommandError(f"File not found: {path}")

        rows = self._read_rows(path)
        self.stdout.write(f"📄 Reading versions from: {path}")
        if dry_run:
            self.stdout.write(
                self.style.WARNING("🧪 Dry run — nothing will be written.")
            )

        bill_cache: dict[str, Bill | None] = {}
        created = updated = skipped = 0
        errors: list[str] = []

        for lineno, data in rows:
            result = self._import_row(data, lineno, bill_cache, dry_run, errors)
            created += result == "created"
            updated += result == "updated"
            skipped += result == "skipped"

        self.stdout.write("")
        for error in errors:
            self.stdout.write(self.style.WARNING(f"  ⚠️  {error}"))

        if dry_run:
            self.stdout.write(self.style.WARNING("🏁 DRY RUN — no changes were made."))
        else:
            self.stdout.write(self.style.SUCCESS("✅ Import complete."))
        self.stdout.write(f"   ✨ Created:  {created}")
        self.stdout.write(f"   🔄 Updated:  {updated}")
        self.stdout.write(f"   ⏭️  Skipped:  {skipped}")
        self.stdout.write(f"   ⚠️  Errors:   {len(errors)}")

    def _read_rows(self, path: Path) -> list[tuple[int, dict[str, str]]]:
        """Return ``(line number, normalised row)`` pairs from the CSV."""
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fieldnames = [(name or "").strip() for name in (reader.fieldnames or [])]
            missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
            if missing:
                raise CommandError(
                    f"{path} is missing required column(s): {', '.join(missing)}.\n"
                    f"Expected header: {', '.join(EXPECTED_COLUMNS)}"
                )
            return [
                (
                    lineno,
                    {
                        (key or "").strip(): (value or "").strip()
                        for key, value in row.items()
                        if key is not None
                    },
                )
                for lineno, row in enumerate(reader, start=2)
            ]

    def _import_row(
        self,
        data: dict[str, str],
        lineno: int,
        bill_cache: dict[str, Bill | None],
        dry_run: bool,
        errors: list[str],
    ) -> str:
        """Import one CSV row; returns ``created`` / ``updated`` / ``skipped``."""
        bill_number = data.get("bill_number", "")
        version_label = data.get("version_label", "")
        if not bill_number or not version_label:
            errors.append(
                f"line {lineno}: bill_number and version_label are both required"
            )
            return "skipped"

        cache_key = bill_number.casefold()
        if cache_key not in bill_cache:
            bill_cache[cache_key] = Bill.objects.filter(
                bill_number__iexact=bill_number
            ).first()
        bill = bill_cache[cache_key]
        if bill is None:
            errors.append(f"line {lineno}: no bill with number '{bill_number}'")
            return "skipped"

        try:
            version_type = _version_type(data.get("version_type", ""))
        except ValueError as exc:
            errors.append(f"line {lineno}: {exc}")
            return "skipped"

        raw_date = data.get("version_date", "")
        version_date = parse_date(raw_date) if raw_date else timezone.localdate()
        if version_date is None:
            errors.append(
                f"line {lineno}: invalid version_date '{raw_date}' "
                f"(expected YYYY-MM-DD)"
            )
            return "skipped"

        defaults = {
            "version_type": version_type,
            "version_date": version_date,
            "document_url": data.get("document_url", ""),
            "notes": data.get("notes", ""),
            "is_current": _truthy(data.get("is_current", "")),
        }

        existing = BillVersion.objects.filter(
            bill=bill, version_label=version_label
        ).first()
        if existing is None:
            if dry_run:
                self.stdout.write(
                    f"  ✅ Would create: {bill.bill_number} — {version_label}"
                )
                return "created"
            with transaction.atomic():
                BillVersion.objects.create(
                    bill=bill, version_label=version_label, **defaults
                )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  ✅ Created: {bill.bill_number} — {version_label}"
                )
            )
            return "created"

        changed = {
            field: value
            for field, value in defaults.items()
            if getattr(existing, field) != value
        }
        if not changed:
            self.stdout.write(f"  ⏭️  Skipped (unchanged): {version_label}")
            return "skipped"

        changed_fields = ", ".join(sorted(changed))
        if dry_run:
            self.stdout.write(
                f"  🔄 Would update: {bill.bill_number} — {version_label} "
                f"({changed_fields})"
            )
            return "updated"
        with transaction.atomic():
            for field, value in changed.items():
                setattr(existing, field, value)
            existing.save(update_fields=[*changed, "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(
                f"  🔄 Updated: {bill.bill_number} — {version_label} ({changed_fields})"
            )
        )
        return "updated"
