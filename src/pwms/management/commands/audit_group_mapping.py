"""Report how Oracle organisational units map onto the seeded group tree.

Read-only. The ERP gives every person a cost centre and an organisational unit;
``seed_structure`` creates a curated tree with different labels. This command
fetches each distinct ``(CHILD_ORG_NAME, PARENT_ORG_NAME)`` pair, runs it through
:class:`~pwms.membership.resolver.GroupResolver`, and reports what it resolves
to — with the number of active people behind each pair, so the biggest gaps are
fixed first.

``--csv`` writes the full table in the alias-file format. Fill ``seeded_path``
for the unresolved rows, point ``--alias-file`` at the edited file, and re-run
until the remaining unresolved pairs are acceptable.

``--pairs-csv`` reads the pairs from a previous report instead of Oracle, which
makes the resolver testable and lets an alias table be iterated on offline.

Usage:
    python manage.py audit_group_mapping
    python manage.py audit_group_mapping --csv /tmp/org-map.csv
    python manage.py audit_group_mapping --pairs-csv /tmp/org-map.csv
    python manage.py audit_group_mapping --alias-file pwms/membership/group_aliases.csv
    python manage.py audit_group_mapping --term 7
    python manage.py audit_group_mapping --fail-on-unresolved
"""

import csv
from collections import Counter
from pathlib import Path
from time import perf_counter

from django.core.management.base import CommandError

from pwms.management.commands.sync_base import OracleSyncBase
from pwms.membership.org_hierarchy import restore_acronym_case
from pwms.membership.resolver import (
    ALIAS_CHILD_FIELD,
    ALIAS_PARENT_FIELD,
    ALIAS_PATH_FIELD,
    GroupResolver,
    ResolverError,
)
from pwms.models.groups import ParliamentTerm


