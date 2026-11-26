"""
Django management command to import workflow states from a CSV file.

Each row is upserted on ``(workflow type, name)``. The optional
``public_status`` column maps the state to a :class:`PublicStatus` by slug or
name; when it is absent, ``--default-public-status`` is used (``New`` by
default). A missing colour gets the standard one for the state's role
(``pwms.state_colors``).

Suggested columns::

    name,description,is_initial,is_terminal,allows_referrals,order,color,public_status
    Draft,Being prepared,true,false,true,0,,New

Usage:
    python manage.py import_states workflow_states/states_petition.csv --workflow-type Petition
    python manage.py import_states workflow_states/states_petition.csv --workflow-type Petition --dry-run
    python manage.py import_states workflow_states/states_int_resolution.csv --workflow-type "International Resolution"
"""

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from pwms.models import PublicStatus, State, WorkflowType
from pwms.state_colors import state_color


class Command(BaseCommand):
    help = "Import workflow states from a CSV file"

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_file",
            type=str,
            help="Path to the CSV file containing state definitions",
        )
        parser.add_argument(
            "--workflow-type",
            type=str,
            required=True,
            help="WorkflowType name or slug to associate states with (e.g. 'Petition')",
        )
        parser.add_argument(
            "--default-public-status",
            type=str,
            default="New",
            help=(
                "PublicStatus (slug or name) for rows without a public_status "
                "column (default: New)"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse the CSV and show what would be imported without making changes",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv_file"])
        dry_run = options["dry_run"]
        workflow_type_ref = options["workflow_type"]

        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        workflow_type = self._resolve_workflow_type(workflow_type_ref)
        self.stdout.write(
            f"🔗 Workflow Type: {workflow_type.name} "
            f"(slug={workflow_type.slug}, id={workflow_type.id})"
        )
        self.stdout.write(f"📄 Reading states from: {csv_path}")

        default_public_status = self._resolve_public_status(
            options["default_public_status"], "default public status"
        )
        public_statuses = {default_public_status.slug.casefold(): default_public_status}

        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        if not rows:
            self.stdout.write(self.style.WARNING("No rows found in CSV."))
            return

        created_count = 0
        updated_count = 0
        skipped_count = 0

        for line_number, row in enumerate(rows, start=2):
            name = (row.get("name") or "").strip()
            if not name:
                raise CommandError(f"Row {line_number}: 'name' is required.")

            is_initial = self._bool(row.get("is_initial"), default=False)
            is_terminal = self._bool(row.get("is_terminal"), default=False)
            allows_referrals = self._bool(row.get("allows_referrals"), default=True)
            order = self._int(row.get("order"), default=0, line_number=line_number)
            color = (row.get("color") or "").strip() or state_color(
                is_initial=is_initial, is_terminal=is_terminal, name=name
            )
            status_ref = (
                row.get("public_status")
                or row.get("public_status_slug")
                or row.get("public_status_name")
                or ""
            ).strip()
            public_status = (
                self._resolve_public_status(
                    status_ref, f"row {line_number}", cache=public_statuses
                )
                if status_ref
                else default_public_status
            )

            fields = {
                "name": name,
                "public_status": public_status,
                "description": row.get("description", ""),
                "is_initial": is_initial,
                "is_terminal": is_terminal,
                "allows_referrals": allows_referrals,
                "order": order,
                "color": color,
            }

            existing = State.objects.filter(
                workflow_type=workflow_type, name=name
            ).first()

            if existing:
                if any(
                    getattr(existing, field) != value for field, value in fields.items()
                ):
                    for field, value in fields.items():
                        setattr(existing, field, value)
                    if not dry_run:
                        existing.save()
                    updated_count += 1
                    self.stdout.write(f"  🔄 Updated: {name} (id={existing.id})")
                else:
                    skipped_count += 1
                    self.stdout.write(
                        f"  ⏭️  Skipped (unchanged): {name} (id={existing.id})"
                    )
                continue

            state = State(workflow_type=workflow_type, **fields)
            if not dry_run:
                state.save()
            created_count += 1
            self.stdout.write(f"  ✅ Created: {name} (id={state.id})")

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
        self.stdout.write(f"   📊 Total:    {len(rows)}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING("\n💡 Run without --dry-run to apply these changes.")
            )

    @staticmethod
    def _resolve_workflow_type(ref: str) -> WorkflowType:
        try:
            return WorkflowType.objects.get(Q(name__iexact=ref) | Q(slug__iexact=ref))
        except WorkflowType.DoesNotExist:
            raise CommandError(
                f"WorkflowType '{ref}' not found. Create it first in the admin or shell."
            )
        except WorkflowType.MultipleObjectsReturned:
            raise CommandError(
                f"Multiple WorkflowTypes match '{ref}'. Use a unique name or slug."
            )

    @staticmethod
    def _resolve_public_status(ref: str, where: str, *, cache=None) -> PublicStatus:
        key = (ref or "").strip()
        if cache is not None and key.casefold() in cache:
            return cache[key.casefold()]
        status = PublicStatus.objects.filter(
            Q(slug__iexact=key) | Q(name__iexact=key)
        ).first()
        if status is None:
            raise CommandError(
                f"PublicStatus '{ref}' not found ({where}). Create it first."
            )
        if cache is not None:
            cache[key.casefold()] = status
            cache[status.slug.casefold()] = status
        return status

    @staticmethod
    def _bool(value, *, default: bool) -> bool:
        text = (value or "").strip().lower()
        if not text:
            return default
        return text in {"1", "true", "t", "yes", "y"}

    @staticmethod
    def _int(value, *, default: int, line_number: int) -> int:
        text = (value or "").strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            raise CommandError(
                f"Row {line_number}: 'order' must be an integer, got {value!r}."
            )
