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

from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from pwms.management.commands.sync_base import OracleSyncBase
from pwms.models import Group

# Acronyms / abbreviations that must stay uppercase in organisational unit
# names. ``OracleSyncBase.strip_group_code_prefix`` only preserves a smaller
# set, so we restore these after sanitising. Kept in sync with the acronym
# list used by ``sync_roles_oracle``.
_ACRONYMS = {
    "BO",
    "BP1",
    "BP2",
    "CAE",
    "CBS",
    "CCSC",
    "CEO",
    "CFO",
    "CIO",
    "CIS",
    "CISO",
    "CS",
    "DS",
    "ECM",
    "ERP",
    "FMO",
    "HC",
    "HR",
    "ICT",
    "IP",
    "IR",
    "IRP",
    "ISS",
    "IT",
    "KIS",
    "LOGB",
    "LR",
    "LS",
    "LSO",
    "LSS",
    "MIS",
    "MP",
    "MR",
    "MPs",
    "MSR",
    "MSS",
    "NA",
    "NCOP",
    "OM",
    "OISD",
    "OSTP",
    "PA",
    "PBO",
    "PCS",
    "PCSD",
    "PDO",
    "PISC",
    "POSA",
    "PMO",
    "PP",
    "PPS",
    "PM",
    "PMU",
    "RM",
    "RMI",
    "RS",
    "SC",
    "SCM",
    "SMG",
    "SS",
    "TAO",
    "TM",
}

