"""
Django management command to sync users and groups from Oracle database to local SQLite3 database.

This command connects to an external Oracle database and synchronizes user data with the local
Bungeni parliamentary management system. It handles user creation, updates, deactivation,
and group membership management.

Usage:
    python manage.py sync_users_oracle
    python manage.py sync_users_oracle --dry-run
    python manage.py sync_users_oracle --verbose
"""

import logging
from time import perf_counter
from typing import Dict, Tuple

import cx_Oracle
from decouple import config
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from workflows.models import Group, GroupMembership, Role, User


class Command(BaseCommand):
    help = "Sync users and groups from Oracle database to local SQLite3 database"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = self.setup_logging()
        self.oracle_conn = None
        self.oracle_cursor = None

        # Sync statistics
        self.stats = {
            "new_users": 0,
            "updated_users": 0,
            "disabled_users": 0,
            "new_memberships": 0,
            "deactivated_memberships": 0,
            "fixed_passwords": 0,
            "new_roles": 0,
            "new_groups": 0,
            "errors": 0,
            "start_time": None,
            "end_time": None,
        }

        # Simple caches to avoid duplicate lookups/creates within a run
        self._role_cache = {}
        self._group_cache = {}

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run the sync without making any database changes",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose logging output",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of records to process in each batch (default: 100)",
        )

    def setup_logging(self) -> logging.Logger:
        """Set up logging configuration for the sync process."""
        logger = logging.getLogger("oracle_user_sync")
        logger.setLevel(logging.DEBUG)

        # Remove existing handlers to avoid duplicates
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)

        # Create file handler with rotation
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            "logs/oracle_user_sync.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        file_handler.setLevel(logging.DEBUG)

        # Create console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)

        # Create formatter
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        # Add handlers to logger
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        return logger

    def handle(self, *args, **options):
        """Main command handler."""
        self.stats["start_time"] = perf_counter()

        # Set verbosity
        if options["verbose"]:
            self.logger.setLevel(logging.DEBUG)
            for handler in self.logger.handlers:
                if isinstance(handler, logging.StreamHandler):
                    handler.setLevel(logging.DEBUG)

        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "DRY RUN MODE - No changes will be made to the database"
                )
            )
            self.logger.info("Starting Oracle user sync in DRY RUN mode")
        else:
            self.logger.info("Starting Oracle user sync")

        try:
            self.connect_to_oracle()
            self.sync_users_and_groups(dry_run, batch_size)

        except Exception as e:
            self.logger.error(f"Sync failed with error: {str(e)}", exc_info=True)
            self.stats["errors"] += 1
            raise CommandError(f"Sync failed: {str(e)}")

        finally:
            self.cleanup_connections()
            self.stats["end_time"] = perf_counter()
            self.print_summary()

    def connect_to_oracle(self):
        """Establish connection to Oracle database."""
        try:
            oracle_connection_string = f"{config('ORA_USER')}/{config('ORA_PWD')}@{config('ORA_HOST')}:{config('ORA_PORT')}/{config('ORA_SERVICE')}"

            self.oracle_conn = cx_Oracle.connect(oracle_connection_string)
            self.oracle_cursor = self.oracle_conn.cursor()

            self.logger.info("Successfully connected to Oracle database")
            self.stdout.write(self.style.SUCCESS("Connected to Oracle database"))

        except Exception as e:
            self.logger.error(f"Failed to connect to Oracle: {str(e)}")
            raise CommandError(f"Oracle connection failed: {str(e)}")

    def fetch_oracle_users(self) -> Dict[str, Tuple]:
        """Fetch users from Oracle database."""
        self.logger.info("Fetching users from Oracle database...")

        query = """
            SELECT TITLE, LASTNAME, FIRSTNAME, MIDDLENAMES, EMAIL, IDNO, SEX,
                   POSITIONDESC, EMPLOYEETYPE, PARTYCODE, USERNAME,
                   CHILD_ORG_NAME, PARENT_ORG_NAME
            FROM APPS.XXPER_PEOPLE_INTERFACE
            WHERE CURRENT_EMPLOYEE_FLAG='Y'
            AND ASSIGNMENT_STATUS='Active Assignment'
            AND EMAIL IS NOT NULL
        """

        self.oracle_cursor.execute(query)
        oracle_users = {user[5]: user for user in self.oracle_cursor.fetchall()}

        self.logger.info(f"Fetched {len(oracle_users)} users from Oracle")
        return oracle_users

    def strip_group_code_prefix(self, name: str) -> str:
        """Remove leading code prefixes like '12345-Group Name' -> 'Group Name'."""
        if not name:
            return ""
        parts = name.split("-", 1)
        return (
            parts[1].strip()
            if len(parts) == 2 and parts[0].strip().isdigit()
            else name.strip()
        )

    def ensure_role_exists(self, role_name: str) -> Role:
        """Get or create a Role by exact name. Uses an in-memory cache for speed."""
        role_name = (role_name or "").strip()
        if not role_name:
            return None
        if role_name in self._role_cache:
            return self._role_cache[role_name]

        # Enhanced role descriptions
        role_descriptions = {
            # ICT/Technical Roles
            "Desktop Technician": "Provides technical support for desktop computers and user workstations",
            "Website Administrator": "Manages and maintains organizational websites and web applications",
            "Service Desk Operator": "Handles IT service desk operations and user support requests",
            "Service Desk Supervisor": "Supervises service desk operations and support team",
            "ECM Analyst Programmer": "Enterprise Content Management analyst and programmer",
            "Training Officer": "Conducts training programs and skill development sessions",
            "Systems Administrator": "Manages server systems and IT infrastructure",
            "Network Administrator": "Maintains network infrastructure and connectivity",
            "Database Administrator": "Manages database systems and data integrity",
            "IT Support Technician": "Provides technical support and troubleshooting",
            "Software Developer": "Develops and maintains software applications",
            "Business Analyst": "Analyzes business processes and requirements",
            "Project Manager": "Manages projects and project teams",
            # Administrative Roles
            "Administrative Officer": "Handles administrative tasks and office coordination",
            "Executive Assistant": "Provides high-level administrative support to executives",
            "Office Manager": "Manages office operations and administrative staff",
            "Receptionist": "Handles front desk operations and visitor management",
            "Personal Assistant": "Provides personal assistance to senior staff",
            # Finance Roles
            "Financial Officer": "Manages financial operations and budgeting",
            "Accountant": "Handles accounting and financial reporting",
            "Budget Analyst": "Analyzes budget requirements and financial planning",
            "Financial Controller": "Oversees financial controls and compliance",
            # HR Roles
            "HR Officer": "Handles human resources operations and employee relations",
            "HR Manager": "Manages HR department and strategic HR functions",
            "Training Coordinator": "Coordinates training programs and development initiatives",
            "Recruitment Officer": "Handles recruitment and hiring processes",
            # Legal/Compliance Roles
            "Legal Advisor": "Provides legal advice and guidance",
            "Compliance Officer": "Ensures regulatory compliance and risk management",
            "Parliamentary Counsel": "Provides legal counsel for parliamentary matters",
            # Communications Roles
            "Communications Officer": "Manages internal and external communications",
            "Media Liaison": "Handles media relations and communications",
            "Public Relations Officer": "Manages public relations and outreach",
            # Security Roles
            "Security Officer": "Maintains security and safety protocols",
            "Protection Officer": "Provides security protection for personnel and facilities",
            # Management Roles
            "Director": "Senior management position with strategic responsibilities",
            "Deputy Director": "Assists director in management and operations",
            "Manager": "Manages departmental operations and staff",
            "Team Leader": "Leads team operations and coordinates team activities",
            "Supervisor": "Supervises staff and operational activities",
            "Coordinator": "Coordinates activities and resources across teams",
            # Research/Policy Roles
            "Researcher": "Conducts research and analysis",
            "Policy Analyst": "Analyzes and develops policy recommendations",
            "Advisor": "Provides expert advice and consultation",
            "Consultant": "Provides specialized consulting services",
            # Parliamentary Roles
            "Clerk": "Provides clerical support for parliamentary proceedings",
            "Table Clerk": "Manages parliamentary table and proceedings documentation",
            "Committee Clerk": "Provides clerical support for committee operations",
            "Hansard Reporter": "Records and transcribes parliamentary proceedings",
            "Interpreter": "Provides interpretation services for parliamentary proceedings",
            "Translator": "Translates documents and communications",
            # Facilities/Maintenance
            "Facilities Manager": "Manages facilities and building operations",
            "Maintenance Officer": "Handles maintenance and repairs",
            "Cleaner": "Maintains cleanliness and hygiene of facilities",
            "Groundskeeper": "Maintains grounds and outdoor facilities",
        }

        description = role_descriptions.get(role_name, f"Role for {role_name}")

        role, created = Role.objects.get_or_create(
            name=role_name,
            defaults={
                "description": description,
            },
        )
        if created:
            self.stats["new_roles"] += 1
            self.logger.info(f"✨ Created Role: {role_name}")
        self._role_cache[role_name] = role
        return role

    def ensure_group_hierarchy(
        self, child_name: str, parent_name: str
    ) -> Tuple[Group, Group]:
        """Ensure parent and child groups exist; return (child, parent). Only creates if missing.

        Group names may come with code prefixes (e.g., '12345-Group'); we strip those.
        """
        child_name = self.strip_group_code_prefix(child_name)
        parent_name = self.strip_group_code_prefix(parent_name)

        child_key = f"child::{child_name}"
        parent_key = f"parent::{parent_name}"

        parent_group = self._group_cache.get(parent_key)
        if not parent_group and parent_name:
            parent_group, created = Group.objects.get_or_create(
                name=parent_name,
                defaults={
                    "short_name": parent_name[:50],
                    "description": "",
                    "group_type": "division",
                    "is_active": True,
                },
            )
            if created:
                self.stats["new_groups"] += 1
                self.logger.info(f"Created parent Group: {parent_name}")
            self._group_cache[parent_key] = parent_group

        child_group = self._group_cache.get(child_key)
        if not child_group and child_name:
            defaults = {
                "short_name": child_name[:50],
                "description": "",
                "group_type": "section",
                "is_active": True,
            }
            if parent_group:
                defaults["parent"] = parent_group
            child_group, created = Group.objects.get_or_create(
                name=child_name,
                defaults=defaults,
            )
            # If child exists but has no parent set and we have a parent_group, set it once
            if not created and parent_group and child_group.parent is None:
                try:
                    child_group.parent = parent_group
                    child_group.save(
                        update_fields=["parent"]
                    ) if child_group.pk else None
                except Exception:
                    # Non-fatal; skip if constraint prevents update
                    pass
            if created:
                self.stats["new_groups"] += 1
                self.logger.info(
                    f"Created child Group: {child_name}{' (parent: ' + parent_name + ')' if parent_group else ''}"
                )
            self._group_cache[child_key] = child_group

        return child_group, parent_group

    def get_or_create_groups(self) -> Dict[str, Group]:
        """Get or create required groups for user assignments."""
        groups = {}

        # Get or create Parliament group
        parliament, created = Group.objects.get_or_create(
            name="Parliament of South Africa",
            defaults={
                "group_type": "legislature",
                "description": "Parliament of South Africa",
                "is_active": True,
            },
        )
        if created:
            self.logger.info("Created Parliament group")

        # Get or create National Assembly
        na_group, created = Group.objects.get_or_create(
            name="NA: NATIONAL ASSEMBLY",
            defaults={
                "group_type": "house",
                "description": "National Assembly of South Africa",
                "parent": parliament,
                "is_active": True,
            },
        )
        if created:
            self.logger.info("Created National Assembly group")
        groups["lower_house"] = na_group

        # Get or create National Council of Provinces
        ncop_group, created = Group.objects.get_or_create(
            name="NCOP: NATIONAL COUNCIL OF PROVINCES",
            defaults={
                "group_type": "house",
                "description": "National Council of Provinces",
                "parent": parliament,
                "is_active": True,
            },
        )
        if created:
            self.logger.info("Created NCOP group")
        groups["upper_house"] = ncop_group

        # Get or create Parliament Staff umbrella group
        staff_group, created = Group.objects.get_or_create(
            name="Parliament Staff",
            defaults={
                "group_type": "administration",
                "description": "All parliamentary staff",
                "parent": parliament,
                "is_active": True,
            },
        )
        if created:
            self.logger.info("Created Parliament Staff group")
        groups["staff_group"] = staff_group

        return groups

    def get_or_create_roles(self) -> Dict[str, Role]:
        """Get or create required roles."""
        roles = {}

        member_role, created = Role.objects.get_or_create(
            name="Member of Parliament",
            defaults={
                "description": "Member of Parliament role",
            },
        )
        if created:
            self.logger.info("Created MP role")
        roles["mp"] = member_role

        staff_role, created = Role.objects.get_or_create(
            name="Staff Member",
            defaults={
                "description": "Parliamentary staff member role",
            },
        )
        if created:
            self.logger.info("Created Staff role")
        roles["staff"] = staff_role

        return roles

    def normalize_role_name(self, positiondesc: str, employeetype: str) -> str:
        """Normalize role names to reduce proliferation.

        Rules:
        - Members always map to 'Member of Parliament'.
        - If no position provided for staff, default to 'Staff Member'.
        - Otherwise, clean up the position description:
          * strip whitespace
          * remove surrounding parentheses content
          * collapse multiple spaces
          * title-case common names
        """
        employeetype = (employeetype or "").strip()
        pos = (positiondesc or "").strip()

        if employeetype.lower() == "member":
            return "Member of Parliament"

        if not pos:
            return "Staff Member"

        # Remove any trailing house markers like ': NA' or ': NCOP'
        if ":" in pos:
            pos = pos.split(":", 1)[0].strip()

        # Drop parentheses segments e.g. "Director (Acting)" -> "Director"
        if "(" in pos and ")" in pos:
            try:
                start = pos.index("(")
                end = pos.rindex(")")
                if end > start:
                    pos = f"{pos[:start].strip()} {pos[end + 1 :].strip()}".strip()
            except Exception:
                pass

        # Collapse whitespace and title case
        pos = " ".join(pos.split())
        normalized = pos.title()
        return normalized or "Staff Member"

    def normalize_name_fields(self, user_data: Tuple) -> Dict[str, str]:
        """Normalize and clean name fields from Oracle data."""
        title, lastname, firstname, middlenames = user_data[0:4]

        # Clean and normalize names
        first_name = firstname.strip() if firstname else ""
        middle_name = middlenames.strip() if middlenames else ""
        last_name = lastname.strip() if lastname else ""

        # Handle cases where first name is empty but middle name exists
        if not first_name and middle_name:
            first_name = middle_name
            middle_name = ""
        elif not first_name:
            first_name = "BLANK"

        # Generate initials
        names = f"{first_name} {middle_name}".strip()
        initials = "".join([name[0].upper() for name in names.split() if name])

        return {
            "title": title.title().strip(".") if title else "",
            "first_name": first_name,
            "middle_name": middle_name,
            "last_name": last_name,
            "initials": initials,
        }

    def set_default_password(self, user) -> None:
        """Set default password for new users."""
        user.set_password("defaultpassword")
        user.save()

    def has_bad_password_hash(self, user) -> bool:
        """Check if user has an MD5-style password hash instead of proper Django hash."""
        if not user.password:
            return False
        # Django password hashes start with algorithm$ (e.g., pbkdf2_sha256$, argon2$, bcrypt$)
        return not user.password.startswith(("pbkdf2_sha256$", "argon2$", "bcrypt$"))

    def fix_bad_password_hash(self, user) -> None:
        """Fix users with bad password hashes by setting default password."""
        if self.has_bad_password_hash(user):
            self.logger.info(f"Fixing bad password hash for user: {user.username}")
            user.set_password("defaultpassword")
            user.save()
            return True
        return False

    def sync_users_and_groups(self, dry_run: bool, batch_size: int):
        """Main sync logic for users and groups."""
        self.logger.info("Starting user and group synchronization...")

        # Fetch data
        oracle_users = self.fetch_oracle_users()
        groups = self.get_or_create_groups()
        roles = self.get_or_create_roles()

        # Get existing users mapped by idno_hmac (deterministic, non-reversible)
        existing_users = {
            user.idno_hmac: user
            for user in User.objects.filter(idno_hmac__isnull=False).exclude(
                idno_hmac=""
            )
        }

        # Also create a username lookup for users without idno or as fallback
        existing_users_by_username = {
            user.username.lower(): user for user in User.objects.all()
        }

        self.logger.info(
            f"Found {len(existing_users)} existing users in local database"
        )

        # Process users in batches
        oracle_user_items = list(oracle_users.items())
        total_batches = (len(oracle_user_items) + batch_size - 1) // batch_size

        for batch_num in range(total_batches):
            start_idx = batch_num * batch_size
            end_idx = min(start_idx + batch_size, len(oracle_user_items))
            batch_users = oracle_user_items[start_idx:end_idx]

            self.logger.info(
                f"Processing batch {batch_num + 1}/{total_batches} ({len(batch_users)} users)"
            )

            if not dry_run:
                with transaction.atomic():
                    self.process_user_batch(
                        batch_users,
                        existing_users,
                        existing_users_by_username,
                        groups,
                        roles,
                    )
            else:
                self.process_user_batch(
                    batch_users,
                    existing_users,
                    existing_users_by_username,
                    groups,
                    roles,
                    dry_run=True,
                )

        # Deactivate users not in Oracle
        self.deactivate_missing_users(oracle_users, existing_users, dry_run)

        # Generate final statistics
        self.generate_final_stats()

    def process_user_batch(
        self,
        batch_users: list,
        existing_users: Dict,
        existing_users_by_username: Dict,
        groups: Dict,
        roles: Dict,
        dry_run: bool = False,
    ) -> None:
        """Process a batch of users from Oracle."""
        for idno, oracle_user in batch_users:
            try:
                # Each user handled individually; outer caller may wrap in a transaction for non-dry-run
                self.process_single_user(
                    idno,
                    oracle_user,
                    existing_users,
                    existing_users_by_username,
                    groups,
                    roles,
                    dry_run,
                )
            except Exception as e:
                # Log the error and continue with next user
                self.logger.error(
                    f"Error processing user {idno}: {str(e)}", exc_info=True
                )
                self.stats["errors"] += 1

    def deactivate_missing_users(
        self,
        oracle_users: Dict[str, Tuple],
        existing_users: Dict[str, User],
        dry_run: bool,
    ) -> None:
        """Deactivate local users that are no longer present in Oracle."""
        # Compare based on HMACs to avoid exposing plaintext
        oracle_hmacs = set(
            filter(
                None,
                [User._compute_idno_hmac(idno) for idno in oracle_users.keys()],
            )
        )
        local_hmacs = set(existing_users.keys())
        missing_hmacs = sorted(local_hmacs - oracle_hmacs)

        if not missing_hmacs:
            self.logger.info(
                "No users to deactivate (all existing users are present in Oracle)"
            )
            return

        today = timezone.now().date()

        for id_hmac in missing_hmacs:
            user = existing_users.get(id_hmac)
            if not user:
                continue

            if not user.is_active:
                self.logger.debug(
                    f"User already inactive, skipping deactivation: {user.username}"
                )
                continue

            active_qs = GroupMembership.objects.filter(user=user, is_active=True)
            active_count = active_qs.count()

            if not dry_run:
                try:
                    user.is_active = False
                    if hasattr(user, "termination_date") and not user.termination_date:
                        user.termination_date = today
                    if hasattr(user, "termination_date"):
                        user.save(update_fields=["is_active", "termination_date"])
                    else:
                        user.save(update_fields=["is_active"])

                    if active_count:
                        active_qs.update(is_active=False, end_date=today)
                except Exception as e:
                    self.logger.error(
                        f"Error deactivating user {user.username} (HMAC): {str(e)}",
                        exc_info=True,
                    )
                    self.stats["errors"] += 1
                    continue

            self.stats["disabled_users"] += 1
            self.stats["deactivated_memberships"] += active_count
            self.logger.info(
                f"{'[DRY RUN] ' if dry_run else ''}Deactivated user: {user.username}; memberships deactivated: {active_count}"
            )

    def process_single_user(
        self,
        idno: str,
        oracle_user: Tuple,
        existing_users: Dict,
        existing_users_by_username: Dict,
        groups: Dict,
        roles: Dict,
        dry_run: bool = False,
    ):
        """Process a single user from Oracle data."""
        # Parse Oracle user data
        (
            title,
            lastname,
            firstname,
            middlenames,
            email,
            _,
            sex,
            positiondesc,
            employeetype,
            partycode,
            username,
            child_org_name,
            parent_org_name,
        ) = oracle_user

        # Normalize names
        name_data = self.normalize_name_fields(oracle_user)

        # Determine user type and group assignment
        is_mp = employeetype == "Member"
        employee_type = "member" if is_mp else "staff"

        # Ensure Role and Group records exist (created only if missing)
        # Ensure normalized role and groups exist
        normalized_role = self.normalize_role_name(
            positiondesc or "", employeetype or ""
        )
        try:
            if not dry_run:
                if normalized_role:
                    self.ensure_role_exists(normalized_role)
                self.ensure_group_hierarchy(child_org_name or "", parent_org_name or "")
            else:
                self.logger.info(
                    f"[DRY RUN] Would ensure role '{normalized_role}' and groups child='{child_org_name or ''}', parent='{parent_org_name or ''}'"
                )
        except Exception as e:
            # Do not block user processing if role/group creation fails
            self.logger.error(
                f"Error ensuring role/group for user {idno} ({username}): {str(e)}",
                exc_info=True,
            )
            self.stats["errors"] += 1

        # Check if user exists by idno_hmac first, then by username as fallback
        user = None
        id_hmac = User._compute_idno_hmac(idno or "")
        if id_hmac and id_hmac in existing_users:
            user = existing_users[id_hmac]
            self.logger.debug(f"Found existing user by idno: {user.username}")
        elif username.lower() in existing_users_by_username:
            user = existing_users_by_username[username.lower()]
            self.logger.debug(f"Found existing user by username: {user.username}")
            # Add to existing_users dict for future lookups
            if id_hmac:
                existing_users[id_hmac] = user

        if user:
            # Update existing user

            # Always update user data from Oracle for all users including admin
            # but preserve is_staff and is_superuser flags for existing users
            is_staff = getattr(user, "is_staff", False)
            is_superuser = getattr(user, "is_superuser", False)

            # Check if username needs to be updated (only if different and not empty)
            new_username = username.lower()
            username_changed = user.username.lower() != new_username and new_username

            try:
                if not dry_run:
                    # Check if the new username already exists for a different user
                    if (
                        username_changed
                        and User.objects.filter(username__iexact=new_username)
                        .exclude(pk=user.pk)
                        .exists()
                    ):
                        self.logger.warning(
                            f"Cannot update username for {user.username} to {new_username}: Username already exists for another user"
                        )
                        username_changed = False

                    # Only proceed with updates if we're not in dry run mode
                    if username_changed:
                        old_username = user.username
                        user.username = new_username

                    # Update other user fields
                    user.email = email or ""
                    user.first_name = name_data["first_name"]
                    user.middle_name = name_data["middle_name"]
                    user.last_name = name_data["last_name"]
                    user.title = name_data["title"]
                    user.positiondesc = positiondesc or ""
                    user.employee_type = employee_type
                    user.party_affiliation = partycode or ""
                    user.gender = sex.lower() if sex else ""
                    user.is_mp = is_mp
                    user.is_active = True
                    user.is_staff = is_staff  # Preserve staff status
                    user.is_superuser = is_superuser  # Preserve superuser status
                    # Update secure ID fields
                    user.set_idno(idno or "")

                    try:
                        user.save()
                        if username_changed:
                            self.logger.info(
                                f"Updated username from {old_username} to {new_username}"
                            )

                        # Check and fix bad password hashes
                        if self.fix_bad_password_hash(user):
                            self.stats["fixed_passwords"] += 1

                    except Exception as e:
                        self.logger.error(
                            f"Error updating user (username={user.username}, hmac={id_hmac}): {str(e)}",
                            exc_info=True,
                        )
                        raise

                # Log the update
                log_message = f"{'[DRY RUN] ' if dry_run else ''}Updated user from Oracle: {user.username}"
                if username_changed:
                    log_message += f" (username updated to: {new_username})"
                self.logger.info(log_message)

                self.stats["updated_users"] += 1

            except Exception as e:
                self.logger.error(
                    f"Error processing user {idno}: {str(e)}", exc_info=True
                )
                self.stats["errors"] += 1
                raise

            # Handle group memberships
            self.handle_group_memberships(user, oracle_user, groups, roles, dry_run)

        else:
            # Create new user
            new_username = username.lower()

            try:
                if not dry_run:
                    # Check if username already exists before creating
                    if User.objects.filter(username__iexact=new_username).exists():
                        self.logger.error(
                            f"Cannot create user with username {new_username}: Username already exists"
                        )
                        self.stats["errors"] += 1
                        return

                    user = User.objects.create(
                        username=new_username,
                        email=email or "",
                        first_name=name_data["first_name"],
                        middle_name=name_data["middle_name"],
                        last_name=name_data["last_name"],
                        title=name_data["title"],
                        positiondesc=positiondesc or "",
                        employee_type=employee_type,
                        party_affiliation=partycode or "",
                        gender=sex.lower() if sex else "",
                        is_mp=is_mp,
                        is_active=True,
                    )
                    # Populate encrypted/HMAC fields
                    user.set_idno(idno or "")
                    user.save(
                        update_fields=[
                            "idno_encrypted",
                            "idno_hmac",
                        ]
                    )

                    # Set default password using Django's proper method
                    self.set_default_password(user)
                    if id_hmac:
                        existing_users[id_hmac] = user

                    # Handle group memberships for new user
                    self.handle_group_memberships(
                        user, oracle_user, groups, roles, dry_run
                    )

                self.stats["new_users"] += 1
                self.logger.info(
                    f"{'[DRY RUN] ' if dry_run else ''}Created new user: {new_username}"
                )

            except Exception as e:
                self.logger.error(
                    f"Error creating user {idno} ({new_username}): {str(e)}",
                    exc_info=True,
                )
                self.stats["errors"] += 1
                return

    def handle_group_memberships(
        self,
        user: User,
        oracle_user: Tuple,
        groups: Dict,
        roles: Dict,
        dry_run: bool = False,
    ):
        """Handle group membership assignments for a user."""
        employeetype = oracle_user[8]
        positiondesc = oracle_user[7]
        child_org_name = oracle_user[11]
        parent_org_name = oracle_user[12]

        # Comprehensive position to role mapping
        position_to_role_mapping = {
            # ICT/Technical Roles
            "Desktop Technician": "Desktop Technician",
            "Website Administrator": "Website Administrator",
            "Service Desk Operator": "Service Desk Operator",
            "Service Desk Supervisor": "Service Desk Supervisor",
            "ECM Analyst Programmer": "ECM Analyst Programmer",
            "Training Officer": "Training Officer",
            "Systems Administrator": "Systems Administrator",
            "Network Administrator": "Network Administrator",
            "Database Administrator": "Database Administrator",
            "IT Support Technician": "IT Support Technician",
            "Software Developer": "Software Developer",
            "Business Analyst": "Business Analyst",
            "Project Manager": "Project Manager",
            # Administrative Roles
            "Administrative Officer": "Administrative Officer",
            "Executive Assistant": "Executive Assistant",
            "Office Manager": "Office Manager",
            "Receptionist": "Receptionist",
            "Personal Assistant": "Personal Assistant",
            # Finance Roles
            "Financial Officer": "Financial Officer",
            "Accountant": "Accountant",
            "Budget Analyst": "Budget Analyst",
            "Financial Controller": "Financial Controller",
            # HR Roles
            "HR Officer": "HR Officer",
            "HR Manager": "HR Manager",
            "Training Coordinator": "Training Coordinator",
            "Recruitment Officer": "Recruitment Officer",
            # Legal/Compliance Roles
            "Legal Advisor": "Legal Advisor",
            "Compliance Officer": "Compliance Officer",
            "Parliamentary Counsel": "Parliamentary Counsel",
            # Communications Roles
            "Communications Officer": "Communications Officer",
            "Media Liaison": "Media Liaison",
            "Public Relations Officer": "Public Relations Officer",
            # Security Roles
            "Security Officer": "Security Officer",
            "Protection Officer": "Protection Officer",
            # Management Roles
            "Director": "Director",
            "Deputy Director": "Deputy Director",
            "Manager": "Manager",
            "Team Leader": "Team Leader",
            "Supervisor": "Supervisor",
            "Coordinator": "Coordinator",
            # Research/Policy Roles
            "Researcher": "Researcher",
            "Policy Analyst": "Policy Analyst",
            "Advisor": "Advisor",
            "Consultant": "Consultant",
            # Parliamentary Roles
            "Clerk": "Clerk",
            "Table Clerk": "Table Clerk",
            "Committee Clerk": "Committee Clerk",
            "Hansard Reporter": "Hansard Reporter",
            "Interpreter": "Interpreter",
            "Translator": "Translator",
            # Facilities/Maintenance
            "Facilities Manager": "Facilities Manager",
            "Maintenance Officer": "Maintenance Officer",
            "Cleaner": "Cleaner",
            "Groundskeeper": "Groundskeeper",
        }

        if employeetype == "Member":
            # Determine house based on position description
            member_house = (
                positiondesc.split(":")[-1].strip() if ":" in positiondesc else ""
            )

            if member_house == "NA":
                target_group = groups["lower_house"]
                role = roles["mp"]
            elif member_house == "NCOP":
                target_group = groups["upper_house"]
                role = roles["mp"]
            else:
                # Default to staff if house cannot be determined
                target_group = groups.get(
                    "staff_group"
                )  # You might want to create a staff group
                role = roles["staff"]
                if not target_group:
                    self.logger.warning(
                        f"No staff group found for user {user.username}"
                    )
                    return

            # Check if membership already exists for house membership
            existing_membership = GroupMembership.objects.filter(
                user=user, group=target_group, role=role
            ).first()

            if not existing_membership:
                if not dry_run:
                    GroupMembership.objects.create(
                        user=user,
                        group=target_group,
                        role=role,
                        start_date=timezone.now().date(),
                        is_active=True,
                    )

                self.stats["new_memberships"] += 1
                self.logger.info(
                    f"{'[DRY RUN] ' if dry_run else ''}Added membership: {user.username} -> {target_group.name} ({role.name})"
                )

            # Add membership for organizational child and parent groups using enhanced role mapping
            # Try to find specific role based on position description
            normalized_position = positiondesc.strip().title()
            specific_role_name = position_to_role_mapping.get(normalized_position)

            if specific_role_name:
                # Create or get the specific role
                role_obj = self.ensure_role_exists(specific_role_name)
                self.logger.info(
                    f"🎯 Mapped position '{positiondesc}' to specific role '{specific_role_name}' for {user.username}"
                )
            else:
                # Use normalized role for unmapped positions
                normalized_role_name = self.normalize_role_name(
                    positiondesc, employeetype
                )
                role_obj = (
                    self.ensure_role_exists(normalized_role_name)
                    if normalized_role_name
                    else None
                )
                if role_obj:
                    self.logger.info(
                        f"📝 Using normalized role '{normalized_role_name}' for unmapped position '{positiondesc}' for {user.username}"
                    )
                    # Log unmapped positions for future reference
                    if positiondesc.strip() and positiondesc.strip() not in getattr(
                        self, "_unmapped_positions", set()
                    ):
                        self._unmapped_positions = getattr(
                            self, "_unmapped_positions", set()
                        )
                        self._unmapped_positions.add(positiondesc.strip())
                        self.logger.info(
                            f"🔍 Unmapped position detected: '{positiondesc}' - consider adding to role mapping"
                        )

            # Child group membership
            org_child_name = self.strip_group_code_prefix(child_org_name or "")
            if role_obj and org_child_name:
                child_group = (
                    self._group_cache.get(f"child::{org_child_name}")
                    or Group.objects.filter(name=org_child_name).first()
                )
                if child_group:
                    exists = GroupMembership.objects.filter(
                        user=user, group=child_group, role=role_obj
                    ).exists()
                    if not exists:
                        if not dry_run:
                            GroupMembership.objects.create(
                                user=user,
                                group=child_group,
                                role=role_obj,
                                start_date=timezone.now().date(),
                                is_active=True,
                            )
                        self.stats["new_memberships"] += 1
                        self.logger.info(
                            f"{'[DRY RUN] ' if dry_run else ''}Added org membership: {user.username} -> {child_group.name} ({role_obj.name})"
                        )

            # Parent group membership
            org_parent_name = self.strip_group_code_prefix(parent_org_name or "")
            if role_obj and org_parent_name:
                parent_group = (
                    self._group_cache.get(f"parent::{org_parent_name}")
                    or Group.objects.filter(name=org_parent_name).first()
                )
                if parent_group:
                    exists = GroupMembership.objects.filter(
                        user=user, group=parent_group, role=role_obj
                    ).exists()
                    if not exists:
                        if not dry_run:
                            GroupMembership.objects.create(
                                user=user,
                                group=parent_group,
                                role=role_obj,
                                start_date=timezone.now().date(),
                                is_active=True,
                            )
                        self.stats["new_memberships"] += 1
                        self.logger.info(
                            f"{'[DRY RUN] ' if dry_run else ''}Added parent org membership: {user.username} -> {parent_group.name} ({role_obj.name})"
                        )
        else:
            # Non-Member: ensure staff membership if staff group present
            target_group = groups.get("staff_group")

            # Try to find specific role based on position description
            normalized_position = positiondesc.strip().title()
            specific_role_name = position_to_role_mapping.get(normalized_position)

            if specific_role_name:
                # Create or get the specific role
                role = self.ensure_role_exists(specific_role_name)
                self.logger.info(
                    f"🎯 Mapped position '{positiondesc}' to specific role '{specific_role_name}' for {user.username}"
                )
            else:
                # Use generic staff role for unmapped positions
                role = roles["staff"]
                self.logger.info(
                    f"📝 Using generic staff role for unmapped position '{positiondesc}' for {user.username}"
                )
                # Log unmapped positions for future reference
                if positiondesc.strip() and positiondesc.strip() not in getattr(
                    self, "_unmapped_positions", set()
                ):
                    self._unmapped_positions = getattr(
                        self, "_unmapped_positions", set()
                    )
                    self._unmapped_positions.add(positiondesc.strip())
                    self.logger.info(
                        f"🔍 Unmapped position detected: '{positiondesc}' - consider adding to role mapping"
                    )

            if target_group:
                existing_membership = GroupMembership.objects.filter(
                    user=user, group=target_group, role=role
                ).first()
                if not existing_membership:
                    if not dry_run:
                        GroupMembership.objects.create(
                            user=user,
                            group=target_group,
                            role=role,
                            start_date=timezone.now().date(),
                            is_active=True,
                        )
                    self.stats["new_memberships"] += 1
                    self.logger.info(
                        f"{'[DRY RUN] ' if dry_run else ''}Added membership: {user.username} -> {target_group.name} ({role.name})"
                    )

            # Also add child/parent org memberships for staff using enhanced role mapping
            if specific_role_name:
                role_obj = self.ensure_role_exists(specific_role_name)
            else:
                normalized_role_name = self.normalize_role_name(
                    positiondesc, employeetype
                )
                role_obj = (
                    self.ensure_role_exists(normalized_role_name)
                    if normalized_role_name
                    else None
                )

            # Child org
            org_child_name = self.strip_group_code_prefix(child_org_name or "")
            if role_obj and org_child_name:
                child_group = (
                    self._group_cache.get(f"child::{org_child_name}")
                    or Group.objects.filter(name=org_child_name).first()
                )
                if child_group:
                    exists = GroupMembership.objects.filter(
                        user=user, group=child_group, role=role_obj
                    ).exists()
                    if not exists:
                        if not dry_run:
                            GroupMembership.objects.create(
                                user=user,
                                group=child_group,
                                role=role_obj,
                                start_date=timezone.now().date(),
                                is_active=True,
                            )
                        self.stats["new_memberships"] += 1
                        self.logger.info(
                            f"{'[DRY RUN] ' if dry_run else ''}Added org membership: {user.username} -> {child_group.name} ({role_obj.name})"
                        )

            # Parent org
            org_parent_name = self.strip_group_code_prefix(parent_org_name or "")
            if role_obj and org_parent_name:
                parent_group = (
                    self._group_cache.get(f"parent::{org_parent_name}")
                    or Group.objects.filter(name=org_parent_name).first()
                )
                if parent_group:
                    exists = GroupMembership.objects.filter(
                        user=user, group=parent_group, role=role_obj
                    ).exists()
                    if not exists:
                        if not dry_run:
                            GroupMembership.objects.create(
                                user=user,
                                group=parent_group,
                                role=role_obj,
                                start_date=timezone.now().date(),
                                is_active=True,
                            )
                        self.stats["new_memberships"] += 1
                        self.logger.info(
                            f"{'[DRY RUN] ' if dry_run else ''}Added parent org membership: {user.username} -> {parent_group.name} ({role_obj.name})"
                        )

    def generate_final_stats(self):
        """Generate final synchronization statistics."""
        # Count active users by type
        active_users = User.objects.filter(is_active=True)
        na_members = (
            active_users.filter(
                is_mp=True,
                memberships__group__name="National Assembly",
                memberships__is_active=True,
            )
            .distinct()
            .count()
        )

        ncop_members = (
            active_users.filter(
                is_mp=True,
                memberships__group__name="National Council of Provinces",
                memberships__is_active=True,
            )
            .distinct()
            .count()
        )

        staff_members = active_users.filter(is_mp=False).count()

        self.logger.info("=== FINAL STATISTICS ===")
        self.logger.info(f"NA Members: {na_members}")
        self.logger.info(f"NCOP Members: {ncop_members}")
        self.logger.info(f"Staff Members: {staff_members}")
        self.logger.info(f"New users created: {self.stats['new_users']}")
        self.logger.info(f"Users updated: {self.stats['updated_users']}")
        self.logger.info(f"Users deactivated: {self.stats['disabled_users']}")
        self.logger.info(f"New memberships: {self.stats['new_memberships']}")
        self.logger.info(f"New roles created: {self.stats['new_roles']}")
        self.logger.info(f"New groups created: {self.stats['new_groups']}")
        self.logger.info(
            f"Deactivated memberships: {self.stats['deactivated_memberships']}"
        )
        self.logger.info(f"Errors encountered: {self.stats['errors']}")

        # Report unmapped positions for future reference
        unmapped_positions = getattr(self, "_unmapped_positions", set())
        if unmapped_positions:
            self.logger.info("")
            self.logger.info("🔍 UNMAPPED POSITIONS DETECTED:")
            self.logger.info("   Consider adding these positions to the role mapping:")
            for position in sorted(unmapped_positions):
                self.logger.info(f"   • '{position}'")
            self.logger.info("")
            self.logger.info(
                "💡 To add these positions, update the position_to_role_mapping"
            )
            self.logger.info("   dictionary in the handle_group_memberships method.")
        else:
            self.logger.info("")
            self.logger.info("✅ All positions were successfully mapped to roles!")

    def cleanup_connections(self):
        """Clean up database connections."""
        if self.oracle_cursor:
            self.oracle_cursor.close()
        if self.oracle_conn:
            self.oracle_conn.close()
        self.logger.info("Oracle connections closed")

    def print_summary(self):
        """Print summary of the sync operation."""
        duration = self.stats["end_time"] - self.stats["start_time"]

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(self.style.SUCCESS("ORACLE USER SYNC COMPLETED"))
        self.stdout.write("=" * 70)
        self.stdout.write(f"Duration: {duration:.2f} seconds")
        self.stdout.write(f"New users created: {self.stats['new_users']}")
        self.stdout.write(f"Users updated: {self.stats['updated_users']}")
        self.stdout.write(f"Users deactivated: {self.stats['disabled_users']}")
        self.stdout.write(f"New memberships: {self.stats['new_memberships']}")
        self.stdout.write(f"New roles created: {self.stats.get('new_roles', 0)}")
        self.stdout.write(f"New groups created: {self.stats.get('new_groups', 0)}")
        self.stdout.write(f"Fixed password hashes: {self.stats['fixed_passwords']}")
        self.stdout.write(f"Errors: {self.stats['errors']}")
        self.stdout.write(
            f"Deactivated memberships: {self.stats['deactivated_memberships']}"
        )

        if self.stats["errors"] > 0:
            self.stdout.write(
                self.style.ERROR(f"Errors encountered: {self.stats['errors']}")
            )
        else:
            self.stdout.write(self.style.SUCCESS("No errors encountered"))

        self.stdout.write("=" * 70)
