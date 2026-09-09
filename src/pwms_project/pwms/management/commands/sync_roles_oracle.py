"""
Django management command to sync role definitions from Oracle database.

This command fetches unique position descriptions from Oracle and creates
Role records with proper normalization and sanitization.

Usage:
    python manage.py sync_roles_oracle
    python manage.py sync_roles_oracle --dry-run
    python manage.py sync_roles_oracle --verbose
"""

from time import perf_counter
from typing import Set

from django.core.management.base import CommandError

from workflows.management.commands.sync_base import OracleSyncBase
from workflows.models import Role


class Command(OracleSyncBase):
    help = "Sync role definitions from Oracle database"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = self.setup_logging("oracle_role_sync")
        self.stats.update(
            {
                "roles_fetched": 0,
                "new_roles": 0,
                "unmapped_positions": 0,
            }
        )
        self._role_cache = {}
        self._unmapped_positions = set()

    def add_arguments(self, parser):
        self.add_common_arguments(parser)
        parser.add_argument(
            "--report-unmapped",
            action="store_true",
            help="Generate report of unmapped positions",
        )

    def handle(self, *args, **options):
        """Main command handler."""
        self.stats["start_time"] = perf_counter()
        self.validate_environment()

        if options["verbose"]:
            self.logger.setLevel(10)
            for handler in self.logger.handlers:
                handler.setLevel(10)

        dry_run = options["dry_run"]
        report_unmapped = options["report_unmapped"]

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "🔍 DRY RUN MODE - No changes will be made to the database"
                )
            )
            self.logger.info("Starting Oracle role sync in DRY RUN mode")
        else:
            self.logger.info("Starting Oracle role sync")

        try:
            self.connect_to_oracle()
            self.sync_roles(dry_run)

            if report_unmapped and self._unmapped_positions:
                self.print_unmapped_report()

            if self.stats["errors"] == 0:
                self.stdout.write(
                    self.style.SUCCESS("✅ Role sync completed successfully")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"⚠️  Role sync completed with {self.stats['errors']} errors"
                    )
                )

        except Exception as e:
            self.logger.error(f"Role sync failed: {str(e)}", exc_info=True)
            self.stats["errors"] += 1
            raise CommandError(f"❌ Role sync failed: {str(e)}")

        finally:
            self.cleanup_connections()
            self.stats["end_time"] = perf_counter()
            self.print_summary()

    def sync_roles(self, dry_run: bool):
        """Fetch and sync roles from Oracle."""
        self.logger.info("👥 Fetching role definitions from Oracle...")

        roles_data = self.fetch_oracle_roles()
        self.stats["roles_fetched"] = len(roles_data)

        self.logger.info(f"📊 Fetched {len(roles_data)} unique position descriptions")

        self.create_standard_roles(dry_run)
        self.create_position_roles(roles_data, dry_run)

        self.logger.info("✅ Role synchronization completed")

    def fetch_oracle_roles(self) -> Set[str]:
        """Fetch unique position descriptions from Oracle.

        Returns:
            Set of normalized role names
        """
        query = """
            SELECT DISTINCT POSITIONDESC, EMPLOYEETYPE
            FROM APPS.XXPER_PEOPLE_INTERFACE
            WHERE CURRENT_EMPLOYEE_FLAG = 'Y'
            AND ASSIGNMENT_STATUS = 'Active Assignment'
            ORDER BY POSITIONDESC
        """

        try:
            self.oracle_cursor.execute(query)
            rows = self.oracle_cursor.fetchall()

            roles_data = set()
            for row in rows:
                positiondesc = row[0] if row[0] else ""
                employeetype = row[1] if row[1] else ""

                normalized_role = self.normalize_role_name(positiondesc, employeetype)
                roles_data.add(normalized_role)

            return roles_data

        except Exception as e:
            self.logger.error(f"Failed to fetch roles from Oracle: {str(e)}")
            raise

    def create_standard_roles(self, dry_run: bool):
        """Create standard parliamentary roles."""
        standard_roles = {
            "Member of Parliament": "Elected Member of Parliament",
            "Staff Member": "General Parliamentary Staff Member",
            "Committee Chairperson": "Chairperson of a Parliamentary Committee",
            "Committee Secretary": "Secretary to a Parliamentary Committee",
            "Researcher": "Parliamentary Research Officer",
            "Protection Officer": "Parliamentary Protection Services Officer",
            "Desktop Technician": "ICT Desktop Support Technician",
        }

        for role_name, description in standard_roles.items():
            self._ensure_role(role_name, description, dry_run)

    def create_position_roles(self, roles_data: Set[str], dry_run: bool):
        """Create roles from Oracle position descriptions."""
        self.logger.info(f"🔄 Processing {len(roles_data)} role definitions...")

        for role_name in sorted(roles_data):
            if role_name in ["Member of Parliament", "Staff Member"]:
                continue

            description = f"Role for {role_name}"
            self._ensure_role(role_name, description, dry_run)

        self.logger.info(f"✅ Processed {len(roles_data)} role definitions")

    def _ensure_role(self, role_name: str, description: str, dry_run: bool):
        """Ensure a role exists, creating it if necessary."""
        if role_name in self._role_cache:
            return self._role_cache[role_name]

        if dry_run:
            self.logger.info(f"🔍 [DRY RUN] Would create role: {role_name}")
            role = Role(name=role_name, description=description)
            role.id = -1
            self._role_cache[role_name] = role
            return role

        role, created = Role.objects.get_or_create(
            name=role_name,
            defaults={"description": description},
        )

        if created:
            self.stats["new_roles"] += 1
            self.logger.info(f"✨ Created role: {role_name}")

        self._role_cache[role_name] = role
        return role

    def print_unmapped_report(self):
        """Print report of unmapped positions."""
        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(self.style.WARNING("⚠️  UNMAPPED POSITIONS REPORT"))
        self.stdout.write("=" * 80)

        if self._unmapped_positions:
            self.stdout.write(
                f"\nFound {len(self._unmapped_positions)} unmapped positions:"
            )
            for position in sorted(self._unmapped_positions):
                self.stdout.write(f"   • {position}")
            self.stdout.write(
                "\nConsider adding these to the role mapping in sync_base.py"
            )
        else:
            self.stdout.write("\n✅ All positions are mapped")

    def print_summary(self):
        """Print summary of role sync operation."""
        duration = self.stats["end_time"] - self.stats["start_time"]

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(self.style.SUCCESS("👥 ORACLE ROLE SYNC COMPLETED"))
        self.stdout.write("=" * 80)

        self.stdout.write(f"⏱️  Duration: {duration:.2f} seconds")

        self.stdout.write("\n📊 ROLE STATISTICS:")
        self.stdout.write(
            f"   Roles fetched from Oracle: {self.stats['roles_fetched']}"
        )
        self.stdout.write(f"   New roles created: {self.stats['new_roles']}")
        self.stdout.write(f"   Unmapped positions: {self.stats['unmapped_positions']}")

        if self.stats["errors"] > 0:
            self.stdout.write(
                "\n" + self.style.ERROR(f"❌ ERRORS: {self.stats['errors']}")
            )
            self.stdout.write("   Check the log file for detailed error information.")

        if self.stats["warnings"] > 0:
            self.stdout.write(
                "\n" + self.style.WARNING(f"⚠️  WARNINGS: {self.stats['warnings']}")
            )

        if self.stats["errors"] == 0 and self.stats["warnings"] == 0:
            self.stdout.write(
                "\n" + self.style.SUCCESS("✅ No errors or warnings encountered")
            )

        self.stdout.write("\n📁 Log file: logs/oracle_role_sync.log")
        self.stdout.write("=" * 80)
