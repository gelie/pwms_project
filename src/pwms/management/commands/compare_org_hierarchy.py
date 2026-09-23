"""Diff PWMS's group hierarchy against the tree Oracle's supervisor data implies.

``sync_groups_oracle`` builds the organisational tree from
``(CHILD_ORG_NAME, PARENT_ORG_NAME)`` edges, which pair a cost centre with its
org unit — not an org unit with its parent. The result is a flat forest: every
org unit hangs directly off ``Administration`` and the division/section
relationship is lost.

The real containment lives in the ``EMPLOYEEID`` / ``SUPERVISORID`` chain (see
:mod:`pwms.membership.org_hierarchy`). This read-only command derives the tree
that chain implies and compares it with the tree PWMS stores, so the gap is
visible before anything is re-parented. Apply it with
``sync_groups_oracle --infer-org-parents``.

Usage:
    python manage.py compare_org_hierarchy
    python manage.py compare_org_hierarchy --prefix IRP
    python manage.py compare_org_hierarchy --prefix IRP --all
    python manage.py compare_org_hierarchy --json
    python manage.py compare_org_hierarchy --strict
"""

import json

from django.core.management.base import CommandError

from pwms.management.commands.sync_base import OracleSyncBase
from pwms.membership.org_hierarchy import (
    compare_with_stored,
    infer_unit_parents,
    person_from_row,
    restore_acronym_case,
)
from pwms.models import Group


class Command(OracleSyncBase):
    help = (
        "Diff PWMS's group hierarchy against the tree the Oracle supervisor "
        "chain implies."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = self.setup_logging("org_hierarchy_compare")

    PEOPLE_QUERY = """
        SELECT EMPLOYEEID, SUPERVISORID, CHILD_ORG_NAME, PARENT_ORG_NAME
        FROM APPS.XXPER_PEOPLE_INTERFACE
        WHERE CURRENT_EMPLOYEE_FLAG = 'Y'
        AND ASSIGNMENT_STATUS = 'Active Assignment'
    """

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Log each inferred edge as it is derived.",
        )
        parser.add_argument(
            "--prefix",
            type=str,
            default=None,
            help="Only compare units whose name starts with this text (e.g. IRP).",
        )
        parser.add_argument(
            "--all",
            dest="show_all",
            action="store_true",
            help="List every unit in scope, not just the differences.",
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit the comparison as JSON.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit non-zero when any unit differs or is missing.",
        )

    def handle(self, *args, **options):
        self.validate_environment()

        if options["verbose"]:
            self.logger.setLevel(10)
            for handler in self.logger.handlers:
                handler.setLevel(10)

        try:
            self.connect_to_oracle()
            rows = self.compare(options["prefix"])
        except Exception as e:
            self.logger.exception("Hierarchy comparison failed")
            raise CommandError(f"❌ Hierarchy comparison failed: {e!s}")
        finally:
            self.cleanup_connections()

        if options["as_json"]:
            self.stdout.write(json.dumps(self._as_dict(rows), indent=2, default=str))
        else:
            self._print(rows, show_all=options["show_all"], prefix=options["prefix"])

        problems = [row for row in rows if row.status != "match"]
        if options["strict"] and problems:
            raise CommandError(f"❌ {len(problems)} hierarchy difference(s) found")

    # -- work ---------------------------------------------------------------
    def compare(self, prefix: str | None = None):
        """Inferred rows for the scope, compared with what PWMS stores."""
        people = self.fetch_people()
        inferences = infer_unit_parents(people, self._normalize_org_name)
        return compare_with_stored(inferences, self._stored_parents(), prefix=prefix)

    def fetch_people(self):
        """Employee / supervisor rows used to derive the organisational tree."""
        self.oracle_cursor.execute(self.PEOPLE_QUERY)
        return [person_from_row(row) for row in self.oracle_cursor.fetchall()]

    def _stored_parents(self) -> dict[str, str | None]:
        """Group name -> current parent name, for every group PWMS holds."""
        return dict(Group.objects.values_list("name", "parent__name"))

    def _normalize_org_name(self, raw: str) -> str:
        """Canonical PWMS group name for a raw Oracle unit / cost-centre name."""
        return restore_acronym_case(self.strip_group_code_prefix(raw or ""))

    # -- presentation -------------------------------------------------------
    def _as_dict(self, rows) -> dict:
        summary = {"total": len(rows), "match": 0, "differs": 0, "missing": 0}
        for row in rows:
            summary[row.status] = summary.get(row.status, 0) + 1
        return {
            "rows": [
                {
                    "unit": row.unit,
                    "stored_parent": row.stored_parent,
                    "inferred_parent": row.inferred_parent,
                    "status": row.status,
                    "low_confidence": row.low_confidence,
                }
                for row in rows
            ],
            "summary": summary,
        }

    def _print(self, rows, show_all: bool, prefix: str | None):
        scope = f" (prefix {prefix!r})" if prefix else ""
        self.stdout.write(
            self.style.MIGRATE_HEADING(f"\nOrganisation hierarchy diff{scope}")
        )
        self.stdout.write("=" * 60)

        shown = rows if show_all else [row for row in rows if row.status != "match"]
        if not shown:
            self.stdout.write(
                self.style.SUCCESS("✅ PWMS matches the tree Oracle implies.")
            )
        for row in shown:
            suffix = "   [low confidence]" if row.low_confidence else ""
            self.stdout.write(f"\n{row.unit}")
            if row.status == "missing":
                self.stdout.write("    PWMS   : (no group)")
            else:
                self.stdout.write(f"    PWMS   : {row.stored_parent or '(top level)'}")
            self.stdout.write(
                f"    Oracle : {row.inferred_parent or '(top level)'}{suffix}"
            )

        summary = self._as_dict(rows)["summary"]
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            f"{summary['total']} unit(s) in scope: {summary['match']} match, "
            f"{summary['differs']} differ, {summary['missing']} missing"
        )
        self.stdout.write(
            "Apply with: python manage.py sync_groups_oracle --infer-org-parents"
        )
