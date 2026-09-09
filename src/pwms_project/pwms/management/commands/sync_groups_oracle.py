"""
Django management command to sync organizational groups from Oracle database.

This command fetches organizational structure from Oracle and creates/updates
Group records with proper hierarchy using MPTT. Group names are sanitized by
removing numeric code prefixes.

Usage:
    python manage.py sync_groups_oracle
    python manage.py sync_groups_oracle --dry-run
    python manage.py sync_groups_oracle --verbose
"""

from time import perf_counter
from typing import Dict, Set, Tuple

from django.core.management.base import CommandError

from workflows.management.commands.sync_base import OracleSyncBase
from workflows.models import Group


class Command(OracleSyncBase):
    help = "Sync organizational groups from Oracle database"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = self.setup_logging("oracle_group_sync")
        self.stats.update(
            {
                "groups_fetched": 0,
                "new_groups": 0,
                "updated_groups": 0,
                "duplicate_names": 0,
            }
        )
        self._group_cache = {}

    def add_arguments(self, parser):
        self.add_common_arguments(parser)

    def handle(self, *args, **options):
        """Main command handler."""
        self.stats["start_time"] = perf_counter()
        self.validate_environment()

        if options["verbose"]:
            self.logger.setLevel(10)
            for handler in self.logger.handlers:
                handler.setLevel(10)

        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "🔍 DRY RUN MODE - No changes will be made to the database"
                )
            )
            self.logger.info("Starting Oracle group sync in DRY RUN mode")
        else:
            self.logger.info("Starting Oracle group sync")

        try:
            self.connect_to_oracle()
            self.sync_groups(dry_run)

            if self.stats["errors"] == 0:
                self.stdout.write(
                    self.style.SUCCESS("✅ Group sync completed successfully")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"⚠️  Group sync completed with {self.stats['errors']} errors"
                    )
                )

        except Exception as e:
            self.logger.error(f"Group sync failed: {str(e)}", exc_info=True)
            self.stats["errors"] += 1
            raise CommandError(f"❌ Group sync failed: {str(e)}")

        finally:
            self.cleanup_connections()
            self.stats["end_time"] = perf_counter()
            self.print_summary()

    def sync_groups(self, dry_run: bool):
        """Fetch and sync organizational groups from Oracle."""
        self.logger.info("🏛️  Fetching organizational groups from Oracle...")

        groups_data = self.fetch_oracle_groups()
        self.stats["groups_fetched"] = len(groups_data)

        self.logger.info(f"📊 Fetched {len(groups_data)} unique organizational units")

        parliament_group = self.ensure_parliament_root(dry_run)
        self.create_main_groups(parliament_group, dry_run)
        self.create_organizational_groups(groups_data, parliament_group, dry_run)

        self.logger.info("✅ Group synchronization completed")

    def fetch_oracle_groups(self) -> Set[Tuple[str, str]]:
        """Fetch unique organizational groups from Oracle.

        Returns:
            Set of (child_org_name, parent_org_name) tuples
        """
        query = """
            SELECT DISTINCT CHILD_ORG_NAME, PARENT_ORG_NAME
            FROM APPS.XXPER_PEOPLE_INTERFACE
            WHERE CURRENT_EMPLOYEE_FLAG = 'Y'
            AND ASSIGNMENT_STATUS = 'Active Assignment'
            AND CHILD_ORG_NAME IS NOT NULL
            ORDER BY PARENT_ORG_NAME, CHILD_ORG_NAME
        """

        try:
            self.oracle_cursor.execute(query)
            rows = self.oracle_cursor.fetchall()

            groups_data = set()
            for row in rows:
                child_org = row[0] if row[0] else ""
                parent_org = row[1] if row[1] else ""

                if child_org or parent_org:
                    groups_data.add((child_org, parent_org))

            return groups_data

        except Exception as e:
            self.logger.error(f"Failed to fetch groups from Oracle: {str(e)}")
            raise

    def ensure_parliament_root(self, dry_run: bool) -> Group:
        """Ensure the root Parliament group exists."""
        if dry_run:
            self.logger.info("🔍 [DRY RUN] Would ensure Parliament root group exists")
            parliament = Group(
                name="Parliament",
                short_name="Parliament",
                group_type="parliament",
                description="Parliament of the Republic of South Africa",
                is_active=True,
            )
            parliament.id = -1
            return parliament

        parliament, created = Group.objects.get_or_create(
            name="Parliament",
            defaults={
                "short_name": "Parliament",
                "group_type": "parliament",
                "description": "Parliament of the Republic of South Africa",
                "is_active": True,
            },
        )
        if created:
            self.stats["new_groups"] += 1
            self.logger.info("✨ Created Parliament root group")

        return parliament

    def create_main_groups(self, parliament: Group, dry_run: bool):
        """Create the three main parliamentary groups."""
        main_groups = [
            {
                "name": "National Assembly",
                "short_name": "NA",
                "group_type": "house",
                "description": "National Assembly of Parliament",
            },
            {
                "name": "National Council of Provinces",
                "short_name": "NCOP",
                "group_type": "house",
                "description": "National Council of Provinces",
            },
            {
                "name": "Parliament Staff",
                "short_name": "Staff",
                "group_type": "administration",
                "description": "Parliamentary Staff Members",
            },
        ]

        for group_data in main_groups:
            if dry_run:
                self.logger.info(
                    f"🔍 [DRY RUN] Would ensure group exists: {group_data['name']}"
                )
                continue

            group, created = Group.objects.get_or_create(
                name=group_data["name"],
                defaults={
                    **group_data,
                    "parent": parliament if parliament.id != -1 else None,
                    "is_active": True,
                },
            )
            if created:
                self.stats["new_groups"] += 1
                self.logger.info(f"✨ Created main group: {group_data['name']}")

    def create_organizational_groups(
        self, groups_data: Set[Tuple[str, str]], parliament: Group, dry_run: bool
    ):
        """Create organizational groups with parent-child hierarchy."""
        self.logger.info(
            f"🔄 Processing {len(groups_data)} organizational group relationships..."
        )

        seen_names = {}

        for child_org, parent_org in sorted(groups_data):
            child_name = self.strip_group_code_prefix(child_org)
            parent_name = self.strip_group_code_prefix(parent_org)

            if not child_name and not parent_name:
                continue

            if parent_name:
                self._ensure_group(parent_name, None, "division", dry_run, seen_names)

            if child_name:
                parent_group_name = parent_name if parent_name else None
                self._ensure_group(
                    child_name, parent_group_name, "section", dry_run, seen_names
                )

        self.logger.info(
            f"✅ Processed {len(groups_data)} organizational relationships"
        )

    def _ensure_group(
        self,
        name: str,
        parent_name: str,
        group_type: str,
        dry_run: bool,
        seen_names: Dict[str, str],
    ):
        """Ensure a group exists, creating it if necessary."""
        cache_key = f"{group_type}::{name}"

        if cache_key in self._group_cache:
            return self._group_cache[cache_key]

        if name in seen_names:
            if seen_names[name] != (parent_name or ""):
                self.logger.warning(
                    f"⚠️  Duplicate group name with different parent: '{name}' "
                    f"(existing parent: '{seen_names[name]}', new parent: '{parent_name or 'None'}')"
                )
                self.stats["duplicate_names"] += 1
            return self._group_cache.get(cache_key)

        seen_names[name] = parent_name or ""

        if dry_run:
            self.logger.info(
                f"🔍 [DRY RUN] Would create {group_type}: {name}"
                + (f" (parent: {parent_name})" if parent_name else "")
            )
            group = Group(
                name=name,
                short_name=name[:50],
                group_type=group_type,
                is_active=True,
            )
            group.id = -1
            self._group_cache[cache_key] = group
            return group

        parent_group = None
        if parent_name:
            parent_cache_key = f"division::{parent_name}"
            parent_group = self._group_cache.get(parent_cache_key)
            if not parent_group:
                try:
                    parent_group = Group.objects.get(name=parent_name)
                    self._group_cache[parent_cache_key] = parent_group
                except Group.DoesNotExist:
                    self.logger.warning(
                        f"⚠️  Parent group not found: {parent_name} for child: {name}"
                    )

        defaults = {
            "short_name": name[:50],
            "description": "",
            "group_type": group_type,
            "is_active": True,
        }
        if parent_group:
            defaults["parent"] = parent_group

        group, created = Group.objects.get_or_create(name=name, defaults=defaults)

        if created:
            self.stats["new_groups"] += 1
            self.logger.info(
                f"✨ Created {group_type}: {name}"
                + (f" (parent: {parent_name})" if parent_name else "")
            )
        elif parent_group and group.parent is None:
            try:
                group.parent = parent_group
                group.save(update_fields=["parent"])
                self.stats["updated_groups"] += 1
                self.logger.info(f"🔄 Updated parent for {name} -> {parent_name}")
            except Exception as e:
                self.logger.warning(f"Could not update parent for {name}: {e}")

        self._group_cache[cache_key] = group
        return group

    def print_summary(self):
        """Print summary of group sync operation."""
        duration = self.stats["end_time"] - self.stats["start_time"]

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(self.style.SUCCESS("🏛️  ORACLE GROUP SYNC COMPLETED"))
        self.stdout.write("=" * 80)

        self.stdout.write(f"⏱️  Duration: {duration:.2f} seconds")

        self.stdout.write("\n📊 GROUP STATISTICS:")
        self.stdout.write(
            f"   Groups fetched from Oracle: {self.stats['groups_fetched']}"
        )
        self.stdout.write(f"   New groups created: {self.stats['new_groups']}")
        self.stdout.write(f"   Groups updated: {self.stats['updated_groups']}")
        self.stdout.write(
            f"   Duplicate names detected: {self.stats['duplicate_names']}"
        )

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

        self.stdout.write("\n📁 Log file: logs/oracle_group_sync.log")
        self.stdout.write("=" * 80)
