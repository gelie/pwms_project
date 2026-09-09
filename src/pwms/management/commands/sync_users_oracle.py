"""
Django management command to sync users from Oracle database (optimized for daily sync).

This command is optimized for daily user synchronization and assumes that groups and roles
have already been synced using sync_groups_oracle and sync_roles_oracle commands.

Key optimizations:
- Pre-loads all groups and roles into memory at start
- Batch processing with efficient queries
- Org groups/roles are looked up from cache; MP party + house-members groups
  (and the MP/Staff roles) are created on demand when missing
- Optimized for speed (target: <2 minutes for 1600+ users)

Usage:
    python manage.py sync_users_oracle
    python manage.py sync_users_oracle --dry-run
    python manage.py sync_users_oracle --verbose
"""

from time import perf_counter

from django.core.management.base import CommandError
from django.utils import timezone
from pwms.management.commands.sync_base import OracleSyncBase
from pwms.membership.sync_service import MembershipSyncService
from pwms.models import Group, GroupMembership, Role, User


class Command(OracleSyncBase):
    help = "Sync users from Oracle database (optimized for daily sync)"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = self.setup_logging("oracle_user_sync")
        self.stats.update(
            {
                "oracle_users_fetched": 0,
                "local_users_found": 0,
                "new_users": 0,
                "updated_users": 0,
                "disabled_users": 0,
                "new_memberships": 0,
                "updated_memberships": 0,
                "deactivated_memberships": 0,
                "fixed_passwords": 0,
            }
        )
        self._group_cache = {}
        self._role_cache = {}
        self._membership_sync: MembershipSyncService | None = None

    def add_arguments(self, parser):
        self.add_common_arguments(parser)
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

    def handle(self, *args, **options):
        """Main command handler."""
        self.stats["start_time"] = perf_counter()
        self.validate_environment()

        if options["verbose"]:
            self.logger.setLevel(10)
            for handler in self.logger.handlers:
                handler.setLevel(10)

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
            self.sync_users(dry_run, batch_size, force_update, skip_deactivation)

            if self.stats["errors"] == 0:
                self.stdout.write(
                    self.style.SUCCESS("✅ User sync completed successfully")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"⚠️  User sync completed with {self.stats['errors']} errors"
                    )
                )

        except Exception as e:
            self.logger.error(f"User sync failed: {e!s}", exc_info=True)
            self.stats["errors"] += 1
            raise CommandError(f"❌ User sync failed: {e!s}")

        finally:
            self.cleanup_connections()
            self.stats["end_time"] = perf_counter()
            self.print_summary()

    def sync_users(
        self,
        dry_run: bool,
        batch_size: int,
        force_update: bool,
        skip_deactivation: bool,
    ):
        """Main sync logic with performance optimizations."""
        self.logger.info("🔄 Starting user synchronization...")

        oracle_users = self.fetch_oracle_users()
        self.stats["oracle_users_fetched"] = len(oracle_users)

        self.preload_groups_and_roles()

        existing_users = {
            user.idno_hmac: user
            for user in User.objects.filter(idno_hmac__isnull=False).exclude(
                idno_hmac=""
            )
        }
        existing_users_by_username = {u.username.lower(): u for u in User.objects.all()}

        # Count all existing users (by idno_hmac or username)
        total_existing = len(
            set(existing_users.values()) | set(existing_users_by_username.values())
        )
        self.stats["local_users_found"] = total_existing
        self.logger.info(
            f"📊 Found {total_existing} existing users in local database ({len(existing_users)} with idno_hmac, {len(existing_users_by_username)} total)"
        )

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

            self.process_user_batch(
                batch_users,
                existing_users,
                existing_users_by_username,
                force_update,
                dry_run,
            )

        if not skip_deactivation:
            self.deactivate_missing_users(oracle_users, existing_users, dry_run)
        else:
            self.logger.info("⏭️  Skipping user deactivation as requested")

        self.logger.info("✅ User synchronization completed")

    def fetch_oracle_users(self) -> dict[str, tuple]:
        """Fetch users from Oracle database."""
        self.logger.info("📥 Fetching users from Oracle database...")

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
                idno = row[5]
                if idno in oracle_users:
                    duplicate_ids.append(idno)
                    self.logger.warning(f"⚠️  Duplicate IDNO found: {idno}")
                    self.stats["warnings"] += 1
                else:
                    oracle_users[idno] = row

            if duplicate_ids:
                self.logger.warning(
                    f"⚠️  Found {len(duplicate_ids)} duplicate ID numbers"
                )

            self.logger.info(f"✅ Fetched {len(oracle_users)} users from Oracle")
            return oracle_users

        except Exception as e:
            self.logger.error(f"Failed to fetch users from Oracle: {e!s}")
            raise

    def preload_groups_and_roles(self):
        """Pre-load all groups and roles into memory for fast lookup."""
        self.logger.info("📚 Pre-loading groups and roles into memory...")

        # Load all groups
        for group in Group.objects.all():
            sanitized_name = self.strip_group_code_prefix(group.name)
            self._group_cache[sanitized_name] = group
            self._group_cache[group.name] = group

        # Load all roles
        for role in Role.objects.all():
            self._role_cache[role.name] = role

        # Verify required groups exist
        required_groups = [
            "National Assembly",
            "National Council of Provinces",
            "Administration",
        ]
        missing_groups = [g for g in required_groups if g not in self._group_cache]

        if missing_groups:
            raise CommandError(
                f"Missing required groups: {', '.join(missing_groups)}\n"
                f"Please run 'python manage.py sync_groups_oracle' first."
            )

        self.logger.info(
            f"✅ Loaded {len(set(self._group_cache.values()))} groups and {len(self._role_cache)} roles"
        )

        # Initialise the membership sync service with the loaded caches
        self._membership_sync = MembershipSyncService(
            group_cache=self._group_cache,
            role_cache=self._role_cache,
            strip_group_code_prefix=self.strip_group_code_prefix,
            normalize_role_name=self.normalize_role_name,
        )

    def process_user_batch(
        self,
        batch_users: list,
        existing_users: dict,
        existing_users_by_username: dict,
        force_update: bool,
        dry_run: bool = False,
    ):
        """Process a batch of users with enhanced error handling."""
        batch_user_ids = []
        for idno, oracle_user in batch_users:
            id_hmac = User._compute_idno_hmac(idno or "")
            username = oracle_user[10]

            user = None
            if id_hmac and id_hmac in existing_users:
                user = existing_users[id_hmac]
            elif username and username.lower() in existing_users_by_username:
                user = existing_users_by_username[username.lower()]

            if user:
                batch_user_ids.append(user.id)

        existing_memberships = {}
        if batch_user_ids:
            memberships = GroupMembership.objects.filter(
                user_id__in=batch_user_ids
            ).select_related("group")

            for membership in memberships:
                key = (membership.user_id, membership.group_id)
                existing_memberships[key] = membership

        for idno, oracle_user in batch_users:
            try:
                self.process_single_user(
                    idno,
                    oracle_user,
                    existing_users,
                    existing_users_by_username,
                    existing_memberships,
                    force_update,
                    dry_run,
                )
            except Exception as e:
                username = oracle_user[10] if len(oracle_user) > 10 else "unknown"
                self.logger.error(
                    f"Error processing user {username}: {e!s}", exc_info=True
                )
                self.stats["errors"] += 1

    def process_single_user(
        self,
        idno: str,
        oracle_user: tuple,
        existing_users: dict,
        existing_users_by_username: dict,
        existing_memberships: dict,
        force_update: bool,
        dry_run: bool = False,
    ):
        """Process a single user from Oracle data."""
        (
            title,
            lastname,
            firstname,
            middlenames,
            email,
            idno,
            sex,
            positiondesc,
            employeetype,
            partycode,
            username,
            child_org_name,
            parent_org_name,
        ) = oracle_user

        name_data = self.normalize_name_fields(oracle_user)
        is_mp = employeetype == "Member"
        employee_type = "member" if is_mp else "staff"

        user = None
        id_hmac = User._compute_idno_hmac(idno or "")
        if id_hmac and id_hmac in existing_users:
            user = existing_users[id_hmac]
        elif username and username.lower() in existing_users_by_username:
            user = existing_users_by_username[username.lower()]

        if user:
            changes = []
            if user.username.lower() != username.lower():
                changes.append(f"username: {user.username} -> {username}")
            if user.email != email:
                changes.append(f"email: {user.email} -> {email}")
            if user.first_name != name_data["first_name"]:
                changes.append(
                    f"first_name: {user.first_name} -> {name_data['first_name']}"
                )
            if user.employee_type != employee_type:
                changes.append(
                    f"employee_type: {user.employee_type} -> {employee_type}"
                )
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
                    user.employee_type = employee_type
                    user.party_affiliation = partycode or ""
                    user.gender = sex.lower() if sex else ""
                    user.is_mp = is_mp
                    user.is_active = True
                    user.set_idno(idno or "")
                    user.save()

                    if self.fix_bad_password_hash(user):
                        self.stats["fixed_passwords"] += 1

                self.stats["updated_users"] += 1
                if changes:
                    self.logger.info(
                        f"🔄 {'[DRY RUN] ' if dry_run else ''}Updated user {username}: {'; '.join(changes[:3])}"
                    )
            else:
                if not dry_run and self.fix_bad_password_hash(user):
                    self.stats["fixed_passwords"] += 1

            self.handle_group_memberships(
                user, oracle_user, existing_memberships, dry_run
            )

        else:
            if not dry_run:
                user = User.objects.create(
                    username=username.lower(),
                    email=email,
                    first_name=name_data["first_name"],
                    middle_name=name_data["middle_name"],
                    last_name=name_data["last_name"],
                    title=name_data["title"],
                    employee_type=employee_type,
                    party_affiliation=partycode or "",
                    gender=sex.lower() if sex else "",
                    is_mp=is_mp,
                    is_active=True,
                )
                user.set_idno(idno or "")
                user.save(update_fields=["idno_encrypted", "idno_hmac"])
                self.set_default_password(user)

                if id_hmac:
                    existing_users[id_hmac] = user
            else:
                user = User(
                    username=username.lower(),
                    email=email,
                    first_name=name_data["first_name"],
                    middle_name=name_data["middle_name"],
                    last_name=name_data["last_name"],
                    title=name_data["title"],
                    employee_type=employee_type,
                    party_affiliation=partycode or "",
                    gender=sex.lower() if sex else "",
                    is_mp=is_mp,
                    is_active=True,
                )
                user.id = -1

            self.stats["new_users"] += 1
            self.logger.info(
                f"✨ {'[DRY RUN] ' if dry_run else ''}Created new user: {username} ({name_data['first_name']} {name_data['last_name']})"
            )

            self.handle_group_memberships(
                user, oracle_user, existing_memberships, dry_run
            )

    def handle_group_memberships(
        self,
        user: User,
        oracle_user: tuple,
        existing_memberships: dict,
        dry_run: bool = False,
    ):
        """Handle group membership assignments via MembershipSyncService."""
        result = self._membership_sync.sync_user_membership(
            user=user,
            oracle_row=oracle_user,
            existing_memberships=existing_memberships,
            dry_run=dry_run,
        )

        self.stats["new_memberships"] += result.get("created", 0)
        self.stats["updated_memberships"] += result.get("updated", 0)
        # Skipped means a group/role could not be resolved for this user.
        self.stats["warnings"] += result.get("skipped", 0)

    def deactivate_missing_users(
        self,
        oracle_users: dict[str, tuple],
        existing_users: dict[str, User],
        dry_run: bool,
    ):
        """Deactivate local users that are no longer present in Oracle."""
        oracle_hmacs = set(
            filter(
                None,
                [User._compute_idno_hmac(idno) for idno in oracle_users],
            )
        )
        local_hmacs = set(existing_users.keys())
        missing_hmacs = sorted(local_hmacs - oracle_hmacs)

        if not missing_hmacs:
            self.logger.info(
                "✅ No users to deactivate (all existing users are present in Oracle)"
            )
            return

        self.logger.info(
            f"🔄 Deactivating {len(missing_hmacs)} users not found in Oracle..."
        )
        today = timezone.now().date()

        for id_hmac in missing_hmacs:
            user = existing_users.get(id_hmac)
            if not user or not user.is_active:
                continue

            active_qs = GroupMembership.objects.filter(user=user, is_active=True)
            active_count = active_qs.count()

            if not dry_run:
                try:
                    user.is_active = False
                    user.save(update_fields=["is_active"])

                    if active_count:
                        active_qs.update(is_active=False, end_date=today)
                except Exception as e:
                    self.logger.error(
                        f"Error deactivating user {user.username}: {e!s}",
                        exc_info=True,
                    )
                    self.stats["errors"] += 1
                    continue

            self.stats["disabled_users"] += 1
            self.stats["deactivated_memberships"] += active_count
            self.logger.info(
                f"{'[DRY RUN] ' if dry_run else ''}Deactivated user: {user.username}; memberships deactivated: {active_count}"
            )

    def normalize_name_fields(self, user_data: tuple) -> dict[str, str]:
        """Normalize and clean name fields from Oracle data."""
        title, lastname, firstname, middlenames = user_data[0:4]

        first_name = firstname.strip() if firstname else ""
        middle_name = middlenames.strip() if middlenames else ""
        last_name = lastname.strip() if lastname else ""

        if not first_name and middle_name:
            first_name = middle_name
            middle_name = ""
        elif not first_name:
            first_name = "UNKNOWN"
            self.stats["warnings"] += 1

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
        return not user.password.startswith(("pbkdf2_sha256$", "argon2$", "bcrypt$"))

    def fix_bad_password_hash(self, user) -> bool:
        """Fix users with bad password hashes by setting default password."""
        if self.has_bad_password_hash(user):
            self.logger.debug(f"🔧 Fixing bad password hash for user: {user.username}")
            user.set_password("defaultpassword")
            user.save()
            return True
        return False

    def print_summary(self):
        """Print summary of user sync operation."""
        duration = self.stats["end_time"] - self.stats["start_time"]

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(self.style.SUCCESS("👥 ORACLE USER SYNC COMPLETED"))
        self.stdout.write("=" * 80)

        self.stdout.write(f"⏱️  Duration: {duration:.2f} seconds")
        if self.stats["oracle_users_fetched"] > 0:
            rate = self.stats["oracle_users_fetched"] / duration
            self.stdout.write(f"📈 Processing rate: {rate:.1f} users/second")

        self.stdout.write("\n📊 USER STATISTICS:")
        self.stdout.write(
            f"   Oracle users fetched: {self.stats['oracle_users_fetched']}"
        )
        self.stdout.write(f"   Local users found: {self.stats['local_users_found']}")
        self.stdout.write(f"   New users created: {self.stats['new_users']}")
        self.stdout.write(f"   Users updated: {self.stats['updated_users']}")
        self.stdout.write(f"   Users deactivated: {self.stats['disabled_users']}")

        self.stdout.write("\n👥 MEMBERSHIP STATISTICS:")
        self.stdout.write(f"   New memberships: {self.stats['new_memberships']}")
        self.stdout.write(
            f"   Updated memberships: {self.stats['updated_memberships']}"
        )
        self.stdout.write(
            f"   Deactivated memberships: {self.stats['deactivated_memberships']}"
        )

        self.stdout.write("\n🔐 PASSWORD FIXES:")
        self.stdout.write(f"   Fixed password hashes: {self.stats['fixed_passwords']}")

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

        self.stdout.write("\n📁 Log file: logs/oracle_user_sync.log")
        self.stdout.write("=" * 80)
