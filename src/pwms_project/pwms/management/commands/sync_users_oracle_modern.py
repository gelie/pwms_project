"""
Modern Django management command to sync users and groups from Oracle database to local SQLite3 database.

This command uses the modern oracledb library and provides enhanced error handling,
batch processing, and comprehensive logging.

Usage:
    python manage.py sync_users_oracle_modern
    python manage.py sync_users_oracle_modern --dry-run
    python manage.py sync_users_oracle_modern --verbose --batch-size 50
"""

import logging
from time import perf_counter
from typing import Dict, List, Tuple

try:
    import oracledb
except ImportError:
    # Fallback to cx_Oracle if oracledb is not available
    import cx_Oracle as oracledb

from decouple import config
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from workflows.models import Group, GroupMembership, Role, User


class Command(BaseCommand):
    help = "Sync users and groups from Oracle database to local SQLite3 database (Modern version)"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = self.setup_logging()
        self.oracle_conn = None
        self.oracle_cursor = None

        # Sync statistics
        self.stats = {
            "oracle_users_fetched": 0,
            "local_users_found": 0,
            "new_users": 0,
            "updated_users": 0,
            "disabled_users": 0,
            "new_memberships": 0,
            "updated_memberships": 0,
            "deactivated_memberships": 0,
            "fixed_passwords": 0,
            "new_roles": 0,
            "new_groups": 0,
            "errors": 0,
            "warnings": 0,
            "start_time": None,
            "end_time": None,
        }

        # Simple caches to avoid duplicate lookups/creates within a run
        self._role_cache = {}
        self._group_cache = {}
        self._batch_memberships = {}  # Batch-level membership cache for performance

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
        parser.add_argument(
            "--force-update",
            action="store_true",
            help="Force update all users even if no changes detected",
        )
        parser.add_argument(
            "--skip-deactivation",
            action="store_true",
            help="Skip deactivation of users not found in Oracle",
        )

    def setup_logging(self) -> logging.Logger:
        """Set up comprehensive logging configuration."""
        logger = logging.getLogger("oracle_user_sync_modern")
        logger.setLevel(logging.DEBUG)

        # Remove existing handlers to avoid duplicates
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)

        # Create file handler with rotation
        import os
        from logging.handlers import RotatingFileHandler

        os.makedirs("logs", exist_ok=True)

        file_handler = RotatingFileHandler(
            "logs/oracle_user_sync_modern.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=10,
        )
        file_handler.setLevel(logging.DEBUG)

        # Create console handler
        # console_handler = logging.StreamHandler()
        # console_handler.setLevel(logging.INFO)

        # Create formatter
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s"
        )
        file_handler.setFormatter(formatter)
        # console_handler.setFormatter(formatter)

        # Add handlers to logger
        logger.addHandler(file_handler)
        # logger.addHandler(console_handler)

        return logger

    def handle(self, *args, **options):
        """Main command handler with comprehensive error handling."""
        self.stats["start_time"] = perf_counter()

        # Validate environment variables
        self.validate_environment()

        # Set verbosity
        if options["verbose"]:
            self.logger.setLevel(logging.DEBUG)
            for handler in self.logger.handlers:
                if isinstance(handler, logging.StreamHandler):
                    handler.setLevel(logging.DEBUG)

        dry_run = options["dry_run"]
        batch_size = options["batch_size"]
        force_update = options["force_update"]
        skip_deactivation = options["skip_deactivation"]

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "🔍 DRY RUN MODE - No changes will be made to the database"
                )
            )
            self.logger.info("Starting Oracle user sync in DRY RUN mode")
        else:
            self.logger.info("Starting Oracle user sync")

        try:
            self.connect_to_oracle()
            self.sync_users_and_groups(
                dry_run, batch_size, force_update, skip_deactivation
            )

            if self.stats["errors"] == 0:
                self.stdout.write(self.style.SUCCESS("✅ Sync completed successfully"))
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"⚠️  Sync completed with {self.stats['errors']} errors"
                    )
                )

        except Exception as e:
            self.logger.error(f"Sync failed with error: {str(e)}", exc_info=True)
            self.stats["errors"] += 1
            raise CommandError(f"❌ Sync failed: {str(e)}")

        finally:
            self.cleanup_connections()
            self.stats["end_time"] = perf_counter()
            self.print_summary()

    def validate_environment(self):
        """Validate required environment variables."""
        required_vars = ["ORA_USER", "ORA_PWD", "ORA_HOST", "ORA_PORT", "ORA_SERVICE"]
        missing_vars = [var for var in required_vars if not config(var)]

        if missing_vars:
            raise CommandError(
                f"Missing required environment variables: {', '.join(missing_vars)}\n"
                f"Please check your .env file or environment configuration."
            )

    def connect_to_oracle(self):
        """Establish connection to Oracle database using modern oracledb library."""
        try:
            # Check if we have the modern oracledb library (python-oracledb)
            # Modern oracledb has init_oracle_client for thick mode support
            is_modern_oracledb = hasattr(oracledb, "init_oracle_client")

            if is_modern_oracledb:
                # Modern oracledb library (python-oracledb)
                self.logger.info("🔧 Using modern oracledb library")

                # Initialize thick mode (REQUIRED for Network Encryption)
                self.logger.info(
                    "🔧 Initializing Oracle thick mode (required for network encryption)..."
                )
                thick_mode_success = False
                last_error = None

                try:
                    # Try to find Oracle Instant Client
                    import platform

                    # Common Oracle client paths
                    oracle_client_paths = []
                    if platform.system() == "Linux":
                        oracle_client_paths = [
                            "/usr/lib/oracle/*/client64/lib",
                            "/opt/oracle/instantclient*",
                            "/usr/local/lib",
                            "/usr/lib64",
                        ]
                    elif platform.system() == "Darwin":  # macOS
                        oracle_client_paths = [
                            "/usr/local/lib",
                            "/opt/oracle/instantclient*",
                        ]
                    elif platform.system() == "Windows":
                        oracle_client_paths = [
                            "C:\\oracle\\instantclient*",
                            "C:\\Program Files\\Oracle\\*",
                        ]

                    # Try initializing thick mode with different approaches
                    init_attempts = [
                        # Method 1: Default initialization
                        lambda: oracledb.init_oracle_client(),
                        # Method 2: With lib_dir (if we can find it)
                        lambda: self._try_thick_mode_with_paths(oracle_client_paths),
                    ]

                    for i, init_attempt in enumerate(init_attempts, 1):
                        try:
                            init_attempt()
                            thick_mode_success = True
                            self.logger.info(
                                f"✅ Initialized Oracle thick mode (method {i})"
                            )
                            break
                        except Exception as init_error:
                            last_error = init_error
                            self.logger.debug(
                                f"Thick mode init attempt {i} failed: {init_error}"
                            )
                            continue

                    if not thick_mode_success:
                        error_msg = (
                            "❌ Failed to initialize Oracle thick mode (REQUIRED for network encryption).\n\n"
                            "Your Oracle database requires Network Encryption, which is only supported in thick mode.\n"
                            "Please install Oracle Instant Client:\n\n"
                            "Linux:\n"
                            "  1. Download from: https://www.oracle.com/database/technologies/instant-client/downloads.html\n"
                            "  2. Extract to /opt/oracle/instantclient_XX_X/\n"
                            "  3. Set LD_LIBRARY_PATH: export LD_LIBRARY_PATH=/opt/oracle/instantclient_XX_X:$LD_LIBRARY_PATH\n\n"
                            "macOS:\n"
                            "  1. Download from: https://www.oracle.com/database/technologies/instant-client/macos-intel-x86-downloads.html\n"
                            "  2. Extract to /usr/local/lib/\n\n"
                            f"Last error: {last_error}"
                        )
                        self.logger.error(error_msg)
                        raise CommandError(error_msg)

                except CommandError:
                    raise
                except Exception as thick_error:
                    error_msg = f"❌ Thick mode initialization failed: {thick_error}"
                    self.logger.error(error_msg)
                    raise CommandError(error_msg)

                # Try multiple connection methods
                connection_attempts = [
                    # Method 1: Direct parameters
                    lambda: oracledb.connect(
                        user=config("ORA_USER"),
                        password=config("ORA_PWD"),
                        host=config("ORA_HOST"),
                        port=int(config("ORA_PORT", 1521)),
                        service_name=config("ORA_SERVICE"),
                    ),
                    # Method 2: DSN style
                    lambda: oracledb.connect(
                        user=config("ORA_USER"),
                        password=config("ORA_PWD"),
                        dsn=f"{config('ORA_HOST')}:{config('ORA_PORT')}/{config('ORA_SERVICE')}",
                    ),
                    # Method 3: Connection string
                    lambda: oracledb.connect(
                        f"{config('ORA_USER')}/{config('ORA_PWD')}@{config('ORA_HOST')}:{config('ORA_PORT')}/{config('ORA_SERVICE')}"
                    ),
                    # Method 4: With explicit protocol
                    lambda: oracledb.connect(
                        user=config("ORA_USER"),
                        password=config("ORA_PWD"),
                        dsn=f"(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST={config('ORA_HOST')})(PORT={config('ORA_PORT')}))(CONNECT_DATA=(SERVICE_NAME={config('ORA_SERVICE')})))",
                    ),
                ]

                last_error = None
                for i, attempt in enumerate(connection_attempts, 1):
                    try:
                        self.oracle_conn = attempt()
                        self.logger.info(
                            f"✅ Connected using modern oracledb (method {i})"
                        )
                        break
                    except Exception as e:
                        last_error = e
                        self.logger.warning(f"Connection attempt {i} failed: {e}")
                        continue
                else:
                    # All modern attempts failed
                    raise last_error

            else:
                # Legacy cx_Oracle style connection
                self.logger.info("🔧 Using cx_Oracle compatibility mode")
                oracle_connection_string = (
                    f"{config('ORA_USER')}/{config('ORA_PWD')}@"
                    f"{config('ORA_HOST')}:{config('ORA_PORT')}/{config('ORA_SERVICE')}"
                )
                self.oracle_conn = oracledb.connect(oracle_connection_string)
                self.logger.info("✅ Connected using cx_Oracle compatibility mode")

            self.oracle_cursor = self.oracle_conn.cursor()

            # Test connection
            self.oracle_cursor.execute("SELECT 1 FROM DUAL")
            self.oracle_cursor.fetchone()

            self.logger.info("  Successfully connected to Oracle database")
            self.stdout.write(self.style.SUCCESS("Connected to Oracle database"))

        except Exception as e:
            self.logger.error(f" Oracle connection failed: {e}")
            raise CommandError(f" Oracle connection failed: {e}")

    def _try_thick_mode_with_paths(self, oracle_client_paths):
        """Try to initialize thick mode with different Oracle client paths."""
        import glob
        import os

        for path_pattern in oracle_client_paths:
            # Expand wildcards in paths
            expanded_paths = glob.glob(path_pattern)
            for path in expanded_paths:
                if os.path.exists(path) and os.path.isdir(path):
                    try:
                        oracledb.init_oracle_client(lib_dir=path)
                        self.logger.info(f"  Found Oracle client at: {path}")
                        return
                    except Exception as e:
                        self.logger.debug(f"Failed to init with path {path}: {e}")
                        continue

        # If no paths worked, try without lib_dir
        raise Exception("No valid Oracle client library found in standard paths")

    def fetch_oracle_users(self) -> Dict[str, Tuple]:
        """Fetch users from Oracle database with enhanced error handling."""
        self.logger.info("")

        query = """
            SELECT TITLE, LASTNAME, FIRSTNAME, MIDDLENAMES, EMAIL, IDNO, SEX,
                   POSITIONDESC, EMPLOYEETYPE, PARTYCODE, USERNAME, CHILD_ORG_NAME, PARENT_ORG_NAME
            FROM APPS.XXPER_PEOPLE_INTERFACE
            WHERE CURRENT_EMPLOYEE_FLAG = 'Y'
            AND ASSIGNMENT_STATUS = 'Active Assignment'
            AND EMAIL IS NOT NULL
            AND IDNO IS NOT NULL
            ORDER BY LASTNAME, FIRSTNAME
        """

        try:
            self.oracle_cursor.execute(query)
            rows = self.oracle_cursor.fetchall()

            oracle_users = {}
            duplicate_ids = []

            for row in rows:
                idno = row[5]  # IDNO is at index 5
                if idno in oracle_users:
                    duplicate_ids.append(idno)
                    self.logger.warning(f"⚠️  Duplicate IDNO found: {idno}")
                    self.stats["warnings"] += 1
                else:
                    oracle_users[idno] = row

            if duplicate_ids:
                self.logger.warning(
                    f"Found {len(duplicate_ids)} duplicate ID numbers in Oracle data"
                )

            self.stats["oracle_users_fetched"] = len(oracle_users)
            self.logger.info(f"📊 Fetched {len(oracle_users)} unique users from Oracle")
            return oracle_users

        except Exception as e:
            self.logger.error(f"Failed to fetch Oracle users: {str(e)}")
            raise CommandError(f"Failed to fetch Oracle users: {str(e)}")

    def get_or_create_groups(self) -> Dict[str, Group]:
        """Get or create required groups with enhanced logging."""
        self.logger.info("🏛️  Setting up parliamentary groups...")
        groups = {}

        try:
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
                self.logger.info("✨ Created Parliament group")

            # Get or create National Assembly
            na_group, created = Group.objects.get_or_create(
                name="National Assembly",
                defaults={
                    "group_type": "house",
                    "description": "National Assembly of South Africa",
                    "parent": parliament,
                    "is_active": True,
                },
            )
            if created:
                self.logger.info("✨ Created National Assembly group")
            groups["lower_house"] = na_group

            # Get or create National Council of Provinces
            ncop_group, created = Group.objects.get_or_create(
                name="National Council of Provinces",
                defaults={
                    "group_type": "house",
                    "description": "National Council of Provinces",
                    "parent": parliament,
                    "is_active": True,
                },
            )
            if created:
                self.logger.info("✨ Created NCOP group")
            groups["upper_house"] = ncop_group

            # Create staff group
            staff_group, created = Group.objects.get_or_create(
                name="Administration",
                defaults={
                    "group_type": "administration",
                    "description": "Parliamentary Staff Members",
                    "parent": parliament,
                    "is_active": True,
                },
            )
            if created:
                self.logger.info("✨ Created Parliamentary Staff group")
            groups["staff_group"] = staff_group

            return groups

        except Exception as e:
            self.logger.error(f"Failed to setup groups: {str(e)}")
            raise CommandError(f"Failed to setup groups: {str(e)}")

    def get_or_create_roles(self) -> Dict[str, Role]:
        """Get or create required roles with enhanced logging."""
        self.logger.info("👥 Setting up parliamentary roles...")
        roles = {}

        try:
            member_role, created = Role.objects.get_or_create(
                name="Member of Parliament",
                defaults={
                    "description": "Member of Parliament role with legislative powers",
                },
            )
            if created:
                self.logger.info("✨ Created MP role")
            roles["mp"] = member_role

            staff_role, created = Role.objects.get_or_create(
                name="Staff Member",
                defaults={
                    "description": "Parliamentary staff member role",
                },
            )
            if created:
                self.logger.info("✨ Created Staff role")
            roles["staff"] = staff_role

            return roles

        except Exception as e:
            self.logger.error(f"Failed to setup roles: {str(e)}")
            raise CommandError(f"Failed to setup roles: {str(e)}")

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
                self.logger.info(f"✨ Created parent Group: {parent_name}")
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
                    f"✨ Created child Group: {child_name}{' (parent: ' + parent_name + ')' if parent_group else ''}"
                )
            self._group_cache[child_key] = child_group

        return child_group, parent_group

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
        """Normalize and clean name fields from Oracle data with validation."""
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
            first_name = "UNKNOWN"
            self.stats["warnings"] += 1

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

    def fix_bad_password_hash(self, user) -> bool:
        """Fix users with bad password hashes by setting default password."""
        if self.has_bad_password_hash(user):
            self.logger.info(f"🔧 Fixing bad password hash for user: {user.username}")
            user.set_password("defaultpassword")
            user.save()
            return True
        return False

    def sync_users_and_groups(
        self,
        dry_run: bool,
        batch_size: int,
        force_update: bool,
        skip_deactivation: bool,
    ):
        """Main sync logic with enhanced batch processing."""
        self.logger.info("🔄 Starting comprehensive user and group synchronization...")

        # Fetch data
        oracle_users = self.fetch_oracle_users()
        groups = self.get_or_create_groups()
        roles = self.get_or_create_roles()

        # Map existing users by idno_hmac (deterministic and non-reversible)
        existing_users = {
            user.idno_hmac: user
            for user in User.objects.filter(idno_hmac__isnull=False).exclude(
                idno_hmac=""
            )
        }
        # Fallback map by username for users without ID numbers
        existing_users_by_username = {u.username.lower(): u for u in User.objects.all()}

        self.stats["local_users_found"] = len(existing_users)
        self.logger.info(
            f"📊 Found {len(existing_users)} existing users in local database"
        )

        # Process users in batches
        oracle_user_items = list(oracle_users.items())
        total_batches = (len(oracle_user_items) + batch_size - 1) // batch_size

        self.logger.info(
            f"📦 Processing {len(oracle_user_items)} users in {total_batches} batches"
        )

        for batch_num in range(total_batches):
            start_idx = batch_num * batch_size
            end_idx = min(start_idx + batch_size, len(oracle_user_items))
            batch_users = oracle_user_items[start_idx:end_idx]

            self.logger.info(
                f"🔄 Processing batch {batch_num + 1}/{total_batches} ({len(batch_users)} users)"
            )

            # Process each user individually to prevent transaction rollback issues
            self.process_user_batch(
                batch_users,
                existing_users,
                existing_users_by_username,
                groups,
                roles,
                force_update,
                dry_run,
            )

        # Deactivate users not in Oracle
        if not skip_deactivation:
            self.deactivate_missing_users(oracle_users, existing_users, dry_run)
        else:
            self.logger.info("⏭️  Skipping user deactivation as requested")

        # Generate final statistics
        self.generate_final_stats()

    def process_user_batch(
        self,
        batch_users: List,
        existing_users: Dict,
        existing_users_by_username: Dict,
        groups: Dict,
        roles: Dict,
        force_update: bool,
        dry_run: bool = False,
    ):
        """Process a batch of users with enhanced error handling."""
        # Prefetch existing memberships for this batch to reduce queries
        batch_user_ids = []
        for idno, oracle_user in batch_users:
            id_hmac = User._compute_idno_hmac(idno or "")
            username = oracle_user[1]

            # Check both idno_hmac and username lookups
            user = None
            if id_hmac and id_hmac in existing_users:
                user = existing_users[id_hmac]
            elif username and username.lower() in existing_users_by_username:
                user = existing_users_by_username[username.lower()]

            if user:
                batch_user_ids.append(user.id)

        # Prefetch all memberships for users in this batch
        existing_memberships = {}
        if batch_user_ids:
            memberships = GroupMembership.objects.filter(
                user_id__in=batch_user_ids
            ).select_related("group", "role")

            for membership in memberships:
                key = (membership.user_id, membership.group_id, membership.role_id)
                existing_memberships[key] = membership

        # Store in instance for access in handle_group_memberships
        self._batch_memberships = existing_memberships

        for idno, oracle_user in batch_users:
            try:
                self.process_single_user(
                    idno,
                    oracle_user,
                    existing_users,
                    existing_users_by_username,
                    groups,
                    roles,
                    force_update,
                    dry_run,
                )
            except Exception as e:
                self.logger.error(
                    f"❌ Error processing user {idno}: {str(e)}", exc_info=True
                )
                self.stats["errors"] += 1

        # Clear batch cache
        self._batch_memberships = {}

    def process_single_user(
        self,
        idno: str,
        oracle_user: Tuple,
        existing_users: Dict,
        existing_users_by_username: Dict,
        groups: Dict,
        roles: Dict,
        force_update: bool,
        dry_run: bool = False,
    ):
        """Process a single user with comprehensive change detection."""
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

        # Validate required fields
        if not email or not username:
            self.logger.warning(f"⚠️  Skipping user {idno}: missing email or username")
            self.stats["warnings"] += 1
            return

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

        id_hmac = User._compute_idno_hmac(idno or "")
        user = None
        if id_hmac and id_hmac in existing_users:
            user = existing_users[id_hmac]
        elif username.lower() in existing_users_by_username:
            user = existing_users_by_username[username.lower()]
            if id_hmac:
                existing_users[id_hmac] = user

        if user:
            # Update existing user
            changes = []

            # Detailed change detection
            if user.username != username.lower():
                changes.append(f"username: {user.username} -> {username.lower()}")
            if user.email != email:
                changes.append(f"email: {user.email} -> {email}")
            if user.first_name != name_data["first_name"]:
                changes.append(
                    f"first_name: {user.first_name} -> {name_data['first_name']}"
                )
            if user.middle_name != name_data["middle_name"]:
                changes.append(
                    f"middle_name: {user.middle_name} -> {name_data['middle_name']}"
                )
            if user.last_name != name_data["last_name"]:
                changes.append(
                    f"last_name: {user.last_name} -> {name_data['last_name']}"
                )
            if user.title != name_data["title"]:
                changes.append(f"title: {user.title} -> {name_data['title']}")
            if user.positiondesc != positiondesc:
                changes.append(f"position: {user.positiondesc} -> {positiondesc}")
            if user.employee_type != employee_type:
                changes.append(
                    f"employee_type: {user.employee_type} -> {employee_type}"
                )
            if user.party_affiliation != partycode:
                changes.append(f"party: {user.party_affiliation} -> {partycode}")
            if user.is_mp != is_mp:
                changes.append(f"is_mp: {user.is_mp} -> {is_mp}")

            if changes or force_update:
                if not dry_run:
                    user.username = username.lower()
                    user.email = email
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
                    user.set_idno(idno or "")
                    user.save()

                    # Check and fix bad password hashes
                    if self.fix_bad_password_hash(user):
                        self.stats["fixed_passwords"] += 1

                self.stats["updated_users"] += 1
                if changes:
                    self.logger.info(
                        f"🔄 {'[DRY RUN] ' if dry_run else ''}Updated user {username}: {'; '.join(changes[:3])}"
                    )
                else:
                    self.logger.debug(
                        f"🔄 {'[DRY RUN] ' if dry_run else ''}Force updated user: {username}"
                    )
            else:
                # Even if no changes, check password hash
                if not dry_run:
                    if self.fix_bad_password_hash(user):
                        self.stats["fixed_passwords"] += 1

            # Handle group memberships
            self.handle_group_memberships(user, oracle_user, groups, roles, dry_run)

        else:
            # Create new user
            if not dry_run:
                user = User.objects.create(
                    username=username.lower(),
                    email=email,
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
                user.set_idno(idno or "")
                user.save(update_fields=["idno_encrypted", "idno_hmac"])

                # Set default password using Django's proper method
                self.set_default_password(user)

                if id_hmac:
                    existing_users[id_hmac] = user

            self.stats["new_users"] += 1
            self.logger.info(
                f"✨ {'[DRY RUN] ' if dry_run else ''}Created new user: {username} ({name_data['first_name']} {name_data['last_name']})"
            )

            # Handle group memberships for new user
            if not dry_run:
                self.handle_group_memberships(user, oracle_user, groups, roles, dry_run)

    def handle_group_memberships(
        self,
        user: User,
        oracle_user: Tuple,
        groups: Dict,
        roles: Dict,
        dry_run: bool = False,
    ):
        """Handle group membership assignments with comprehensive logic."""
        employeetype = oracle_user[8]
        positiondesc = oracle_user[7]

        target_group = None
        role = None

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
                # Log warning for unclear house assignment
                self.logger.warning(
                    f"⚠️  Unclear house assignment for MP {user.username}: {positiondesc}"
                )
                target_group = groups["lower_house"]  # Default to NA
                role = roles["mp"]
                self.stats["warnings"] += 1
        else:
            # Staff member - use enhanced role mapping
            target_group = groups["staff_group"]

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

        if target_group and role:
            # Check if membership already exists using prefetched cache
            membership_key = (user.id, target_group.id, role.id)
            existing_membership = self._batch_memberships.get(membership_key)

            # Fallback to database query if not in cache (safety check)
            if not existing_membership:
                existing_membership = GroupMembership.objects.filter(
                    user=user, group=target_group, role=role
                ).first()
                if existing_membership:
                    # Add to cache for future lookups
                    self._batch_memberships[membership_key] = existing_membership

            if not existing_membership:
                if not dry_run:
                    membership = GroupMembership.objects.create(
                        user=user,
                        group=target_group,
                        role=role,
                        start_date=timezone.now().date(),
                        is_active=True,
                    )
                    # Add to cache to prevent duplicate creation
                    self._batch_memberships[membership_key] = membership

                self.stats["new_memberships"] += 1
                self.logger.info(
                    f"👥 {'[DRY RUN] ' if dry_run else ''}Added membership: {user.username} -> {target_group.name} ({role.name})"
                )
            elif not existing_membership.is_active:
                # Reactivate membership
                if not dry_run:
                    existing_membership.is_active = True
                    existing_membership.end_date = None
                    existing_membership.save()

                self.stats["updated_memberships"] += 1
                self.logger.info(
                    f"🔄 {'[DRY RUN] ' if dry_run else ''}Reactivated membership: {user.username} -> {target_group.name}"
                )

        # Add membership for organizational child and parent groups using normalized role
        child_org_name = oracle_user[11]
        parent_org_name = oracle_user[12]

        normalized_role_name = self.normalize_role_name(positiondesc, employeetype)
        role_obj = (
            self.ensure_role_exists(normalized_role_name)
            if normalized_role_name
            else None
        )

        # Child group membership
        org_child_name = self.strip_group_code_prefix(child_org_name or "")
        if role_obj and org_child_name:
            child_group = (
                self._group_cache.get(f"child::{org_child_name}")
                or Group.objects.filter(name=org_child_name).first()
            )
            if child_group:
                membership_key = (user.id, child_group.id, role_obj.id)
                exists = membership_key in self._batch_memberships

                # Fallback to database query if not in cache
                if not exists:
                    existing = GroupMembership.objects.filter(
                        user=user, group=child_group, role=role_obj
                    ).first()
                    if existing:
                        self._batch_memberships[membership_key] = existing
                        exists = True

                if not exists:
                    if not dry_run:
                        membership = GroupMembership.objects.create(
                            user=user,
                            group=child_group,
                            role=role_obj,
                            start_date=timezone.now().date(),
                            is_active=True,
                        )
                        # Add to cache to prevent duplicate creation
                        self._batch_memberships[membership_key] = membership
                    self.stats["new_memberships"] += 1
                    self.logger.info(
                        f"👥 {'[DRY RUN] ' if dry_run else ''}Added org membership: {user.username} -> {child_group.name} ({role_obj.name})"
                    )

        # Parent group membership
        org_parent_name = self.strip_group_code_prefix(parent_org_name or "")
        if role_obj and org_parent_name:
            parent_group = (
                self._group_cache.get(f"parent::{org_parent_name}")
                or Group.objects.filter(name=org_parent_name).first()
            )
            if parent_group:
                membership_key = (user.id, parent_group.id, role_obj.id)
                exists = membership_key in self._batch_memberships

                # Fallback to database query if not in cache
                if not exists:
                    existing = GroupMembership.objects.filter(
                        user=user, group=parent_group, role=role_obj
                    ).first()
                    if existing:
                        self._batch_memberships[membership_key] = existing
                        exists = True

                if not exists:
                    if not dry_run:
                        membership = GroupMembership.objects.create(
                            user=user,
                            group=parent_group,
                            role=role_obj,
                            start_date=timezone.now().date(),
                            is_active=True,
                        )
                        # Add to cache to prevent duplicate creation
                        self._batch_memberships[membership_key] = membership
                    self.stats["new_memberships"] += 1
                    self.logger.info(
                        f"👥 {'[DRY RUN] ' if dry_run else ''}Added parent org membership: {user.username} -> {parent_group.name} ({role_obj.name})"
                    )

    def deactivate_missing_users(
        self, oracle_users: Dict, existing_users: Dict, dry_run: bool
    ):
        """Deactivate users that are no longer in Oracle with detailed logging."""
        self.logger.info("🔍 Checking for users to deactivate...")

        # Users missing from Oracle should be disabled (compare via HMAC)
        oracle_hmacs = set(
            filter(None, [User._compute_idno_hmac(k) for k in oracle_users.keys()])
        )
        local_hmacs = set(existing_users.keys())
        missing_hmacs = sorted(local_hmacs - oracle_hmacs)

        for id_hmac in missing_hmacs:
            user = existing_users.get(id_hmac)
            if not user:
                continue
            if not user.is_active:
                self.logger.debug(f"User already inactive: {user.username}")
                continue
            if dry_run:
                self.logger.info(f"[DRY RUN] Would deactivate user: {user.username}")
                continue
            user.is_active = False
            user.save(update_fields=["is_active"])
            self.logger.info(f"Deactivated user: {user.username}")
            self.stats["disabled_users"] += 1

        self.logger.info(f"⚠️  Found {len(missing_hmacs)} users to deactivate")

    def generate_final_stats(self):
        """Generate comprehensive final synchronization statistics."""
        self.logger.info("📊 Generating final statistics...")

        try:
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
            total_active = active_users.count()
            total_inactive = User.objects.filter(is_active=False).count()

            # Active memberships
            active_memberships = GroupMembership.objects.filter(is_active=True).count()

            self.logger.info("=" * 60)
            self.logger.info("📈 FINAL SYNCHRONIZATION STATISTICS")
            self.logger.info("=" * 60)
            self.logger.info("🏛️  Parliamentary Composition:")
            self.logger.info(f"   • NA Members: {na_members}")
            self.logger.info(f"   • NCOP Members: {ncop_members}")
            self.logger.info(f"   • Staff Members: {staff_members}")
            self.logger.info(f"   • Total Active Users: {total_active}")
            self.logger.info(f"   • Total Inactive Users: {total_inactive}")
            self.logger.info(f"   • Active Memberships: {active_memberships}")
            self.logger.info("")
            self.logger.info("🔄 Sync Operations:")
            self.logger.info(
                f"   • Oracle users fetched: {self.stats['oracle_users_fetched']}"
            )
            self.logger.info(
                f"   • Local users found: {self.stats['local_users_found']}"
            )
            self.logger.info(f"   • New users created: {self.stats['new_users']}")
            self.logger.info(f"   • Users updated: {self.stats['updated_users']}")
            self.logger.info(f"   • Users deactivated: {self.stats['disabled_users']}")
            self.logger.info(f"   • New memberships: {self.stats['new_memberships']}")
            self.logger.info(
                f"   • Memberships updated: {self.stats['updated_memberships']}"
            )
            self.logger.info(
                f"   • Memberships deactivated: {self.stats['deactivated_memberships']}"
            )
            self.logger.info(f"   • New roles created: {self.stats['new_roles']}")
            self.logger.info(f"   • New groups created: {self.stats['new_groups']}")
            self.logger.info(f"   • Errors: {self.stats['errors']}")
            self.logger.info(f"   • Warnings: {self.stats['warnings']}")

            # Report unmapped positions for future reference
            unmapped_positions = getattr(self, "_unmapped_positions", set())
            if unmapped_positions:
                self.logger.info("")
                self.logger.info("🔍 UNMAPPED POSITIONS DETECTED:")
                self.logger.info(
                    "   Consider adding these positions to the role mapping:"
                )
                for position in sorted(unmapped_positions):
                    self.logger.info(f"   • '{position}'")
                self.logger.info("")
                self.logger.info(
                    "💡 To add these positions, update the position_to_role_mapping"
                )
                self.logger.info(
                    "   dictionary in the handle_group_memberships method."
                )
            else:
                self.logger.info("")
                self.logger.info("✅ All positions were successfully mapped to roles!")

            self.logger.info("")
            self.logger.info("⏱️  Performance Metrics:")
            if self.stats["start_time"] and self.stats["end_time"]:
                duration = self.stats["end_time"] - self.stats["start_time"]
                self.logger.info(f"   • Total duration: {duration:.2f} seconds")
                self.logger.info(
                    f"   • Users per second: {self.stats['oracle_users_fetched'] / max(duration, 0.001):.1f}"
                )

            self.logger.info("=" * 60)
            self.logger.info("")
            self.logger.info("⚠️  Issues:")
            self.logger.info(f"   • Errors encountered: {self.stats['errors']}")
            self.logger.info(f"   • Warnings issued: {self.stats['warnings']}")
            self.logger.info("=" * 60)

        except Exception as e:
            self.logger.error(f"❌ Error generating final stats: {str(e)}")
            self.stats["errors"] += 1

    def cleanup_connections(self):
        """Clean up database connections with error handling."""
        try:
            if self.oracle_cursor:
                self.oracle_cursor.close()
                self.logger.debug("Oracle cursor closed")
        except Exception as e:
            self.logger.warning(f"Error closing Oracle cursor: {str(e)}")

        try:
            if self.oracle_conn:
                self.oracle_conn.close()
                self.logger.debug("Oracle connection closed")
        except Exception as e:
            self.logger.warning(f"Error closing Oracle connection: {str(e)}")

        self.logger.info("🔌 Database connections cleaned up")

    def print_summary(self):
        """Print comprehensive summary of the sync operation."""
        duration = self.stats["end_time"] - self.stats["start_time"]

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(self.style.SUCCESS("🏛️  ORACLE USER SYNC COMPLETED (MODERN)"))
        self.stdout.write("=" * 80)

        # Performance metrics
        self.stdout.write(f"⏱️  Duration: {duration:.2f} seconds")
        if self.stats["oracle_users_fetched"] > 0:
            rate = self.stats["oracle_users_fetched"] / duration
            self.stdout.write(f"📈 Processing rate: {rate:.1f} users/second")

        # User statistics
        self.stdout.write("\n📊 USER STATISTICS:")
        self.stdout.write(
            f"   Oracle users fetched: {self.stats['oracle_users_fetched']}"
        )
        self.stdout.write(f"   Local users found: {self.stats['local_users_found']}")
        self.stdout.write(f"   New users created: {self.stats['new_users']}")
        self.stdout.write(f"   Users updated: {self.stats['updated_users']}")
        self.stdout.write(f"   Users deactivated: {self.stats['disabled_users']}")

        # Membership statistics
        self.stdout.write("\n👥 MEMBERSHIP STATISTICS:")
        self.stdout.write(f"   New memberships: {self.stats['new_memberships']}")
        self.stdout.write(
            f"   Updated memberships: {self.stats['updated_memberships']}"
        )
        self.stdout.write(
            f"   Deactivated memberships: {self.stats['deactivated_memberships']}"
        )

        # Role and Group statistics
        self.stdout.write("\n🏢 ROLE & GROUP STATISTICS:")
        self.stdout.write(f"   New roles created: {self.stats['new_roles']}")
        self.stdout.write(f"   New groups created: {self.stats['new_groups']}")
        self.stdout.write(f"   Fixed password hashes: {self.stats['fixed_passwords']}")

        # Error reporting
        if self.stats["errors"] > 0:
            self.stdout.write(
                "\n" + self.style.ERROR(f"❌ ERRORS: {self.stats['errors']}")
            )
            self.stdout.write("   Check the log file for detailed error information.")

        if self.stats["warnings"] > 0:
            self.stdout.write(
                "\n" + self.style.WARNING(f"⚠️  WARNINGS: {self.stats['warnings']}")
            )
            self.stdout.write("   Check the log file for detailed warning information.")

        if self.stats["errors"] == 0 and self.stats["warnings"] == 0:
            self.stdout.write(
                "\n" + self.style.SUCCESS("✅ No errors or warnings encountered")
            )

        self.stdout.write("\n📁 Log file: logs/oracle_user_sync_modern.log")
        self.stdout.write("=" * 80)
