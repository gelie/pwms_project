import asyncio

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from workflows.models import Drive, Site
from workflows.sharepoint import get_all_sites, get_site_drives, get_token


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
    """

    help = "Populate or update the Site and Drive tables with data fetched from SharePoint."

    def handle(self, *args, **options):
        """Entry point used by ``manage.py``."""
        try:
            # Retrieve token (synchronous) and sites (asynchronous) using the existing helpers.
            token_data = get_token()
            sites_response = asyncio.run(get_all_sites(token_data))
            self._process_sites(sites_response, token_data)
        except Exception as exc:  # pragma: no cover – Django will wrap this.
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
                self.stdout.write(self.style.WARNING("Skipping site with missing id"))
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
        existing_qs = Site.objects.filter(site_id__in=list(incoming_by_id.keys()))
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
                to_create.append(Site(site_id=site_id, **defaults))

        created_cnt, updated_cnt = 0, 0

        # ------------------------------------------------------------------
        # 4️⃣ Execute bulk operations inside a transaction for atomicity.
        # ------------------------------------------------------------------
        with transaction.atomic():
            if to_create:
                Site.objects.bulk_create(to_create)
                created_cnt = len(to_create)
                self.stdout.write(
                    self.style.SUCCESS(f"Created {created_cnt} new site(s)")
                )
            if to_update:
                Site.objects.bulk_update(
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
                self.stdout.write(
                    self.style.SUCCESS(f"Updated {updated_cnt} existing site(s)")
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Finished populating sites – {created_cnt} created, {updated_cnt} updated."
            )
        )

        # ------------------------------------------------------------------
        # 5️⃣ Process drives for all sites
        # ------------------------------------------------------------------
        self._process_drives(token_data)

    def _process_drives(self, token_data):
        """Synchronise the ``Drive`` model with the data returned from SharePoint."""
        self.stdout.write(self.style.HTTP_INFO("Processing drives for all sites..."))

        # Get all sites from the database
        sites = Site.objects.all()
        total_drives_created = 0
        total_drives_updated = 0

        for site in sites:
            try:
                # Get drives for this site
                drives_response = asyncio.run(get_site_drives(token_data, site.site_id))
                drives = drives_response.get("value", [])

                if not isinstance(drives, list):
                    self.stdout.write(
                        self.style.WARNING(
                            f"Unexpected drives response format for site {site.name}"
                        )
                    )
                    continue

                # ------------------------------------------------------------------
                # Build a dict of incoming drives keyed by the SharePoint ``id``.
                # ------------------------------------------------------------------
                incoming_drives_by_id = {}
                for drive in drives:
                    drive_id = drive.get("id")
                    if not drive_id:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Skipping drive with missing id for site {site.name}"
                            )
                        )
                        continue

                    incoming_drives_by_id[drive_id] = {
                        "name": drive.get("name") or "",
                    }

                # ------------------------------------------------------------------
                # Load existing ``Drive`` objects for this site.
                # ------------------------------------------------------------------
                existing_drives_qs = Drive.objects.filter(
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
                            Drive(site=site, drive_id=drive_id, **defaults)
                        )

                # ------------------------------------------------------------------
                # Execute bulk operations inside a transaction for atomicity.
                # ------------------------------------------------------------------
                with transaction.atomic():
                    if drives_to_create:
                        Drive.objects.bulk_create(drives_to_create)
                        drives_created_cnt = len(drives_to_create)
                        total_drives_created += drives_created_cnt
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"Created {drives_created_cnt} drive(s) for site {site.name}"
                            )
                        )

                    if drives_to_update:
                        Drive.objects.bulk_update(drives_to_update, ["name"])
                        drives_updated_cnt = len(drives_to_update)
                        total_drives_updated += drives_updated_cnt
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"Updated {drives_updated_cnt} drive(s) for site {site.name}"
                            )
                        )

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"Failed to process drives for site {site.name}: {str(e)}"
                    )
                )
                continue

        self.stdout.write(
            self.style.SUCCESS(
                f"Finished processing drives – {total_drives_created} created, {total_drives_updated} updated."
            )
        )
