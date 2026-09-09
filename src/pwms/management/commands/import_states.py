"""
Django management command to import workflow states from a CSV file.

Usage:
    python manage.py import_states workflow_states/states_petition.csv --workflow-type Petition
    python manage.py import_states workflow_states/states_petition.csv --workflow-type Petition --dry-run
    python manage.py import_states workflow_states/states_int_resolution.csv --workflow-type "International Resolution"
"""

import csv
import uuid
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from django.db.models import Q

from workflows.models import State, WorkflowType


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

        # Look up the WorkflowType by name or slug
        try:
            workflow_type = WorkflowType.objects.get(
                Q(name__iexact=workflow_type_ref)
                | Q(slug__iexact=workflow_type_ref)
            )
        except WorkflowType.DoesNotExist:
            raise CommandError(
                f"WorkflowType '{workflow_type_ref}' not found. "
                f"Create it first in the admin or shell."
            )

        self.stdout.write(
            f"🔗 Workflow Type: {workflow_type.name} (slug={workflow_type.slug}, id={workflow_type.id})"
        )
        self.stdout.write(f"📄 Reading states from: {csv_path}")

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        if not rows:
            self.stdout.write(self.style.WARNING("No rows found in CSV."))
            return

        created_count = 0
        updated_count = 0
        skipped_count = 0

        for row in rows:
            name = row["name"]
            description = row.get("description", "")
            is_initial = row.get("is_initial", "false").lower() == "true"
            is_terminal = row.get("is_terminal", "false").lower() == "true"
            allows_referrals = row.get("allows_referrals", "true").lower() == "true"
            order = int(row.get("order", 0))
            color = row.get("color", "#5b8f22")

            # Optional explicit ID (for backwards compatibility with older CSVs).
            # If omitted or empty, Django will auto-generate a UUID7.
            state_id = row.get("id", "").strip() or None

            # Match by unique_together: workflow_type + name
            existing = State.objects.filter(
                workflow_type=workflow_type, name=name
            ).first()

            if existing:
                changed = False
                if state_id and existing.id != uuid.UUID(state_id):
                    existing.id = state_id
                    changed = True
                if existing.description != description:
                    existing.description = description
                    changed = True
                if existing.is_initial != is_initial:
                    existing.is_initial = is_initial
                    changed = True
                if existing.is_terminal != is_terminal:
                    existing.is_terminal = is_terminal
                    changed = True
                if existing.allows_referrals != allows_referrals:
                    existing.allows_referrals = allows_referrals
                    changed = True
                if existing.order != order:
                    existing.order = order
                    changed = True
                if existing.color != color:
                    existing.color = color
                    changed = True

                if changed:
                    if not dry_run:
                        existing.save()
                    updated_count += 1
                    self.stdout.write(
                        f"  🔄 Updated: {name} (id={existing.id})"
                    )
                else:
                    skipped_count += 1
                    self.stdout.write(
                        f"  ⏭️  Skipped (unchanged): {name} (id={existing.id})"
                    )
            else:
                kwargs = {
                    "workflow_type": workflow_type,
                    "name": name,
                    "description": description,
                    "is_initial": is_initial,
                    "is_terminal": is_terminal,
                    "allows_referrals": allows_referrals,
                    "order": order,
                    "color": color,
                }
                if state_id:
                    kwargs["id"] = state_id

                state = State(**kwargs)
                if not dry_run:
                    state.save()
                created_count += 1
                self.stdout.write(
                    f"  ✅ Created: {name} (id={state.id})"
                )

        # Summary
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
                self.style.WARNING(
                    "\n💡 Run without --dry-run to apply these changes."
                )
            )
