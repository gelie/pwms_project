"""Clear ``SharepointSiteMember`` rows to start the membership list over.

``SharepointSiteMember`` is **not** synced from SharePoint: ``populate_sites``
only mirrors sites and drives, and site membership — the entitlement behind the
attachment picker — is curated by hand in the admin. This command is the bulk
counterpart of that manual editing: it empties the table (or a slice of it) so
the list can be rebuilt deliberately, and it reports exactly what it removed.

Membership is an access-control decision, so non-superusers lose access to the
affected sites until rows are added back. The command therefore confirms before
writing, offers ``--dry-run`` (the work happens inside a transaction that is
then rolled back, so the reported counts are the real ones) and can be narrowed
to specific sites or to inactive rows only.

Usage:
    python manage.py reset_site_members --dry-run
    python manage.py reset_site_members
    python manage.py reset_site_members --noinput
    python manage.py reset_site_members --site "Committee Library"
    python manage.py reset_site_members --inactive-only --site contoso,committee
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from pwms.models import SharepointSite, SharepointSiteMember


class Command(BaseCommand):
    help = "Delete SharepointSiteMember rows (manually curated site membership)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--site",
            action="append",
            default=[],
            metavar="SELECTOR",
            help=(
                "Only reset the members of this site, matched case-insensitively "
                "on site ID, name or URL. Repeat the option to reset several "
                "sites; the default is every mirrored site."
            ),
        )
        parser.add_argument(
            "--inactive-only",
            action="store_true",
            help=(
                "Only delete rows marked inactive (is_active=False), leaving "
                "current memberships alone. Useful for pruning accidental or "
                "retired rows without interrupting access."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Run the reset inside the transaction and roll it back, so the "
                "reported counts are exact and nothing is written."
            ),
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do not prompt for confirmation.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        inactive_only = options["inactive_only"]

        sites = self._resolve_sites(options["site"])
        rows = SharepointSiteMember.objects.filter(site__in=sites)
        if inactive_only:
            rows = rows.filter(is_active=False)

        summary = self._summarise(rows)
        if not summary:
            self.stdout.write(self.style.SUCCESS("Nothing to reset."))
            return

        if not dry_run and options["interactive"]:
            self._confirm(summary, inactive_only)

        with transaction.atomic():
            deleted, _ = rows.delete()
            if dry_run:
                transaction.set_rollback(True)

        self._report(summary, deleted, dry_run=dry_run, inactive_only=inactive_only)

    def _resolve_sites(self, selectors):
        """Map every ``--site`` selector to a site; default to all of them."""
        if not selectors:
            return SharepointSite.objects.all()

        pks = []
        missing = []
        for selector in selectors:
            needle = selector.strip()
            candidates = list(
                SharepointSite.objects.filter(
                    Q(site_id__iexact=needle)
                    | Q(name__iexact=needle)
                    | Q(url__iexact=needle)
                )
            )
            if not candidates:
                missing.append(selector)
            elif len(candidates) > 1:
                names = ", ".join(sorted(site.name for site in candidates))
                raise CommandError(
                    f"--site {selector!r} is ambiguous (matches {names}); "
                    "use the site ID instead."
                )
            else:
                pks.append(candidates[0].pk)

        if missing:
            quoted = ", ".join(repr(selector) for selector in missing)
            raise CommandError(
                f"No mirrored site matches {quoted}. Run populate_sites first, or "
                "pass a site ID, name or URL that exists in the admin."
            )
        return SharepointSite.objects.filter(pk__in=pks)

    def _summarise(self, rows):
        """Per-site row counts, ordered by site name (empty when nothing matches)."""
        return list(
            rows.order_by("site__name")
            .values("site__name", "site__site_id")
            .annotate(
                total=Count("id"),
                active=Count("id", filter=Q(is_active=True)),
                inactive=Count("id", filter=Q(is_active=False)),
            )
        )

    def _confirm(self, summary, inactive_only):
        total = sum(row["total"] for row in summary)
        self.stdout.write(
            self.style.WARNING(
                f"This permanently deletes {total} SharepointSiteMember row(s) "
                f"across {len(summary)} site(s).\n"
                + (
                    "Inactive rows only: current memberships are untouched."
                    if inactive_only
                    else (
                        "Membership governs the attachment picker, so non-superusers "
                        "lose access to these sites until rows are added back in the "
                        "admin."
                    )
                )
            )
        )
        confirm = input("Type 'yes' to continue, or 'no' to cancel: ")
        if confirm != "yes":
            raise CommandError("reset_site_members cancelled.")

    def _report(self, summary, deleted, *, dry_run, inactive_only):
        heading = "DRY RUN – nothing written." if dry_run else "Site members reset."
        self.stdout.write(
            self.style.WARNING(heading) if dry_run else self.style.SUCCESS(heading)
        )
        scope = "inactive row(s)" if inactive_only else "row(s)"
        self.stdout.write(
            f"  SharepointSiteMember: {deleted} {scope} deleted across "
            f"{len(summary)} site(s)"
        )
        for row in summary:
            detail = f"{row['active']} active, {row['inactive']} inactive"
            self.stdout.write(
                f"    {row['site__name']} ({row['site__site_id']}): "
                f"{row['total']} deleted ({detail})"
            )
        if not dry_run and not inactive_only:
            self.stdout.write(
                "  Membership is curated manually — re-add rows in the admin "
                "(SharePoint sites → members)."
            )
