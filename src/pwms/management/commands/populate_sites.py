import asyncio
import csv
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import ClassVar

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pwms.models import SharepointDrive, SharepointSite, SharepointSiteMember, User
from pwms.utils.sharepoint import (
    get_all_sites,
    get_site_drives,
    get_token,
    get_user_list,
    iter_user_list_members,
)

# Dedicated logger for this command – writes to logs/populate_sites.log and is
# isolated from the project-wide root logger (see ``logger.propagate``).
logger = logging.getLogger("pwms.populate_sites")


class Command(BaseCommand):
    """Populate the ``Site`` and ``Drive`` tables from SharePoint.

    The command:
    1. Retrieves an Azure AD token.
    2. Calls the Microsoft Graph API to obtain all sites.
    3. Stores each site in the local ``Site`` model, using bulk ``create`` and
       bulk ``update`` to minimise database round‑trips.
    4. For each site, queries for drives and populates the ``Drive`` table.
    5. Tracks when a record was last synced (``last_synced_at``) and the remote
       modification timestamp reported by SharePoint (``remote_modified_at``).

    If a site already exists, it is only updated when the remote
    ``lastModifiedDateTime`` is newer than the value stored in the database.

    All progress is written to a dedicated rotating log file
    (``<LOG_DIR>/populate_sites.log``) in addition to stdout, so a run can be
    audited without trawling the project-wide ``pwms.log``.

    In addition, each site's ``User Information List`` is read to populate the
    ``SharepointSiteMember`` table (this only needs ``Sites.Read.All``). Sites
    whose user list cannot be read are collected into a CSV report
    (``--failures-file``) suitable for sending to the SharePoint admin.
    """

    help = "Populate or update the Site and Drive tables with data fetched from SharePoint."

    #: Name of the dedicated log file created inside ``settings.LOG_DIR``.
    log_filename = "populate_sites.log"

    #: Columns of the hand-off report, in presentation order.
    failure_columns = (
        "Site Name",
        "Site URL",
        "Site ID",
        "HTTP Status",
        "Graph Endpoint",
        "Error",
        "Recommended Action",
    )

    #: Remediation hint per Graph HTTP status (a generic one is used otherwise).
    _failure_actions: ClassVar[dict[int, str]] = {
        401: "App-only token was rejected – confirm CLIENT_ID/CLIENT_SECRET are "
        "correct and that the app registration still has admin consent.",
        403: "Grant the calling app the Sites.Read.All application permission "
        "with tenant-wide admin consent, then re-run the sync.",
        404: "Site not found or not visible to the app – confirm the site still "
        "exists and that the app has been granted access to it.",
        429: "Graph throttled the request – re-run the sync later.",
        503: "Graph was temporarily unavailable – re-run the sync later.",
    }

    def add_arguments(self, parser):
        parser.add_argument(
            "--failures-file",
            default=None,
            help=(
                "Where to write the CSV report of sites whose members could not "
                "be read. Defaults to "
                "<LOG_DIR>/site_member_failures_<timestamp>.csv"
            ),
        )

    def _setup_logging(self) -> logging.Logger:
        """Attach a dedicated rotating file handler for this command.

        The handler is recreated on every run (idempotently) and writes at
        ``DEBUG`` level with the calling function/line captured via
        ``stacklevel``. ``propagate`` is disabled so these records do not also
        land in the shared ``pwms.log`` file.
        """
        logger.setLevel(logging.DEBUG)

        # Avoid stacking duplicate handlers on repeated invocations.
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)

        settings.LOG_DIR.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            settings.LOG_DIR / self.log_filename,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s"
            )
        )
        logger.addHandler(file_handler)
        logger.propagate = False
        return logger

    def _log(self, style, message: str, level: int = logging.INFO):
        """Write ``message`` to the log file and echo it to stdout styled."""
        logger.log(level, message, stacklevel=2)
        self.stdout.write(style(message))

    def _describe_member_failure(self, site, exc) -> dict:
        """Normalise one ``get_user_list`` failure into a report row."""
        status = getattr(getattr(exc, "response", None), "status_code", "") or ""
        if status:
            action = self._failure_actions.get(
                status,
                "Investigate the Graph error above before re-running the sync.",
            )
        else:
            action = (
                "Not an HTTP error – check the message; if it concerns the access "
                "token, re-run once a token can be retrieved."
            )

        return {
            "Site Name": site.name,
            "Site URL": site.url,
            "Site ID": site.site_id,
            "HTTP Status": status,
            "Graph Endpoint": (
                f"https://graph.microsoft.com/v1.0/sites/{site.site_id}"
                "/lists/User Information List/items"
            ),
            "Error": " ".join(str(exc).split()),
            "Recommended Action": action,
        }

    def _write_failures_report(self, failures: list[dict], path: Path) -> Path:
        """Write ``failures`` to ``path`` as a CSV for the SharePoint admin.

        ``utf-8-sig`` is used so Excel detects the encoding from the BOM.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.failure_columns)
            writer.writeheader()
            writer.writerows(failures)
        return path

    def handle(self, *args, **options):
        """Entry point used by ``manage.py``."""
        self._setup_logging()
        self._log(
            self.style.HTTP_INFO,
            f"Starting SharePoint site sync (log file: {settings.LOG_DIR / self.log_filename})",
        )
        try:
            # Retrieve token (synchronous) and sites (asynchronous) using the existing helpers.
            token_data = get_token()
            sites_response = asyncio.run(get_all_sites(token_data))
            self._process_sites(sites_response, token_data)
            # Members need a site + drive sync first (we operate on stored rows).
            self._process_site_members(token_data, options.get("failures_file"))
            self._log(self.style.SUCCESS, "Finished SharePoint site sync")
        except Exception as exc:  # pragma: no cover – Django will wrap this.
            logger.exception("Failed to populate sites")
            raise CommandError(f"Failed to populate sites: {exc}")

    def _process_sites(self, sites_response, token_data):
        """Synchronise the ``Site`` model with the data returned from SharePoint."""
        # Microsoft Graph usually returns the collection under the ``value`` key.
        sites = sites_response.get("value", [])
        if not isinstance(sites, list):
            raise ValueError("Unexpected response format: 'value' is not a list")

        # ------------------------------------------------------------------
        # 1️⃣ Build a dict of incoming data keyed by the SharePoint ``id``.
        # ------------------------------------------------------------------
        incoming_by_id = {}
        now_ts = timezone.now()
        for site in sites:
            site_id = site.get("id")
            if not site_id:
                self._log(
                    self.style.WARNING,
                    "Skipping site with missing id",
                    level=logging.WARNING,
                )
                continue

            # The Graph query no longer filters personal (OneDrive) sites, so
            # exclude them here to preserve the original behaviour.
            if site.get("isPersonalSite"):
                continue

            incoming_by_id[site_id] = {
                "name": site.get("displayName") or site.get("name") or "",
                "url": site.get("webUrl") or "",
                "is_personal_site": site.get("isPersonalSite", False),
                "last_synced_at": now_ts,
                "remote_modified_at": parse_datetime(site.get("lastModifiedDateTime"))
                if site.get("lastModifiedDateTime")
                else None,
            }

        # ------------------------------------------------------------------
        # 2️⃣ Load existing ``Site`` objects that match the incoming IDs.
        # ------------------------------------------------------------------
        existing_qs = SharepointSite.objects.filter(
            site_id__in=list(incoming_by_id.keys())
        )
        existing_by_id = {obj.site_id: obj for obj in existing_qs}

        # ------------------------------------------------------------------
        # 3️⃣ Separate objects into creates and conditional updates.
        # ------------------------------------------------------------------
        to_create = []
        to_update = []

        for site_id, defaults in incoming_by_id.items():
            if site_id in existing_by_id:
                obj = existing_by_id[site_id]

                incoming_remote = defaults["remote_modified_at"]
                existing_remote = obj.remote_modified_at

                # Update only when the remote timestamp is newer, or when
                # either side lacks a timestamp (first sync or missing data).
                if (
                    incoming_remote
                    and existing_remote
                    and incoming_remote <= existing_remote
                ):
                    continue  # No newer information – skip this record.

                obj.name = defaults["name"]
                obj.url = defaults["url"]
                obj.is_personal_site = defaults["is_personal_site"]
                obj.last_synced_at = defaults["last_synced_at"]
                obj.remote_modified_at = defaults["remote_modified_at"]
                to_update.append(obj)
            else:
                to_create.append(SharepointSite(site_id=site_id, **defaults))

        created_cnt, updated_cnt = 0, 0

        # ------------------------------------------------------------------
        # 4️⃣ Execute bulk operations inside a transaction for atomicity.
        # ------------------------------------------------------------------
        with transaction.atomic():
            if to_create:
                SharepointSite.objects.bulk_create(to_create)
                created_cnt = len(to_create)
                self._log(self.style.SUCCESS, f"Created {created_cnt} new site(s)")
            if to_update:
                # Only Graph-owned columns are mirrored. Locally curated fields
                # (``enabled``, which gates the attachment picker) are
                # deliberately absent, so a re-sync never undoes an
                # administrator's choice.
                SharepointSite.objects.bulk_update(
                    to_update,
                    [
                        "name",
                        "url",
                        "is_personal_site",
                        "last_synced_at",
                        "remote_modified_at",
                    ],
                )
                updated_cnt = len(to_update)
                self._log(self.style.SUCCESS, f"Updated {updated_cnt} existing site(s)")

        self._log(
            self.style.SUCCESS,
            f"Finished populating sites – {created_cnt} created, {updated_cnt} updated.",
        )

        # ------------------------------------------------------------------
        # 5️⃣ Process drives for all sites
        # ------------------------------------------------------------------
        self._process_drives(token_data)

    def _process_drives(self, token_data):
        """Synchronise the ``Drive`` model with the data returned from SharePoint."""
        self._log(self.style.HTTP_INFO, "Processing drives for all sites...")

        # Get all sites from the database
        sites = SharepointSite.objects.all()
        total_drives_created = 0
        total_drives_updated = 0

        for site in sites:
            try:
                # Get drives for this site
                drives_response = asyncio.run(get_site_drives(token_data, site.site_id))
                drives = drives_response.get("value", [])

                if not isinstance(drives, list):
                    self._log(
                        self.style.WARNING,
                        f"Unexpected drives response format for site {site.name}",
                        level=logging.WARNING,
                    )
                    continue

                # ------------------------------------------------------------------
                # Build a dict of incoming drives keyed by the SharePoint ``id``.
                # ------------------------------------------------------------------
                incoming_drives_by_id = {}
                for drive in drives:
                    drive_id = drive.get("id")
                    if not drive_id:
                        self._log(
                            self.style.WARNING,
                            f"Skipping drive with missing id for site {site.name}",
                            level=logging.WARNING,
                        )
                        continue

                    incoming_drives_by_id[drive_id] = {
                        "name": drive.get("name") or "",
                    }

                # ------------------------------------------------------------------
                # Load existing ``Drive`` objects for this site.
                # ------------------------------------------------------------------
                existing_drives_qs = SharepointDrive.objects.filter(
                    site=site, drive_id__in=list(incoming_drives_by_id.keys())
                )
                existing_drives_by_id = {
                    obj.drive_id: obj for obj in existing_drives_qs
                }

                # ------------------------------------------------------------------
                # Separate drives into creates and updates.
                # ------------------------------------------------------------------
                drives_to_create = []
                drives_to_update = []

                for drive_id, defaults in incoming_drives_by_id.items():
                    if drive_id in existing_drives_by_id:
                        obj = existing_drives_by_id[drive_id]
                        obj.name = defaults["name"]
                        drives_to_update.append(obj)
                    else:
                        drives_to_create.append(
                            SharepointDrive(site=site, drive_id=drive_id, **defaults)
                        )

                # ------------------------------------------------------------------
                # Execute bulk operations inside a transaction for atomicity.
                # ------------------------------------------------------------------
                with transaction.atomic():
                    if drives_to_create:
                        SharepointDrive.objects.bulk_create(drives_to_create)
                        drives_created_cnt = len(drives_to_create)
                        total_drives_created += drives_created_cnt
                        self._log(
                            self.style.SUCCESS,
                            f"Created {drives_created_cnt} drive(s) for site {site.name}",
                            level=logging.DEBUG,
                        )

                    if drives_to_update:
                        SharepointDrive.objects.bulk_update(drives_to_update, ["name"])
                        drives_updated_cnt = len(drives_to_update)
                        total_drives_updated += drives_updated_cnt
                        self._log(
                            self.style.SUCCESS,
                            f"Updated {drives_updated_cnt} drive(s) for site {site.name}",
                            level=logging.DEBUG,
                        )

            except Exception as e:
                self._log(
                    self.style.ERROR,
                    f"Failed to process drives for site {site.name}: {e!s}",
                    level=logging.ERROR,
                )
                logger.debug("Drive sync failure detail", exc_info=True)
                continue

        self._log(
            self.style.SUCCESS,
            f"Finished processing drives – {total_drives_created} created, {total_drives_updated} updated.",
        )

    def _process_site_members(self, token_data, failures_file: str | None = None):
        """Synchronise ``SharepointSiteMember`` rows for every stored site.

        The remote site "members" are read from each site's ``User Information
        List`` (``GET /sites/{id}/lists/User Information List/items?expand=fields``).
        Entries that describe a real person are matched to local :class:`User`
        records by e-mail / username and stored with a ``(site, user)`` natural
        key. Members who no longer appear are deactivated rather than deleted.

        This endpoint only needs the ``Sites.Read.All`` application permission
        (not ``Sites.Manage.All``). Sites where the call fails are reported and
        skipped (the site/drive sync is unaffected). Those sites are also written
        to ``failures_file`` (default
        ``<LOG_DIR>/site_member_failures_<timestamp>.csv``) so the failures can
        be handed to a SharePoint admin.
        """
        self._log(self.style.HTTP_INFO, "Processing site members...")

        # Local users, keyed by lower-cased email and username, for O(1) matching.
        user_by_email, user_by_username = {}, {}
        for user in User.objects.all().only("id", "email", "username"):
            if user.email:
                user_by_email[user.email.strip().lower()] = user
            if user.username:
                user_by_username[user.username.strip().lower()] = user

        total = {
            "created": 0,
            "deactivated": 0,
            "unmatched": 0,
            "sites_skipped": 0,
        }
        failures: list[dict] = []

        for site in SharepointSite.objects.all().only("id", "site_id", "name"):
            try:
                response = asyncio.run(get_user_list(token_data, site.site_id))
            except Exception as e:
                self._log(
                    self.style.WARNING,
                    f"Site members unavailable for {site.name}: {e!s}",
                    level=logging.WARNING,
                )
                failures.append(self._describe_member_failure(site, e))
                total["sites_skipped"] += 1
                continue

            # Real people from the User Information List, lower-cased for matching.
            matched_users = {}
            unmatched = 0
            for member in iter_user_list_members(response):
                key = member["email"] or member["username"]
                if not key:
                    continue
                user = user_by_email.get(key) or user_by_username.get(key)
                if user is None:
                    unmatched += 1
                    continue
                matched_users[user.id] = user

            with transaction.atomic():
                existing = SharepointSiteMember.objects.filter(site=site)
                existing_by_user = {m.user_id: m for m in existing}
                active_user_ids = {
                    uid for uid, m in existing_by_user.items() if m.is_active
                }

                to_create = [
                    SharepointSiteMember(site=site, user=user, is_active=True)
                    for uid, user in matched_users.items()
                    if uid not in existing_by_user
                ]
                if to_create:
                    SharepointSiteMember.objects.bulk_create(to_create)
                    total["created"] += len(to_create)

                # Deactivate rows for members who no longer appear.
                deactivate = [
                    existing_by_user[uid]
                    for uid in (active_user_ids - set(matched_users.keys()))
                ]
                if deactivate:
                    SharepointSiteMember.objects.filter(
                        pk__in=[m.pk for m in deactivate]
                    ).update(is_active=False)
                    total["deactivated"] += len(deactivate)

            if unmatched:
                total["unmatched"] += unmatched

        self._log(
            self.style.SUCCESS,
            "Finished processing site members – "
            f"{total['created']} created, {total['deactivated']} deactivated, "
            f"{total['unmatched']} remote principals had no local user, "
            f"{total['sites_skipped']} site(s) skipped (user list unavailable).",
        )

        # Hand-off report for the SharePoint admin: one row per site whose
        # ``User Information List`` call failed.
        if failures:
            report_path = (
                Path(failures_file)
                if failures_file
                else settings.LOG_DIR
                / f"site_member_failures_{timezone.now():%Y%m%d_%H%M%S}.csv"
            )
            self._write_failures_report(failures, report_path)
            self._log(
                self.style.WARNING,
                f"{len(failures)} site(s) could not be read – report for the "
                f"SharePoint admin written to {report_path}",
                level=logging.WARNING,
            )
        else:
            self._log(
                self.style.SUCCESS,
                "No site-member user-list failures – no report written.",
            )
