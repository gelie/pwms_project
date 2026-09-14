"""
Import group names from a CSV / text list into ``pwms.Group``.

The input file is read as blank-line-separated **sections**. The first
non-empty line of each section is treated as the section heading and skipped;
every following line is a name to import. Use ``--section`` to import a single
section only.

Usage:
    python manage.py import_groups "names.csv" --parent "Government of RSA" --type ministry
    python manage.py import_groups "names.csv" --parent "Government of RSA" --type ministry --section Ministries
    python manage.py import_groups "names.csv" --parent "Government of RSA" --type ministry --dry-run
"""

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.db.models import Q

from pwms.models import Group


def _clean(name: str) -> str:
    """Collapse runs of whitespace so trailing/duplicate spaces don't create dupes."""
    return " ".join(name.split())


class Command(BaseCommand):
    help = (
        "Import group names from a CSV/text file as Group records under a parent group"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "names_file",
            type=str,
            help="Path to the CSV/text file containing the group names",
        )
        parser.add_argument(
            "--parent",
            type=str,
            required=True,
            help="Name or slug of the parent group (e.g. 'Government of RSA')",
        )
        parser.add_argument(
            "--type",
            dest="group_type",
            type=str,
            required=True,
            help="group_type to assign to the imported groups (e.g. 'ministry')",
        )
        parser.add_argument(
            "--section",
            type=str,
            default=None,
            help="Only import names from this section heading (case-insensitive)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be imported without making any changes",
        )

    def handle(self, *args, **options):
        names_path = Path(options["names_file"])
        dry_run = options["dry_run"]
        parent_ref = options["parent"]
        group_type = options["group_type"]
        section_ref = options["section"]

        if not names_path.exists():
            raise CommandError(f"File not found: {names_path}")

        valid_types = {choice for choice, _ in Group.GROUP_TYPE_CHOICES}
        if group_type not in valid_types:
            raise CommandError(
                f"Invalid group type '{group_type}'. "
                f"Valid types: {', '.join(sorted(valid_types))}"
            )

        parent = self._resolve_parent(parent_ref)
        self.stdout.write(
            f"🌳 Parent: {parent.name} (slug={parent.slug}, id={parent.id})"
        )
        self.stdout.write(f"🏷️  Type:   {group_type}")
        self.stdout.write(f"📄 Reading names from: {names_path}")

        names = self._read_names(names_path, section_ref)
        if not names:
            self.stdout.write(self.style.WARNING("No names found to import."))
            return

        created_count = 0
        updated_count = 0
        skipped_count = 0

        for name in names:
            # Natural key is (name, parent), matching the model's unique constraint.
            existing = Group.objects.filter(name=name, parent=parent).first()

            if existing is not None:
                if existing.group_type != group_type:
                    if not dry_run:
                        with transaction.atomic():
                            existing.group_type = group_type
                            existing.save(update_fields=["group_type"])
                    updated_count += 1
                    self.stdout.write(
                        f"  🔄 Updated type: {name} "
                        f"({existing.group_type!r} -> {group_type!r})"
                    )
                else:
                    skipped_count += 1
                    self.stdout.write(f"  ⏭️  Skipped (exists): {name}")
                continue

            if dry_run:
                created_count += 1
                self.stdout.write(f"  ✅ Would create: {name}")
                continue

            try:
                with transaction.atomic():
                    Group.objects.create(
                        name=name,
                        group_type=group_type,
                        is_active=True,
                        parent=parent,
                    )
                created_count += 1
                self.stdout.write(f"  ✅ Created: {name}")
            except IntegrityError as exc:
                skipped_count += 1
                self.stdout.write(
                    self.style.WARNING(f"  ⚠️  Could not create {name}: {exc!s}")
                )

        self.stdout.write("")
        if dry_run:
            self.stdout.write(
                self.style.WARNING("🏁 DRY RUN — no changes were made. Summary:")
            )
        else:
            self.stdout.write(self.style.SUCCESS("✅ Import complete. Summary:"))

        self.stdout.write(f"   ✨ Created:  {created_count}")
        self.stdout.write(f"   🔄 Updated:  {updated_count}")
        self.stdout.write(f"   ⏭️  Skipped:  {skipped_count}")
        self.stdout.write(f"   📊 Total:    {len(names)}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING("\n💡 Run without --dry-run to apply these changes.")
            )

    def _resolve_parent(self, parent_ref: str) -> Group:
        matches = list(
            Group.objects.filter(
                Q(name__iexact=parent_ref) | Q(slug__iexact=parent_ref)
            )
        )
        if not matches:
            raise CommandError(
                f"Parent group '{parent_ref}' not found. Create it first."
            )
        if len(matches) > 1:
            options = ", ".join(f"{g.name} (id={g.id})" for g in matches)
            raise CommandError(
                f"Parent reference '{parent_ref}' is ambiguous: {options}. "
                f"Use a unique name or slug."
            )
        return matches[0]

    def _read_names(self, names_path: Path, section_ref: str | None) -> list[str]:
        """Parse blank-line-separated sections, skipping each section heading."""
        with open(names_path, newline="", encoding="utf-8-sig") as f:
            rows = [row for row in csv.reader(f)]

        sections: list[tuple[str, list[str]]] = []
        heading: str | None = None
        names: list[str] = []
        for row in rows:
            cells = [_clean(cell) for cell in row]
            line = next((cell for cell in cells if cell), "")
            if not line:
                if heading is not None or names:
                    sections.append((heading or "", names))
                heading, names = None, []
                continue
            if heading is None:
                heading = line  # first non-empty line of the block
            else:
                names.append(line)
        if heading is not None or names:
            sections.append((heading or "", names))

        if section_ref is not None:
            wanted = section_ref.casefold()
            matching = [n for h, n in sections if h.casefold() == wanted]
            if not matching:
                available = ", ".join(h or "(untitled)" for h, _ in sections)
                raise CommandError(
                    f"Section '{section_ref}' not found. Available sections: {available}"
                )
            names = matching[0]
        else:
            names = [name for _, section_names in sections for name in section_names]

        # De-duplicate while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for name in names:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique
