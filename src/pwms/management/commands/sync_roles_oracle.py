"""
Django management command to sync role definitions from Oracle database.

This command fetches unique position descriptions from Oracle and creates
Role records. Role names are normalised so that the same logical role always
maps to a single, unique Role record regardless of source case, spacing,
punctuation or common misspellings.

Usage:
    python manage.py sync_roles_oracle
    python manage.py sync_roles_oracle --dry-run
    python manage.py sync_roles_oracle --verbose
"""

import re
from time import perf_counter

from django.core.management.base import CommandError
from django.db import IntegrityError
from pwms.management.commands.sync_base import OracleSyncBase
from pwms.models import Role

# Words that stay lowercase when they appear inside a role name (never the
# first word).
_LOWER_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}

# Acronyms / abbreviations that must always remain uppercase.
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
# (e.g. "ICT", "Ict" and "ict" all become "ICT").
_ACRONYM_BY_LOWER = {name.lower(): name for name in _ACRONYMS}

# Canonical roles that are always ensured, with their descriptions.
STANDARD_ROLES = {
    "Member of Parliament": "Elected Member of Parliament",
    "Staff Member": "General Parliamentary Staff Member",
    "Committee Chairperson": "Chairperson of a Parliamentary Committee",
    "Committee Secretary": "Secretary to a Parliamentary Committee",
    "Researcher": "Parliamentary Research Officer",
    "Protection Officer": "Parliamentary Protection Services Officer",
    "Desktop Technician": "ICT Desktop Support Technician",
}

# More descriptive text for well-known Oracle positions. The fallback is
# "Role for <name>".
ROLE_DESCRIPTIONS = {
    "Accountant": "Handles accounting and financial reporting",
    "Administrative Officer": "Handles administrative tasks and office coordination",
    "Business Analyst": "Analyzes business processes and requirements",
    "Committee Clerk": "Provides clerical support for committee operations",
    "Communications Officer": "Manages internal and external communications",
    "Control Officer": "Monitors and controls operational compliance",
    "Database Administrator": "Manages database systems and data integrity",
    "Director": "Senior management position with strategic responsibilities",
    "ECM Analyst Programmer": "Enterprise Content Management analyst and programmer",
    "Executive Assistant": "Provides high-level administrative support to executives",
    "Financial Officer": "Manages financial operations and budgeting",
    "HR Officer": "Handles human resources operations and employee relations",
    "ICT Technician": "Provides technical support and troubleshooting",
    "Legal Advisor": "Provides legal advice and guidance",
    "Manager": "Manages departmental operations and staff",
    "Network Administrator": "Maintains network infrastructure and connectivity",
    "Office Manager": "Manages office operations and administrative staff",
    "Project Manager": "Manages projects and project teams",
    "Research Officer": "Conducts research and analysis",
    "Security Officer": "Maintains security and safety protocols",
    "Service Desk Operator": "Handles IT service desk operations and user support requests",
    "Software Developer": "Develops and maintains software applications",
    "Supervisor": "Supervises staff and operational activities",
    "Systems Administrator": "Manages server systems and IT infrastructure",
    "Training Officer": "Conducts training programs and skill development sessions",
    "Under Secretary": "Senior administrative official in a department",
}

# Common misspellings / alternative renderings -> the single canonical Role
# name to use. Keys are the fuzzy ``_role_key`` of the cleaned source text
# (lower-cased, punctuation removed, whitespace collapsed).
ROLE_ALIASES = {
    # Members / staff
    "member of parliament": "Member of Parliament",
    "members of parliament": "Member of Parliament",
    "member parliament": "Member of Parliament",
    "member of the parliament": "Member of Parliament",
    "mp": "Member of Parliament",
    "member of staff": "Staff Member",
    "general staff": "Staff Member",
    "staffmember": "Staff Member",
    # Committee roles + misspellings
    "committee secretary": "Committee Secretary",
    "committe secretary": "Committee Secretary",
    "committee secretarty": "Committee Secretary",
    "committee secretery": "Committee Secretary",
    "secretary to committee": "Committee Secretary",
    "secretary committee": "Committee Secretary",
    "committee chairperson": "Committee Chairperson",
    "committee chair": "Committee Chairperson",
    "chairperson of committee": "Committee Chairperson",
    "chair of committee": "Committee Chairperson",
    # Research
    "parliamentary researcher": "Researcher",
    "research officer": "Research Officer",
    # Protection / security
    "protection officer": "Protection Officer",
    "protection services officer": "Protection Officer",
    "parliamentary protection officer": "Protection Officer",
    # Desktop / systems administration (unify singular/plural & shorthand)
    "desktop technician": "Desktop Technician",
    "pc technician": "Desktop Technician",
    "system administrator": "Systems Administrator",
    "systems administrator": "Systems Administrator",
    "system admin": "Systems Administrator",
    "systems admin": "Systems Administrator",
    "sysadmin": "Systems Administrator",
    "database administrator": "Database Administrator",
    "network administrator": "Network Administrator",
    # Misspellings & special cases seen in legacy data
    "ecm": "ECM Analyst Programmer",
    "control": "Control Officer",
    "undersecretary": "Under Secretary",
    "under secretary": "Under Secretary",
}

