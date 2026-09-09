"""
Base class for Oracle sync commands with shared connection and utility methods.

Provides common functionality for sync_groups_oracle, sync_roles_oracle, and sync_users_oracle.
"""

import logging
import re

try:
    import oracledb
except ImportError:
    import cx_Oracle as oracledb

from decouple import config
from django.core.management.base import BaseCommand, CommandError


class OracleSyncBase(BaseCommand):
    """Base class for Oracle synchronization commands."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.oracle_conn = None
        self.oracle_cursor = None
        self.stats = {
            "start_time": None,
            "end_time": None,
            "errors": 0,
            "warnings": 0,
        }

    def add_common_arguments(self, parser):
        """Add common arguments shared across all sync commands."""
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

    def setup_logging(self, log_name: str) -> logging.Logger:
        """Set up comprehensive logging configuration."""
        logger = logging.getLogger(log_name)
        logger.setLevel(logging.DEBUG)

        for handler in logger.handlers[:]:
            logger.removeHandler(handler)

        import os
        from logging.handlers import RotatingFileHandler

        os.makedirs("logs", exist_ok=True)

        file_handler = RotatingFileHandler(
            f"logs/{log_name}.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
        )
        file_handler.setLevel(logging.DEBUG)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)

        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s"
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        return logger

    def validate_environment(self):
        """Validate required environment variables."""
        required_vars = ["ORA_USER", "ORA_PWD", "ORA_HOST", "ORA_PORT", "ORA_SERVICE"]
        missing_vars = [var for var in required_vars if not config(var, default=None)]

        if missing_vars:
            raise CommandError(
                f"Missing required environment variables: {', '.join(missing_vars)}\n"
                f"Please check your .env file or environment configuration."
            )

    def connect_to_oracle(self):
        """Establish connection to Oracle database using modern oracledb library."""
        try:
            is_modern_oracledb = hasattr(oracledb, "init_oracle_client")

            if is_modern_oracledb:
                self.logger.info("🔧 Using modern oracledb library")
                self.logger.info(
                    "🔧 Initializing Oracle thick mode (required for network encryption)..."
                )
                thick_mode_success = False
                last_error = None

                try:
                    import platform

                    oracle_client_paths = []
                    if platform.system() == "Linux":
                        oracle_client_paths = [
                            "/usr/lib/oracle/*/client64/lib",
                            "/opt/oracle/instantclient*",
                            "/usr/local/lib",
                            "/usr/lib64",
                        ]
                    elif platform.system() == "Darwin":
                        oracle_client_paths = [
                            "/usr/local/lib",
                            "/opt/oracle/instantclient*",
                        ]
                    elif platform.system() == "Windows":
                        oracle_client_paths = [
                            "C:\\oracle\\instantclient*",
                            "C:\\Program Files\\Oracle\\*",
                        ]

                    init_attempts = [
                        lambda: oracledb.init_oracle_client(),
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

                connection_attempts = [
                    lambda: oracledb.connect(
                        user=config("ORA_USER"),
                        password=config("ORA_PWD"),
                        host=config("ORA_HOST"),
                        port=int(config("ORA_PORT", 1521)),
                        service_name=config("ORA_SERVICE"),
                    ),
                    lambda: oracledb.connect(
                        user=config("ORA_USER"),
                        password=config("ORA_PWD"),
                        dsn=f"{config('ORA_HOST')}:{config('ORA_PORT')}/{config('ORA_SERVICE')}",
                    ),
                    lambda: oracledb.connect(
                        f"{config('ORA_USER')}/{config('ORA_PWD')}@{config('ORA_HOST')}:{config('ORA_PORT')}/{config('ORA_SERVICE')}"
                    ),
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
                    raise last_error

            else:
                self.logger.info("🔧 Using cx_Oracle compatibility mode")
                oracle_connection_string = (
                    f"{config('ORA_USER')}/{config('ORA_PWD')}@"
                    f"{config('ORA_HOST')}:{config('ORA_PORT')}/{config('ORA_SERVICE')}"
                )
                self.oracle_conn = oracledb.connect(oracle_connection_string)
                self.logger.info("✅ Connected using cx_Oracle compatibility mode")

            self.oracle_cursor = self.oracle_conn.cursor()

            self.oracle_cursor.execute("SELECT 1 FROM DUAL")
            self.oracle_cursor.fetchone()

            self.logger.info("✅ Successfully connected to Oracle database")
            self.stdout.write(self.style.SUCCESS("Connected to Oracle database"))

        except Exception as e:
            self.logger.error(f"❌ Oracle connection failed: {e}")
            raise CommandError(f"❌ Oracle connection failed: {e}")

    def _try_thick_mode_with_paths(self, oracle_client_paths):
        """Try to initialize thick mode with different Oracle client paths."""
        import glob
        import os

        for path_pattern in oracle_client_paths:
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

        raise Exception("No valid Oracle client library found in standard paths")

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

    def strip_group_code_prefix(self, name: str) -> str:
        """Strip numeric code prefix and extract hierarchical parts after colon.

        For administrative groups (ICT, TAO, etc.), we preserve the parent context
        to avoid name collisions between different departments.
        For parliamentary groups, we use the legacy behavior for compatibility.

        Examples:
            "80002-Members: NA: National Assembly" -> "National Assembly"
            "12345-CBS: PP: PARLIAMENTARY PUBLIC PARTICIPATION SECTION" -> "Parliamentary Public Participation Section"
            "60350-ISS: CATERING SERVICES SECTION" -> "Catering Services Section"
            "ICT: CIO: Man and Gen" -> "ICT: CIO: Man and Gen"  # Preserve hierarchy
            "TAO: Man and Gen" -> "TAO: Man and Gen"  # Preserve hierarchy
            "MEMBERS SECTION" -> "Members Section"
        """
        if not name:
            return ""

        name = name.strip()
        # Strip numeric code prefix
        name = re.sub(r"^\d+-", "", name)
        # Normalize whitespace
        name = " ".join(name.split())

        # Split by colon to analyze hierarchy
        parts = [part.strip() for part in name.split(":")]

        # Check if this is an administrative group that needs hierarchy preservation
        # Administrative groups typically start with department codes like ICT, TAO, HR, etc.
        admin_prefixes = [
            "ICT",
            "TAO",
            "HR",
            "FIN",
            "SEC",
            "COM",
            "LEG",
            "PRO",
            "SMG",
            "CBS",
            "CFO",
            "FMO",
            "HC",
            "IRP",
            "KIS",
            "LSO",
            "MSS",
            "NA",
            "NCOP",
            "PCSD",
            "RM",
            "RMI",
            "SCM",
            "CAE",
            "Catering",
            "Household",
            "ISS",
            "OSTP",
            "Protection Services",
        ]

        if len(parts) > 1 and parts[0].upper() in admin_prefixes:
            # Administrative group - preserve hierarchy to avoid collisions
            normalized_parts = []
            for part in parts:
                normalized_parts.append(self._normalize_group_part(part))
            return ": ".join(normalized_parts)
        else:
            # Parliamentary group or single part - use legacy behavior
            if len(parts) > 1:
                # Extract last part after colon (legacy behavior)
                name = parts[-1]
            else:
                name = parts[0]
            return self._normalize_group_part(name)

    def _normalize_group_part(self, part: str) -> str:
        """Normalize a single group part to title case, preserving acronyms."""
        # Convert to title case, but preserve common acronyms
        acronyms = [
            "NA",
            "NCOP",
            "ICT",
            "HR",
            "IT",
            "CEO",
            "CFO",
            "CIO",
            "MP",
            "MPs",
            "TAO",
        ]
        words = part.split()
        result_words = []

        for word in words:
            # Keep acronyms uppercase if they're all caps and 2-4 letters
            if word.isupper() and 2 <= len(word) <= 4 and word in acronyms:
                result_words.append(word)
            else:
                result_words.append(word.title())

        return " ".join(result_words)

    def normalize_role_name(self, positiondesc: str, employeetype: str) -> str:
        """Normalize role names to reduce proliferation.

        Rules:
        - Members always map to 'Member of Parliament'.
        - If no position provided for staff, default to 'Staff Member'.
        - Otherwise, clean up the position description.
        """
        employeetype = (employeetype or "").strip()
        pos = (positiondesc or "").strip()

        if employeetype.lower() == "member":
            return "Member of Parliament"

        if not pos:
            return "Staff Member"

        if ":" in pos:
            pos = pos.split(":", 1)[0].strip()

        if "(" in pos and ")" in pos:
            try:
                start = pos.index("(")
                end = pos.rindex(")")
                if end > start:
                    pos = f"{pos[:start].strip()} {pos[end + 1 :].strip()}".strip()
            except Exception:
                pass

        pos = " ".join(pos.split())
        normalized = pos.title()

        if normalized in ["Ecm", "ECM"]:
            normalized = "ECM Analyst Programmer"
        elif normalized == "Control":
            normalized = "Control Officer"
        elif normalized == "Undersecretary":
            normalized = "Under Secretary"

        return normalized or "Staff Member"

    def print_duration(self):
        """Print execution duration."""
        if self.stats["start_time"] and self.stats["end_time"]:
            duration = self.stats["end_time"] - self.stats["start_time"]
            self.stdout.write(f"\n⏱️  Duration: {duration:.2f} seconds")
            self.logger.info(f"Total duration: {duration:.2f} seconds")