class Command(OracleSyncBase):
    help = "Report how Oracle organisational units map onto the seeded groups."

    #: Same population the user sync processes, so the counts line up with what
    #: a real run would touch.
    PAIRS_QUERY = """
        SELECT CHILD_ORG_NAME, PARENT_ORG_NAME, COUNT(*) AS PEOPLE
        FROM APPS.XXPER_PEOPLE_INTERFACE
        WHERE CURRENT_EMPLOYEE_FLAG = 'Y'
        AND ASSIGNMENT_STATUS = 'Active Assignment'
        AND EMAIL IS NOT NULL
        AND IDNO IS NOT NULL
        GROUP BY CHILD_ORG_NAME, PARENT_ORG_NAME
        ORDER BY PEOPLE DESC, PARENT_ORG_NAME, CHILD_ORG_NAME
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = self.setup_logging("audit_group_mapping")

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Log each pair as it is resolved.",
        )
        parser.add_argument(
            "--csv",
            dest="csv_path",
            type=str,
            default=None,
            help=(
                "Write every pair to this CSV, in the alias-file format. Fill "
                "in seeded_path for the unresolved rows and pass the file back "
                "with --alias-file."
            ),
        )
        parser.add_argument(
            "--alias-file",
            dest="alias_file",
            type=str,
            default=None,
            help=(
                "Explicit alias table. Defaults to the packaged "
                "pwms/membership/group_aliases.csv when it exists."
            ),
        )
        parser.add_argument(
            "--pairs-csv",
            dest="pairs_csv",
            type=str,
            default=None,
            help=(
                "Read the Oracle pairs from a previous report instead of the "
                "ERP. Columns: oracle_child_org, oracle_parent_org, people."
            ),
        )
        parser.add_argument(
            "--term",
            dest="term",
            type=int,
            default=None,
            help=(
                "ParliamentTerm.number used when resolving houses (default: the "
                "term in force today)."
            ),
        )
        parser.add_argument(
            "--limit",
            dest="limit",
            type=int,
            default=20,
            help="How many unresolved pairs to print; 0 prints all (default: 20).",
        )
        parser.add_argument(
            "--fail-on-unresolved",
            action="store_true",
            help="Exit non-zero when any Oracle organisation pair is unresolved.",
        )

    def handle(self, *args, **options):
        self.stats["start_time"] = perf_counter()

        if options["verbose"]:
            self.logger.setLevel(10)
            for handler in self.logger.handlers:
                handler.setLevel(10)

        term = self._resolve_term(options["term"])
        resolver = self._build_resolver(options["alias_file"], term)

        if options["pairs_csv"]:
            pairs = self._read_pairs_csv(Path(options["pairs_csv"]))
        else:
            self.validate_environment()
            try:
                self.connect_to_oracle()
                pairs = self._fetch_pairs()
            except CommandError:
                raise
            except Exception as exc:
                raise CommandError(f"Oracle fetch failed: {exc}") from exc
            finally:
                self.cleanup_connections()

        rows = self._resolve_rows(resolver, pairs, verbose=options["verbose"])
        self._print_summary(resolver, rows, limit=options["limit"])

        if options["csv_path"]:
            self._write_csv(Path(options["csv_path"]), rows)

        self.stats["end_time"] = perf_counter()
        self.print_duration()

        unresolved = [row for row in rows if not row["resolution"].resolved]
        if unresolved and options["fail_on_unresolved"]:
            raise CommandError(
                f"{len(unresolved)} Oracle organisation pair(s) are unresolved; "
                "fill seeded_path in the alias file and re-run."
            )

    # ------------------------------------------------------------------ #
    #  Setup                                                             #
    # ------------------------------------------------------------------ #

    def _normalize_org_name(self, raw: str) -> str:
        """Canonical PWMS spelling for a raw Oracle unit / cost-centre name.

        Mirrors the normalisation ``sync_users_oracle`` uses, so an alias written
        from this report matches what the sync will look for.
        """
        return restore_acronym_case(self.strip_group_code_prefix(raw or ""))

    def _build_resolver(self, alias_file, term) -> GroupResolver:
        try:
            return GroupResolver.from_database(
                normalize=self._normalize_org_name,
                alias_file=alias_file,
                term=term,
            )
        except ResolverError as exc:
            raise CommandError(str(exc)) from exc

    def _resolve_term(self, number) -> ParliamentTerm | None:
        if number is None:
            return GroupResolver.active_term()
        try:
            return ParliamentTerm.objects.get(number=number)
        except ParliamentTerm.DoesNotExist as exc:
            raise CommandError(
                f"ParliamentTerm number={number} does not exist."
            ) from exc

    def _fetch_pairs(self) -> list[dict]:
        self.logger.info("Fetching distinct Oracle organisation pairs...")
        self.oracle_cursor.execute(self.PAIRS_QUERY)
        pairs = [
            {
                "child": row[0] or "",
                "parent": row[1] or "",
                "people": int(row[2] or 0),
            }
            for row in self.oracle_cursor.fetchall()
        ]
        self.logger.info(f"Fetched {len(pairs)} distinct organisation pair(s)")
        return pairs

    def _read_pairs_csv(self, path: Path) -> list[dict]:
        """Read pairs from a previous ``--csv`` report (no Oracle needed)."""
        if not path.exists():
            raise CommandError(f"Pairs file not found: {path}")

        pairs = []
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                raw_people = (row.get("people") or "0").strip() or "0"
                try:
                    people = int(raw_people)
                except ValueError as exc:
                    raise CommandError(
                        f"Invalid people count {raw_people!r} in {path}"
                    ) from exc
                pairs.append(
                    {
                        "child": (row.get(ALIAS_CHILD_FIELD) or "").strip(),
                        "parent": (row.get(ALIAS_PARENT_FIELD) or "").strip(),
                        "people": people,
                    }
                )
        self.logger.info(f"Read {len(pairs)} pair(s) from {path}")
        return pairs

    # ------------------------------------------------------------------ #
    #  Resolution and reporting                                          #
    # ------------------------------------------------------------------ #

    def _resolve_rows(self, resolver, pairs, *, verbose: bool) -> list[dict]:
        rows = []
        for pair in pairs:
            resolution = resolver.resolve_org(pair["child"], pair["parent"])
            rows.append(
                {
                    **pair,
                    "resolution": resolution,
                    "normalized_child": resolver.normalize_name(pair["child"]),
                    "normalized_parent": resolver.normalize_name(pair["parent"]),
                }
            )
            if verbose:
                target = resolution.path or "UNRESOLVED"
                self.logger.info(
                    "%s (%s) -> %s [%s]",
                    pair["child"] or "—",
                    pair["parent"] or "—",
                    target,
                    resolution.method,
                )
        return rows

    def _print_summary(self, resolver, rows, *, limit: int) -> None:
        total_pairs = len(rows)
        total_people = sum(row["people"] for row in rows)
        resolved = [row for row in rows if row["resolution"].resolved]
        unresolved = [row for row in rows if not row["resolution"].resolved]
        resolved_people = sum(row["people"] for row in resolved)
        unresolved_people = total_people - resolved_people

        term = resolver.term
        term_label = f"{term.number} ({term.name})" if term is not None else "none"
        self.stdout.write(
            f"Oracle organisation pairs: {total_pairs} "
            f"({total_people} active people); term: {term_label}"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Resolved: {len(resolved)} pair(s), {resolved_people} people"
            )
        )
        if unresolved:
            share = (unresolved_people / total_people * 100) if total_people else 0.0
            self.stdout.write(
                self.style.WARNING(
                    f"Unresolved: {len(unresolved)} pair(s), "
                    f"{unresolved_people} people ({share:.1f}%)"
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("No unresolved pairs."))

        methods = Counter(row["resolution"].method for row in rows)
        for method, count in sorted(
            methods.items(), key=lambda item: (-item[1], item[0])
        ):
            self.stdout.write(f"  {method}: {count}")

        if resolver.alias_conflicts:
            self.stdout.write(
                self.style.WARNING(
                    "Ignored ambiguous aliases (the same name maps to several "
                    "paths): " + ", ".join(resolver.alias_conflicts)
                )
            )

        if unresolved:
            ordered = sorted(
                unresolved,
                key=lambda row: (-row["people"], row["child"], row["parent"]),
            )
            shown = ordered if limit <= 0 else ordered[:limit]
            self.stdout.write("\nLargest unresolved pairs:")
            for row in shown:
                self.stdout.write(
                    f"  {row['people']:>4}  {row['child'] or '—'}  "
                    f"(parent: {row['parent'] or '—'})"
                )
            if len(shown) < len(ordered):
                self.stdout.write(
                    f"  … {len(ordered) - len(shown)} more (--limit 0 for all)"
                )

    def _write_csv(self, path: Path, rows: list[dict]) -> None:
        """Write the table in the alias-file format.

        Resolved rows come out with ``seeded_path`` filled — useful as a
        regression fixture. Unresolved rows have it blank, ready to edit.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            ALIAS_CHILD_FIELD,
            ALIAS_PARENT_FIELD,
            "people",
            ALIAS_PATH_FIELD,
            "method",
            "confidence",
            "normalized_child_org",
            "normalized_parent_org",
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                resolution = row["resolution"]
                writer.writerow(
                    {
                        ALIAS_CHILD_FIELD: row["child"],
                        ALIAS_PARENT_FIELD: row["parent"],
                        "people": row["people"],
                        ALIAS_PATH_FIELD: resolution.path,
                        "method": resolution.method,
                        "confidence": resolution.confidence,
                        "normalized_child_org": row["normalized_child"],
                        "normalized_parent_org": row["normalized_parent"],
                    }
                )
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(rows)} row(s) to {path}"))