# Reverse index: canonical role name -> alias keys that should resolve to it.
# Used to detect (and clean up) legacy DB rows stored under a misspelling.
_ALIAS_KEYS_BY_CANONICAL: dict[str, set[str]] = {}
for _alias_key, _canonical in ROLE_ALIASES.items():
    _ALIAS_KEYS_BY_CANONICAL.setdefault(_canonical, set()).add(_alias_key)


class Command(OracleSyncBase):
    help = "Sync role definitions from Oracle database"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = self.setup_logging("oracle_role_sync")
        self.stats.update(
            {
                "roles_fetched": 0,
                "new_roles": 0,
                "matched_roles": 0,
                "updated_roles": 0,
            }
        )
        self._role_cache: dict[str, Role] = {}
        self._existing_by_key: dict[str, list[Role]] | None = None

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
            self.logger.info("Starting Oracle role sync in DRY RUN mode")
        else:
            self.logger.info("Starting Oracle role sync")

        try:
            self.connect_to_oracle()
            self.sync_roles(dry_run)

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
            self.logger.error(f"Role sync failed: {e!s}", exc_info=True)
            self.stats["errors"] += 1
            raise CommandError(f"❌ Role sync failed: {e!s}")

        finally:
            self.cleanup_connections()
            self.stats["end_time"] = perf_counter()
            self.print_summary()

    def sync_roles(self, dry_run: bool):
        """Fetch and sync roles from Oracle."""
        self.logger.info("👥 Fetching role definitions from Oracle...")

        self._load_existing_roles()

        roles_data = self.fetch_oracle_roles()
        self.stats["roles_fetched"] = len(roles_data)

        self.logger.info(f"📊 Fetched {len(roles_data)} unique position descriptions")

        self.create_standard_roles(dry_run)
        self.create_position_roles(roles_data, dry_run)

        self.logger.info("✅ Role synchronization completed")

    def _load_existing_roles(self):
        """Pre-load existing roles indexed by their canonical (fuzzy) key.

        Lets us detect roles that already exist in the database under slightly
        different case/spacing/punctuation before we insert anything, so we
        never create a second Role for the same logical role.
        """
        self._existing_by_key = {}
        for role in Role.objects.all().only("id", "name", "description"):
            self._existing_by_key.setdefault(self._role_key(role.name), []).append(role)

    def fetch_oracle_roles(self) -> set[str]:
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
            self.logger.error(f"Failed to fetch roles from Oracle: {e!s}")
            raise

    def create_standard_roles(self, dry_run: bool):
        """Ensure the canonical parliamentary roles exist."""
        for role_name, description in STANDARD_ROLES.items():
            self._ensure_role(role_name, description, dry_run)

    def create_position_roles(self, roles_data: set[str], dry_run: bool):
        """Create roles from Oracle position descriptions."""
        self.logger.info(f"🔄 Processing {len(roles_data)} role definitions...")

        for role_name in sorted(roles_data):
            # Standard roles are already ensured (with proper descriptions),
            # so don't re-create them via the generic position path.
            if role_name in STANDARD_ROLES:
                continue

            description = ROLE_DESCRIPTIONS.get(role_name, f"Role for {role_name}")
            self._ensure_role(role_name, description, dry_run)

        self.logger.info(f"✅ Processed {len(roles_data)} role definitions")

    def _ensure_role(self, role_name: str, description: str, dry_run: bool):
        """Ensure a role exists exactly once, tolerating bad source formatting.

        Existence is checked with a canonical ``_role_key`` (lower-cased,
        punctuation removed) against both the database and roles handled
        earlier in this run, so a role is never created twice just because the
        source spelled or formatted its name differently. Legacy rows stored
        under a misspelling/alias are renamed to the canonical name rather than
        left as near-duplicates.
        """
        role_name = (role_name or "").strip()
        if not role_name:
            return None

        # Resolve the name to its canonical text (acronyms, case, spacing,
        # aliases/misspellings) so we only ever create (or match) one Role.
        canonical_name = self.normalize_role_name(role_name, "Staff")
        if canonical_name != role_name:
            description = ROLE_DESCRIPTIONS.get(canonical_name, description)
            resolved = self._ensure_role(canonical_name, description, dry_run)
            # A non-aliased variant that resolved to an existing role is a
            # match, not a new creation.
            if resolved is not None and getattr(resolved, "id", None) not in (
                None,
                -1,
            ):
                self.stats["matched_roles"] += 1
            return resolved

        # Already handled earlier in this run under this canonical name.
        cached = self._role_cache.get(role_name)
        if cached is not None:
            return cached

        if self._existing_by_key is None:
            self._load_existing_roles()

        # Existing DB role that is the same logical role (any casing/spacing).
        key = self._role_key(role_name)
        matches = self._existing_by_key.get(key)
        if matches:
            role = matches[0]
            if len(matches) > 1:
                self.stats["warnings"] += 1
                self.logger.warning(
                    f"⚠️  {len(matches)} existing roles match '{role_name}' "
                    f"({', '.join(r.name for r in matches)}); keeping "
                    f"'{role.name}'"
                )

            if role.name != role_name:
                # Same logical role but legacy formatting (case/spacing):
                # normalise the stored name to the canonical text.
                if dry_run:
                    self.logger.info(
                        f"🔍 [DRY RUN] Would normalise role name "
                        f"'{role.name}' -> '{role_name}'"
                    )
                else:
                    try:
                        role.name = role_name
                        role.save(update_fields=["name"])
                        self.stats["updated_roles"] += 1
                    except IntegrityError:
                        pass
                self.logger.info(
                    f"🔎 Role already exists as '{role.name}' (source: '{role_name}')"
                )
            else:
                self.logger.info(f"✅ Role already exists: {role_name}")

            # Back-fill a description only when it is missing and we have one.
            if description and not role.description and not dry_run:
                role.description = description
                role.save(update_fields=["description"])
                self.stats["updated_roles"] += 1

            self.stats["matched_roles"] += 1
            self._role_cache[role_name] = role
            return role

        # Existing DB role stored under a misspelled/alternate spelling?
        adopted = self._adopt_alias_role(role_name, dry_run)
        if adopted is not None:
            self._role_cache[role_name] = adopted
            return adopted

        if dry_run:
            self.logger.info(f"🔍 [DRY RUN] Would create role: {role_name}")
            role = Role(name=role_name, description=description)
            role.id = -1
            self._role_cache[role_name] = role
            return role

        try:
            role, created = Role.objects.get_or_create(
                name=role_name,
                defaults={"description": description or f"Role for {role_name}"},
            )
        except IntegrityError:
            # Lost a race / exact duplicate; adopt the existing row.
            role = Role.objects.filter(name=role_name).first()
            if role is None:
                raise
            created = False

        if created:
            self.stats["new_roles"] += 1
            self.logger.info(f"✨ Created role: {role_name}")
        else:
            self.stats["matched_roles"] += 1
            self.logger.info(f"✅ Role already exists: {role_name}")

        self._role_cache[role_name] = role
        self._existing_by_key.setdefault(key, []).append(role)
        return role

    def _adopt_alias_role(self, canonical_name: str, dry_run: bool) -> Role | None:
        """Find a legacy DB role stored under an alias of ``canonical_name``.

        Such a row is renamed to the canonical name (keeping its pk, so
        existing memberships stay intact) instead of creating a duplicate.
        """
        for alias_key in _ALIAS_KEYS_BY_CANONICAL.get(canonical_name, ()):
            bucket = self._existing_by_key.get(alias_key) or []
            for role in list(bucket):
                if role.name == canonical_name:
                    return role  # already canonical; just reuse

                if dry_run:
                    self.logger.info(
                        f"🔍 [DRY RUN] Would rename legacy role "
                        f"'{role.name}' -> '{canonical_name}'"
                    )
                    placeholder = Role(name=canonical_name, description="")
                    placeholder.id = -1
                    return placeholder

                try:
                    role.name = canonical_name
                    role.save(update_fields=["name"])
                except IntegrityError:
                    self.logger.warning(
                        f"⚠️  Could not rename '{role.name}' to "
                        f"'{canonical_name}' (name already in use)"
                    )
                    continue

                # Keep the in-memory index consistent with the rename.
                bucket.remove(role)
                self._existing_by_key.setdefault(
                    self._role_key(canonical_name), []
                ).append(role)

                self.stats["matched_roles"] += 1
                self.stats["updated_roles"] += 1
                self.logger.info(
                    f"♻️  Renamed legacy role to canonical: '{canonical_name}'"
                )
                return role
        return None

    # ------------------------------------------------------------------ #
    #  Normalisation helpers (override the base implementation)          #
    # ------------------------------------------------------------------ #

    def normalize_role_name(self, positiondesc: str, employeetype: str) -> str:
        """Normalise an Oracle position into a clean, unique Role name.

        Members always map to "Member of Parliament"; staff with no position
        map to "Staff Member". Everything else is cleaned (house markers,
        parenthetical notes, punctuation, whitespace, case and known
        misspellings) so the same role always yields the same Role record.
        """
        employeetype = (employeetype or "").strip().lower()
        if employeetype in ("member", "mp"):
            return "Member of Parliament"

        cleaned = self._clean_role_text(positiondesc or "")
        if not cleaned:
            return "Staff Member"

        canonical = ROLE_ALIASES.get(self._role_key(cleaned))
        if canonical:
            return canonical

        return self._smart_title(cleaned)

    def _clean_role_text(self, text: str) -> str:
        """Strip code prefixes, house markers, brackets and stray whitespace."""
        text = (text or "").strip()
        # Normalise fancy quotes/apostrophes to plain ASCII.
        text = (
            text.replace("\u2019", "'")
            .replace("\u2018", "'")
            .replace("\u201c", '"')
            .replace("\u201d", '"')
        )
        # Numeric code prefix, e.g. "12345-..." or "12345: ...".
        text = re.sub(r"^\d+\s*[-:]\s*", "", text)
        # House markers: keep the part before the first colon ("Manager: NA").
        if ":" in text:
            text = text.split(":", 1)[0].strip()
        # Parenthetical notes, e.g. "Director (Acting)".
        text = re.sub(r"\([^)]*\)", " ", text)
        # Collapse whitespace and tidy stray punctuation.
        text = " ".join(text.split())
        text = text.strip(" -–—,.;:'\"")
        return text

    def _smart_title(self, text: str) -> str:
        """Title-case a phrase while preserving acronyms and small words."""
        words = text.split()
        result = []
        for idx, word in enumerate(words):
            lower = word.lower()
            acronym = _ACRONYM_BY_LOWER.get(lower)
            if acronym is not None:
                result.append(acronym)
            elif idx > 0 and lower in _LOWER_WORDS:
                result.append(lower)
            else:
                result.append(self._cap_word(word))
        return " ".join(result)

    @staticmethod
    def _cap_word(word: str) -> str:
        """Capitalise the first letter of a word, lower-casing the rest."""
        lower = word.lower()
        for i, ch in enumerate(lower):
            if ch.isalpha():
                return f"{lower[:i]}{ch.upper()}{lower[i + 1 :]}"
        return lower

    @staticmethod
    def _role_key(name: str) -> str:
        """Collapse a role name into a stable lookup key.

        Lower-cased, punctuation stripped (so "under-secretary" and
        "undersecretary" match) and whitespace collapsed.
        """
        chars = "".join(ch for ch in name.lower() if ch.isalnum() or ch.isspace())
        return " ".join(chars.split())

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
        self.stdout.write(f"   Existing roles matched: {self.stats['matched_roles']}")
        self.stdout.write(f"   Descriptions back-filled: {self.stats['updated_roles']}")

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