# Lower-cased lookup so mixed-case input still resolves to the canonical form
# (e.g. "CBS", "Cbs" and "cbs" all become "CBS").
_ACRONYM_BY_LOWER = {name.lower(): name for name in _ACRONYMS}


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
            self.logger.error(f"Group sync failed: {e!s}", exc_info=True)
            self.stats["errors"] += 1
            raise CommandError(f"❌ Group sync failed: {e!s}")

        finally:
            self.cleanup_connections()
            self.stats["end_time"] = perf_counter()
            self.print_summary()

    def sync_groups(self, dry_run: bool):
        """Fetch and sync organizational groups from Oracle."""
        self.logger.info("🏛️  Fetching organizational groups from Oracle...")

        groups_data = self.fetch_oracle_groups()
        self.stats["groups_fetched"] = len(groups_data)

        self.logger.info(
            f"📊 Fetched {len(groups_data)} unique organizational unit relationships"
        )

        parliament_group = self.ensure_parliament_root(dry_run)
        main_groups = self.create_main_groups(parliament_group, dry_run)
        administration = main_groups.get("administration")

        self.create_organizational_groups(groups_data, administration, dry_run)

        self.logger.info("✅ Group synchronization completed")

    def fetch_oracle_groups(self) -> set[tuple[str, str]]:
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
            self.logger.error(f"Failed to fetch groups from Oracle: {e!s}")
            raise

    def ensure_parliament_root(self, dry_run: bool) -> Group:
        """Ensure the root Parliament group exists."""
        if dry_run:
            self.logger.info("🔍 [DRY RUN] Would ensure Parliament root group exists")
            parliament = Group(
                name="Parliament",
                short_name="parliament",
                group_type="parliament",
                description="Parliament of the Republic of South Africa",
                is_active=True,
            )
            parliament.id = -1
            return parliament

        # The root group has no parent; match by that natural key so a section
        # that happens to be named "Parliament" elsewhere can't shadow it.
        parliament = Group.objects.filter(
            name="Parliament", parent__isnull=True
        ).first()

        if parliament is None:
            parliament = Group.objects.create(
                name="Parliament",
                short_name="parliament",
                group_type="parliament",
                description="Parliament of the Republic of South Africa",
                is_active=True,
            )
            self.stats["new_groups"] += 1
            self.logger.info("✨ Created Parliament root group")

        return parliament

    def create_main_groups(self, parliament: Group, dry_run: bool) -> dict[str, Group]:
        """Create the three main parliamentary groups under ``parliament``.

        Returns a mapping of ``short_name`` -> Group so the caller can anchor
        the organisational hierarchy on the ``Administration`` staff group.
        Main groups are matched by their ``(name, parent)`` position directly
        under Parliament (not by name alone, since Oracle org units can
        legitimately share these names); a matching orphan is re-attached and
        a genuinely missing group is created.
        """
        main_groups = [
            {
                "name": "National Assembly",
                "short_name": "national-assembly",
                "group_type": "house",
                "description": "National Assembly of Parliament",
            },
            {
                "name": "National Council of Provinces",
                "short_name": "national-council-of-provinces",
                "group_type": "house",
                "description": "National Council of Provinces",
            },
            {
                "name": "Administration",
                "short_name": "administration",
                "group_type": "administration",
                "description": "Parliamentary Staff Members",
            },
        ]

        result: dict[str, Group] = {}
        for group_data in main_groups:
            if dry_run:
                self.logger.info(
                    f"🔍 [DRY RUN] Would ensure main group exists: {group_data['name']}"
                )
                # Lightweight non-persisted placeholder so downstream planning
                # (e.g. attaching org units under Administration) can run.
                placeholder = Group(
                    name=group_data["name"],
                    group_type=group_data["group_type"],
                    is_active=True,
                )
                placeholder.id = -1
                result[group_data["short_name"]] = placeholder
                continue

            # The main groups sit directly under Parliament, so they are
            # matched by that natural key -- (name, parent) -- NOT by name
            # alone. Oracle org units can legitimately share these names
            # (e.g. a "National Assembly" *section* under Members Section),
            # and the model permits both via UniqueConstraint(name, parent).
            group = Group.objects.filter(
                name=group_data["name"], parent=parliament
            ).first()

            if group is None and parliament.id != -1:
                # Nothing under Parliament yet: adopt an orphaned main group
                # (same name, no parent, matching type) left by an earlier
                # run, otherwise it is created below.
                orphan = Group.objects.filter(
                    name=group_data["name"],
                    parent__isnull=True,
                    group_type=group_data["group_type"],
                ).first()
                if orphan is not None:
                    try:
                        orphan.parent = parliament
                        orphan.save(update_fields=["parent"])
                        self.stats["updated_groups"] += 1
                        self.logger.info(
                            f"🔄 Re-attached orphaned main group to Parliament: "
                            f"{group_data['name']}"
                        )
                        group = orphan
                    except Exception as e:
                        self.logger.warning(
                            f"Could not re-attach main group "
                            f"'{group_data['name']}': {e!s}"
                        )

            if group is None:
                # No matching main group -> create it under Parliament.
                try:
                    group = Group.objects.create(
                        name=group_data["name"],
                        short_name=group_data["short_name"],
                        group_type=group_data["group_type"],
                        description=group_data["description"],
                        is_active=True,
                        parent=parliament if parliament.id != -1 else None,
                    )
                    self.stats["new_groups"] += 1
                    self.logger.info(f"✨ Created main group: {group_data['name']}")
                except IntegrityError:
                    # Lost a race / concurrent insert: reuse the existing row.
                    group = Group.objects.filter(
                        name=group_data["name"], parent=parliament
                    ).first()
                    if group is None:
                        raise

            result[group_data["short_name"]] = group

        return result

    def create_organizational_groups(
        self,
        groups_data: set[tuple[str, str]],
        administration: Group,
        dry_run: bool,
    ):
        """Build the staff organisational tree under ``administration``.

        Oracle returns a set of ``(child_org, parent_org)`` edges. Instead of
        forcing every parent into a flat root-level "division" (the old
        behaviour), this reconstructs the real tree:

        * a name that only ever appears as a *child* is a leaf  -> ``section``
        * a name that is the parent of at least one other unit, or that sits
          directly under Administration, is an internal unit -> ``division``
        * deeper chains are honoured (a unit may itself live under another)
        * the whole forest is anchored under the ``Administration`` group

        Existence is checked against the model's natural key ``(name, parent)``
        before anything is inserted, so re-runs are idempotent. Units left
        orphaned by earlier sync runs (parent ``None``) are adopted and
        re-parented rather than duplicated.
        """
        # ---- 1. Normalise Oracle edges and derive the intended tree ---------
        children_of: dict[str, set[str]] = {}
        parent_of: dict[str, set[str]] = {}
        all_names: set[str] = set()

        for raw_child, raw_parent in groups_data:
            child = self._restore_acronym_case(
                self.strip_group_code_prefix(raw_child or "")
            )
            parent = self._restore_acronym_case(
                self.strip_group_code_prefix(raw_parent or "")
            )
            if not child and not parent:
                continue

            if child:
                all_names.add(child)
                if parent:
                    all_names.add(parent)
                    children_of.setdefault(parent, set()).add(child)
                    parent_of.setdefault(child, set()).add(parent)
                # Child without a parent -> top-level unit (root of the forest).
            elif parent:
                # Parent appears without any child row -> standalone unit.
                all_names.add(parent)

        if not all_names:
            self.logger.info("ℹ️  No organisational units found to sync")
            return

        # A name that is the parent of anything is an internal ("division")
        # node; names that only ever appear as children become "section" nodes.
        nodes_with_children = set(children_of)

        # Pick one canonical parent per name; flag genuine collisions so we
        # never create two conflicting rows for the same logical unit.
        canonical_parent: dict[str, str] = {}
        for child, parents in parent_of.items():
            ordered = sorted(parents)
            canonical_parent[child] = ordered[0]
            if len(ordered) > 1:
                self.stats["duplicate_names"] += len(ordered) - 1
                self.logger.warning(
                    f"⚠️  Org unit '{child}' reported under multiple parents "
                    f"({', '.join(ordered)}); keeping '{ordered[0]}'"
                )

        self._administration = administration
        self._org_all_names = all_names
        self._org_nodes_with_children = nodes_with_children
        self._org_canonical_parent = canonical_parent
        self._org_created_by_name: dict[str, Group] = {}
        self._org_same_name: dict[str, list[Group]] = {}
        self._org_visiting: set[str] = set()

        # ---- 2. Ensure every unit exists at its intended position -----------
        # An atomic block keeps the MPTT bookkeeping consistent even if a unit
        # fails part-way through.
        with transaction.atomic():
            for name in sorted(all_names):
                self._ensure_org_node(name, dry_run)

        self.logger.info(
            f"✅ Processed {len(groups_data)} organisational relationships "
            f"({len(all_names)} unique units)"
        )

    def _ensure_org_node(self, name: str, dry_run: bool) -> Group | None:
        """Recursively ensure an org unit (and, via recursion, its ancestors)."""
        cached = self._org_created_by_name.get(name)
        if cached is not None:
            return cached

        if name in self._org_visiting:
            self.logger.warning(f"⚠️  Cycle detected in org data around '{name}'")
            self.stats["warnings"] += 1
            return None
        self._org_visiting.add(name)

        try:
            parent_name = self._org_canonical_parent.get(name)
            if parent_name:
                parent_group = self._ensure_org_node(parent_name, dry_run)
                if parent_group is None:
                    self.logger.warning(
                        f"⚠️  Skipping '{name}': parent unit '{parent_name}' "
                        "could not be resolved"
                    )
                    self.stats["warnings"] += 1
                    return None
                # Has children -> internal unit; otherwise a leaf.
                node_type = (
                    "division" if name in self._org_nodes_with_children else "section"
                )
            else:
                # No Oracle parent -> sits directly under Administration.
                parent_group = self._administration
                node_type = "division"

            group = self._ensure_org_group(
                name, parent_name or "", parent_group, node_type, dry_run
            )
            if group is not None:
                self._org_created_by_name[name] = group
            return group
        finally:
            self._org_visiting.discard(name)

    def _ensure_org_group(
        self,
        name: str,
        parent_name: str,
        parent_group: Group,
        node_type: str,
        dry_run: bool,
    ) -> Group | None:
        """Check ``(name, parent)`` existence, adopt orphans, or create."""
        if dry_run:
            # Read-only plan: report what would happen without writing anything.
            parent_lookup = (
                {"parent__name": parent_name}
                if parent_name
                else (
                    {"parent__name": self._administration.name}
                    if self._administration is not None
                    else {"parent__isnull": True}
                )
            )
            # Case-insensitive so legacy acronym casing ("Cbs: ...") is seen.
            existing = Group.objects.filter(name__iexact=name, **parent_lookup).first()
            if existing is None:
                self.logger.info(
                    f"🔍 [DRY RUN] Would create {node_type}: '{name}' "
                    f"(parent: {parent_name or 'Administration'})"
                )
            elif existing.name == name:
                self.logger.info(
                    f"🔍 [DRY RUN] Already present: {node_type} '{name}' "
                    f"(parent: {parent_name or 'Administration'})"
                )
            else:
                self.logger.info(
                    f"🔍 [DRY RUN] Would normalise {node_type} name "
                    f"'{existing.name}' -> '{name}'"
                )
            # Non-persisted placeholder to keep the traversal simple.
            placeholder = Group(name=name, group_type=node_type, is_active=True)
            placeholder.id = -1
            return placeholder

        if parent_group is None or parent_group.id is None or parent_group.id == -1:
            self.logger.error(
                f"❌ Cannot create '{name}': parent group is not persisted"
            )
            self.stats["errors"] += 1
            return None

        # 1) Exact natural-key match -> nothing to create.
        existing = Group.objects.filter(name=name, parent=parent_group).first()
        if existing is not None:
            self.logger.info(
                f"✅ Group already exists: {node_type} '{name}' "
                f"(parent: {parent_group.name})"
            )
            return existing

        # 1b) Same parent but the stored name differs only by acronym casing
        #     (rows created before acronym restoration, e.g. "Cbs: ..." vs the
        #     canonical "CBS: ...") -> normalise it in place so we never
        #     create a duplicate that differs only by case.
        normalised = self._adopt_case_variant_group(name, parent_group, node_type)
        if normalised is not None:
            return normalised

        # 2) Same name under the wrong node from an earlier run -> adopt it
        #    into the intended position instead of creating a duplicate.
        adopted = self._adopt_misplaced_org_group(name, parent_group, node_type)
        if adopted is not None:
            return adopted

        # 3) Genuinely new -> create under the intended parent.
        try:
            group = Group.objects.create(
                name=name,
                short_name=name[:50],
                description="",
                group_type=node_type,
                is_active=True,
                parent=parent_group,
            )
        except IntegrityError:
            # Duplicate slipped in (race / concurrent run); fall back to it.
            group = Group.objects.filter(name=name, parent=parent_group).first()
            if group is None:
                raise
        self.stats["new_groups"] += 1
        self.logger.info(
            f"✨ Created {node_type}: {name} (parent: {parent_group.name})"
        )
        return group

    def _adopt_case_variant_group(
        self, name: str, parent_group: Group, node_type: str
    ) -> Group | None:
        """Reuse a legacy org group that differs from ``name`` only by casing.

        Legacy rows created before acronym restoration store names such as
        "Cbs: ..." where the current run expects "CBS: ...". When such a row
        already sits under the intended parent, rename it in place (its pk is
        preserved, so children and memberships stay attached) instead of
        creating a near-identical duplicate.
        """
        candidate = (
            Group.objects.filter(name__iexact=name, parent=parent_group)
            .exclude(name=name)
            .first()
        )
        if candidate is None or not self._is_org_managed(candidate, parent_group):
            return None

        old_name = candidate.name
        try:
            candidate.name = name
            candidate.save(update_fields=["name"])
        except IntegrityError:
            self.logger.warning(
                f"⚠️  Could not normalise '{old_name}' -> '{name}' (name already in use)"
            )
            return None

        self.stats["updated_groups"] += 1
        self.logger.info(
            f"♻️  Normalised org unit name casing: '{old_name}' -> '{name}'"
        )
        return candidate

    def _adopt_misplaced_org_group(
        self, name: str, parent_group: Group, node_type: str
    ) -> Group | None:
        """Re-parent an existing org group found under the wrong node.

        Only clearly org-managed rows are touched:
          * orphans with no parent at all, or
          * groups sitting directly under Administration, or
          * groups whose current parent is another org unit being synced here.

        This deliberately never moves houses, committees or other curated
        trees that merely happen to share a name.
        """
        same_name = self._org_same_name.get(name)
        if same_name is None:
            same_name = list(Group.objects.filter(name=name).select_related("parent"))
            self._org_same_name[name] = same_name

        for candidate in same_name:
            if candidate.id == parent_group.id:
                continue  # exact match (already returned by caller)
            current_parent = candidate.parent

            if not self._is_org_managed(candidate, current_parent):
                continue

            old_parent = current_parent.name if current_parent else None
            try:
                candidate.parent = parent_group
                candidate.save(update_fields=["parent"])
            except IntegrityError:
                self.logger.warning(
                    f"⚠️  Could not re-parent '{name}' under "
                    f"'{parent_group.name}' (name/parent conflict); leaving as is"
                )
                continue

            self.stats["updated_groups"] += 1
            self.logger.info(
                f"🔄 Re-parented existing {node_type} '{name}': "
                f"'{old_parent or 'None'}' -> '{parent_group.name}'"
            )
            return candidate

        return None

    def _is_org_managed(self, group: Group, current_parent: Group | None) -> bool:
        """Whether a DB group may safely be moved by this org sync."""
        if current_parent is None:
            return True  # orphan left by an earlier sync run
        if group.group_type not in {"division", "section"}:
            return False  # curated node (house, committee, party, ...)
        admin = self._administration
        if admin is not None and current_parent.id == admin.id:
            return True
        return current_parent.name in self._org_all_names

    @staticmethod
    def _restore_acronym_case(name: str) -> str:
        """Uppercase known acronyms that title-casing would have mangled.

        ``strip_group_code_prefix`` title-cases every word and only preserves
        a small acronym set (ICT, HR, ...). Words such as "CBS", "CAE",
        "FMO" therefore come back as "Cbs", "Cae", "Fmo". This re-uppercases
        any token whose lower-cased form matches a known acronym, ignoring any
        surrounding punctuation (so "Cbs:" becomes "CBS:"), while leaving all
        other casing untouched.
        """
        words = (name or "").split()
        restored = []
        for word in words:
            core = word.strip("():;,./-'\"")
            replacement = _ACRONYM_BY_LOWER.get(core.lower())
            if replacement is None:
                restored.append(word)
                continue
            # Preserve punctuation that surrounded the acronym token.
            start = word.find(core)
            lead = word[:start]
            trail = word[start + len(core) :]
            restored.append(f"{lead}{replacement}{trail}")
        return " ".join(restored)

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
