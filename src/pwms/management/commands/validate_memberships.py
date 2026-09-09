"""
Django management command to validate GroupMembership data integrity.

This command performs comprehensive validation of membership data to detect
issues like group name collisions, incorrect assignments, and data integrity problems.

Usage:
    python manage.py validate_memberships
    python manage.py validate_memberships --fix-collisions
    python manage.py validate_memberships --report-file=validation_report.json
"""

import json
from time import perf_counter
from django.core.management.base import BaseCommand, CommandError

from workflows.membership.validators import MembershipValidator, MembershipSyncValidator


class Command(BaseCommand):
    help = "Validate GroupMembership data integrity and correctness"

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix-collisions",
            action="store_true",
            help="Attempt to fix detected group name collisions",
        )
        parser.add_argument(
            "--report-file",
            type=str,
            help="Save validation report to JSON file",
        )
        parser.add_argument(
            "--sync-readiness",
            action="store_true",
            help="Check if system is ready for sync operations",
        )
        parser.add_argument(
            "--suggest-mapping",
            type=str,
            help="Suggest group mapping for Oracle organization name",
        )

    def handle(self, *args, **options):
        """Main command handler."""
        start_time = perf_counter()

        if options["sync_readiness"]:
            self._check_sync_readiness()
            return

        if options["suggest_mapping"]:
            self._suggest_mapping(options["suggest_mapping"])
            return

        # Run comprehensive validation
        validator = MembershipValidator()
        report = validator.validate_all()

        # Print report
        validator.print_report(report)

        # Save report to file if requested
        if options["report_file"]:
            self._save_report(report, options["report_file"])

        # Attempt to fix collisions if requested
        if options["fix_collisions"] and report["stats"].get("group_collisions", 0) > 0:
            self._attempt_collision_fix()

        # Exit with appropriate code
        duration = perf_counter() - start_time
        self.stdout.write(f"\n⏱️  Validation completed in {duration:.2f} seconds")

        if report["summary"]["total_errors"] > 0:
            raise CommandError("❌ Validation failed with errors")
        elif report["summary"]["total_warnings"] > 0:
            self.stdout.write(
                self.style.WARNING("⚠️  Validation completed with warnings")
            )
        else:
            self.stdout.write(self.style.SUCCESS("✅ All validation checks passed"))

    def _check_sync_readiness(self):
        """Check if system is ready for sync operations."""
        self.stdout.write("🔍 Checking sync readiness...")

        validator = MembershipSyncValidator()
        is_ready = validator.validate_sync_readiness()

        if is_ready:
            self.stdout.write(
                self.style.SUCCESS("✅ System is ready for sync operations")
            )
        else:
            self.stdout.write(
                self.style.ERROR("❌ System is not ready for sync operations")
            )
            raise CommandError("Sync readiness check failed")

    def _suggest_mapping(self, oracle_org_name: str):
        """Suggest group mapping for Oracle organization name."""
        self.stdout.write(f"🔍 Suggesting mappings for: '{oracle_org_name}'")

        validator = MembershipSyncValidator()
        suggestions = validator.suggest_oracle_mapping(oracle_org_name)

        self.stdout.write("\n📋 Suggested mappings:")
        for suggestion in suggestions:
            self.stdout.write(f"   • {suggestion}")

    def _save_report(self, report: dict, filename: str):
        """Save validation report to JSON file."""
        try:
            with open(filename, "w") as f:
                json.dump(report, f, indent=2, default=str)
            self.stdout.write(f"📁 Report saved to: {filename}")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Failed to save report: {str(e)}"))

    def _attempt_collision_fix(self):
        """Attempt to fix group name collisions."""
        self.stdout.write("\n🔧 Attempting to fix group name collisions...")
        self.stdout.write(
            self.style.WARNING(
                "⚠️  Automatic collision fixing is not implemented yet. "
                "Please review the warnings and manually resolve collisions."
            )
        )
