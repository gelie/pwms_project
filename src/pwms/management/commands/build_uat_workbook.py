"""Generate the UAT results workbook from the printable acceptance-test form.

``src/pwms/docs/UAT Form.html`` is the **single source of truth** for the IRPD
acceptance scenarios. This command reads it and writes ``UAT Form.xlsx`` beside
it, so the section never maintains the same scenarios in two places: change the
form, re-run the command, and the workbook follows.

The workbook is built for a section, not a single person:

* ``Results`` is a **matrix** — one row per scenario, one column per tester — so
  several testers fill in the same file and the roll-up happens as they type. Each
  row carries *Failed by*, *Blocked by*, *Tested* and a triage *Status*.
* ``Summary`` counts the matrix and the defect log, and states whether the form's
  own acceptance criteria (§15) are met.
* ``Scenarios`` carries the full steps and expected results, and ``Expected
  behaviour`` the deliberate behaviours that must not be logged as defects.

The printable PDF remains the signed record; this workbook is how the results are
gathered and rolled up. Both come from the same HTML.
"""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.page import PageMargins
from openpyxl.worksheet.properties import PageSetupProperties

#: Where the form and the workbook live, relative to the project root.
DOCS_DIR = Path("src") / "pwms" / "docs"
FORM_NAME = "UAT Form.html"
WORKBOOK_NAME = "UAT Form.xlsx"

#: Tester columns the matrix is laid out for. The section is ~10 people; a column
#: can be inserted after the last one, but the roll-up ranges would need widening.
TESTER_COLUMNS = 8

#: Result letters the form defines, and the values the pickers offer.
RESULTS = ("P", "F", "B", "N")
SEVERITIES = ("S1", "S2", "S3", "S4")
DEFECT_STATUSES = ("Open", "Fixed", "Closed", "Deferred")

#: Defect log body rows (fixed so the pickers and Summary formulas can point at them).
DEFECT_FIRST_ROW, DEFECT_LAST_ROW = 4, 43

#: A scenario section heading: "3. A — Access and sign-in".
AREA_HEADING = re.compile(r"^(?P<number>\d+)\.\s+(?P<letter>[A-Z])\s+—\s+(?P<area>.+)$")

# --- Palette, matching the printable form and the wall card ------------------
HEADING_FILL = PatternFill("solid", fgColor="1F4E9C")
SUBHEAD_FILL = PatternFill("solid", fgColor="EEF3FB")
INPUT_FILL = PatternFill("solid", fgColor="FFFFFF")
BAND_FILL = PatternFill("solid", fgColor="F8FAFC")
PASS_FILL = PatternFill("solid", fgColor="E3F3E5")
FAIL_FILL = PatternFill("solid", fgColor="FBE0DD")
BLOCK_FILL = PatternFill("solid", fgColor="FDF3D7")
NA_FILL = PatternFill("solid", fgColor="EFEFEF")

FONT_NAME = "DejaVu Sans"
HEADING_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=11)
SUBHEAD_FONT = Font(name=FONT_NAME, bold=True, color="163A73", size=10)
BODY_FONT = Font(name=FONT_NAME, size=10)
BOLD_FONT = Font(name=FONT_NAME, size=10, bold=True)
MUTED_FONT = Font(name=FONT_NAME, size=9, italic=True, color="555555")
TITLE_FONT = Font(name=FONT_NAME, bold=True, color="1F4E9C", size=14)
VERDICT_FONT = Font(name=FONT_NAME, size=11, bold=True)

THIN = Side(style="thin", color="C8D3E4")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TOP_WRAP = Alignment(vertical="top", wrap_text=True)
TOP_LEFT = Alignment(vertical="top", horizontal="left")
WRAP_CENTRE = Alignment(vertical="center", horizontal="center", wrap_text=True)
BAND_TEXT = Alignment(vertical="center", indent=1)


class Command(BaseCommand):
    help = (
        "Build the UAT results workbook (xlsx) from the printable acceptance-test "
        "form. The form's HTML is the source of truth for the scenarios."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--form",
            type=str,
            default=None,
            help=f"Path to the form HTML (default: <docs>/{FORM_NAME})",
        )
        parser.add_argument(
            "--output",
            type=str,
            default=None,
            help=f"Path of the workbook to write (default: <docs>/{WORKBOOK_NAME})",
        )
        parser.add_argument(
            "--docs-dir",
            type=str,
            default=None,
            help="Directory holding the form, and receiving the workbook.",
        )

    def handle(self, *args, **options):
        base = Path(getattr(settings, "BASE_DIR", Path.cwd()))
        docs_dir = Path(options["docs_dir"] or base / DOCS_DIR)
        form_path = Path(options["form"] or docs_dir / FORM_NAME)
        output_path = Path(options["output"] or docs_dir / WORKBOOK_NAME)

        if not form_path.exists():
            raise CommandError(f"Form not found: {form_path}")

        soup = BeautifulSoup(form_path.read_text(encoding="utf-8"), "html.parser")
        scenarios = _read_scenarios(soup)
        if not scenarios:
            raise CommandError(
                f"No scenario tables found in {form_path}. Is this the UAT form?"
            )

        workbook = _build_workbook(
            scenarios=scenarios,
            areas=_read_areas(scenarios),
            severity=_read_severity(soup),
            result_key=_read_result_key(soup),
            expected=_read_expected_behaviour(soup),
            acceptance=_read_acceptance_criteria(soup),
            source_name=form_path.name,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(output_path)

        critical = sum(1 for row in scenarios if row["critical"])
        self.stdout.write(
            f"Read {len(scenarios)} scenarios ({critical} critical) from {form_path.name}"
        )
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {output_path} ({TESTER_COLUMNS} tester columns)")
        )


# --- Reading the form ------------------------------------------------------


def _clean(node) -> str:
    """Flatten a table cell or heading to a single line of text."""
    return " ".join(node.get_text(" ", strip=True).split())


def _read_scenarios(soup) -> list[dict]:
    """Every scenario row in the form, in the order it prints.

    Each scenario section is an ``h2`` such as "3. A — Access and sign-in"
    followed by its scenario table. A row's first cell carries the ID, and a
    trailing ``*`` marks it critical to acceptance (form §2).
    """
    scenarios = []
    for heading in soup.find_all("h2"):
        match = AREA_HEADING.match(_clean(heading))
        if match is None:
            continue
        table = heading.find_next("table", class_="scenarios")
        if table is None:
            continue
        area = f"{match.group('letter')} — {match.group('area')}"
        for row in table.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            raw_id = _clean(cells[0])
            scenarios.append(
                {
                    "id": raw_id.replace("*", "").strip(),
                    "critical": "*" in raw_id,
                    "area": area,
                    "what": _clean(cells[1]),
                    "expected": _clean(cells[2]),
                }
            )
    return scenarios


def _read_areas(scenarios) -> list[str]:
    """The distinct scenario areas, in the form's order."""
    return list(dict.fromkeys(row["area"] for row in scenarios))


def _heading(soup, prefix: str):
    """The first ``h2``/``h3`` whose text starts with ``prefix``."""
    return soup.find(
        lambda tag: tag.name in {"h2", "h3"} and _clean(tag).startswith(prefix)
    )


def _table_rows_after(soup, prefix: str) -> list[tuple[str, str]]:
    """The (label, detail) rows of the ``.fields`` table under a heading.

    The form uses those tables for the severity scale (§2), the
    expected-behaviour list (§13) and the acceptance criteria details, so matching
    on the heading picks the right one.
    """
    heading = _heading(soup, prefix)
    if heading is None:
        return []
    table = heading.find_next("table", class_="fields")
    if table is None:
        return []
    rows = []
    for row in table.select("tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            rows.append((_clean(cells[0]), _clean(cells[1])))
    return rows


def _read_severity(soup) -> list[tuple[str, str]]:
    """The S1–S4 severity scale from the form's §2."""
    return _table_rows_after(soup, "Severity")


def _read_result_key(soup) -> list[tuple[str, str]]:
    """The P / F / B / N key from the form's §2."""
    key = []
    for item in soup.select("ul.key li"):
        letter, _, meaning = _clean(item).partition(" — ")
        if letter and meaning:
            key.append((letter, meaning))
    return key


def _read_expected_behaviour(soup) -> list[tuple[str, str]]:
    """The "do not log these as defects" list from the form's §13."""
    return _table_rows_after(soup, "13.")


def _read_acceptance_criteria(soup) -> list[str]:
    """The numbered acceptance criteria from the form's §15."""
    heading = _heading(soup, "15.")
    if heading is None:
        return []
    block = heading.find_next("ol")
    if block is None:
        return []
    return [_clean(item) for item in block.find_all("li")]


# --- Building the workbook -------------------------------------------------


def _build_workbook(
    *,
    scenarios: list[dict],
    areas: list[str],
    severity: list[tuple[str, str]],
    result_key: list[tuple[str, str]],
    expected: list[tuple[str, str]],
    acceptance: list[str],
    source_name: str,
) -> Workbook:
    """Sheets in the order they are meant to be read: Instructions first, then
    the scenarios, then the working sheets, and the sign-off last."""
    workbook = Workbook()
    workbook.remove(workbook.active)

    _sheet_instructions(
        workbook, severity=severity, result_key=result_key, source_name=source_name
    )
    _sheet_scenarios(workbook, scenarios)
    results_sheet = _sheet_results(workbook, scenarios)
    _sheet_defects(workbook)
    _sheet_summary(workbook, areas=areas, scenarios=scenarios, acceptance=acceptance)
    _sheet_expected(workbook, expected)
    _sheet_signoff(workbook)

    workbook.active = workbook.index(results_sheet)
    return workbook


def _sheet_instructions(workbook, *, severity, result_key, source_name) -> None:
    sheet = workbook.create_sheet("Instructions")
    sheet.sheet_view.showGridLines = False
    sheet.column_dimensions["A"].width = 4
    sheet.column_dimensions["B"].width = 24
    sheet.column_dimensions["C"].width = 68

    _title(sheet, "User Acceptance Testing — PWMS", last_column=3)
    row = _subtitle(
        sheet,
        2,
        "IRPD section · Delegation Reports · International Resolutions · "
        "International Agreements",
    )

    row += 1
    row = _paragraph(
        sheet,
        row,
        "What this workbook is: the results side of the printable acceptance form. "
        "The form and its PDF remain the record that is signed; this workbook is "
        "where the section's testers record what they found, and where the results "
        "are rolled up.",
    )
    row = _paragraph(
        sheet,
        row,
        f"Generated from {source_name}. The form is the source of truth for the "
        "scenarios, so change the form and rebuild rather than editing the scenario "
        "rows here.",
    )

    row += 1
    row = _section(sheet, row, "How to use it", last_column=3)
    for step in (
        (
            "On the Results sheet, type each tester's name at the top of their own "
            "column (columns H onward). One column per person."
        ),
        (
            "Work down the rows and record your result in your own column — choose "
            "P, F, B or N from the drop-down. Leave a cell blank if you have not "
            "tested that scenario yet."
        ),
        (
            "The Scenarios sheet has the full steps and expected result for each "
            "row: read it there, then record on Results. Expected behaviour lists "
            "the deliberate behaviours that must not be logged as defects."
        ),
        (
            "Where a result is F or B, add a row to the Defect log and give the "
            "scenario's ID in its Scenario column, so the defect and the scenario "
            "tie together."
        ),
        (
            "The Summary sheet counts everything as you type and tells you whether "
            "the acceptance criteria are met. Complete the Sign-off sheet when the "
            "section is ready to decide."
        ),
    ):
        row = _bullet(sheet, row, step)

    row += 1
    row = _section(sheet, row, "Result key", last_column=3)
    for letter, meaning in result_key:
        row = _pair(sheet, row, letter, meaning)

    row += 1
    row = _section(sheet, row, "Severity — grade every defect you log", last_column=3)
    for label, detail in severity:
        row = _pair(sheet, row, label, detail)

    row += 1
    row = _section(sheet, row, "Test details", last_column=3)
    for label in (
        "Environment (URL)",
        "Build / version tested",
        "Test round (1st / re-test / final)",
        "Date(s) of testing",
    ):
        sheet.cell(row=row, column=2, value=label).font = SUBHEAD_FONT
        cell = sheet.cell(row=row, column=3)
        cell.fill = INPUT_FILL
        cell.border = BORDER
        row += 1

    _fit_to_width(sheet)


def _sheet_scenarios(workbook, scenarios) -> None:
    sheet = workbook.create_sheet("Scenarios")
    for column, width in {"A": 8, "B": 26, "C": 9, "D": 68, "E": 68}.items():
        sheet.column_dimensions[column].width = width

    _title(sheet, "Scenarios", last_column=5)
    _subtitle(
        sheet, 2, "The full steps and acceptance criteria behind each row of Results"
    )
    sheet.freeze_panes = "B4"

    headers = (
        "ID",
        "Area",
        "Critical",
        "What to do",
        "Expected result (acceptance criterion)",
    )
    for column, header in enumerate(headers, start=1):
        _header_cell(sheet, 3, column, header)

    row = 4
    for offset, scenario in enumerate(scenarios):
        values = (
            scenario["id"],
            scenario["area"],
            "Yes" if scenario["critical"] else "",
            scenario["what"],
            scenario["expected"],
        )
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=row, column=column, value=value)
            cell.font = BOLD_FONT if column == 1 else BODY_FONT
            cell.alignment = TOP_WRAP if column in (2, 4, 5) else TOP_LEFT
            cell.border = BORDER
            if offset % 2 and column > 1:
                cell.fill = BAND_FILL
        # Approximate the wrapped height, so a printed copy is readable.
        lines = max(len(scenario["what"]) // 62, len(scenario["expected"]) // 62)
        sheet.row_dimensions[row].height = max(26, 13 * (lines + 1))
        row += 1

    sheet.page_setup.orientation = "landscape"
    sheet.print_title_rows = "3:3"
    _fit_to_width(sheet)


def _sheet_results(workbook, scenarios):
    """The tester matrix: the sheet the section actually works in."""
    sheet = workbook.create_sheet("Results")
    sheet.sheet_view.showGridLines = False

    last_column = 7 + TESTER_COLUMNS
    _title(sheet, "Results — one column per tester", last_column=last_column)

    # Test details, above the matrix.
    for label, row in (
        ("Environment (URL)", 3),
        ("Build / version tested", 4),
        ("Test round (1st / re-test / final)", 5),
        ("Date(s) of testing", 6),
    ):
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
        sheet.merge_cells(
            start_row=row, start_column=4, end_row=row, end_column=last_column
        )
        cell = sheet.cell(row=row, column=1, value=label)
        cell.font = SUBHEAD_FONT
        cell.alignment = TOP_LEFT
        cell.border = BORDER
        value = sheet.cell(row=row, column=4)
        value.fill = INPUT_FILL
        value.border = BORDER

    header_row, name_row = 8, 9
    first_row = 10
    last_row = first_row + len(scenarios) - 1
    last_tester = get_column_letter(last_column)

    for column, width in {
        "A": 8,
        "B": 30,
        "C": 9,
        "D": 9,
        "E": 9,
        "F": 8,
        "G": 11,
    }.items():
        sheet.column_dimensions[column].width = width
    for column in range(8, last_column + 1):
        sheet.column_dimensions[get_column_letter(column)].width = 12

    headers = ["ID", "Area", "Critical", "Failed by", "Blocked by", "Tested", "Status"]
    headers += [f"Tester {index}" for index in range(1, TESTER_COLUMNS + 1)]
    for column, header in enumerate(headers, start=1):
        _header_cell(sheet, header_row, column, header)

    cell = sheet.cell(
        row=name_row,
        column=1,
        value="← each tester types their name at the top of their own column",
    )
    cell.font = MUTED_FONT
    sheet.merge_cells(
        start_row=name_row, start_column=1, end_row=name_row, end_column=7
    )
    for column in range(8, last_column + 1):
        name = sheet.cell(row=name_row, column=column)
        name.fill = INPUT_FILL
        name.border = BORDER
        name.alignment = WRAP_CENTRE
        name.font = BOLD_FONT

    tester_range = f"H{first_row}:{last_tester}{last_row}"

    for offset, scenario in enumerate(scenarios):
        row = first_row + offset
        row_range = f"$H{row}:${last_tester}{row}"
        values = [
            scenario["id"],
            scenario["area"],
            "Yes" if scenario["critical"] else "",
            f'=COUNTIF({row_range},"F")',
            f'=COUNTIF({row_range},"B")',
            f'=COUNTIF({row_range},"P")+COUNTIF({row_range},"F")+COUNTIF({row_range},"B")',
            f'=IF(D{row}>0,"FAILED",IF(E{row}>0,"BLOCKED",IF(F{row}>0,"OK","Not tested")))',
        ]
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=row, column=column, value=value)
            cell.font = BOLD_FONT if column == 1 else BODY_FONT
            cell.border = BORDER
            cell.alignment = WRAP_CENTRE if column >= 3 else TOP_LEFT
        for column in range(8, last_column + 1):
            cell = sheet.cell(row=row, column=column)
            cell.border = BORDER
            cell.font = BOLD_FONT
            cell.alignment = WRAP_CENTRE
            if offset % 2:
                cell.fill = BAND_FILL

    results_dv = DataValidation(
        type="list",
        formula1=f'"{",".join(RESULTS)}"',
        allow_blank=True,
        showErrorMessage=True,
        errorTitle="Record P, F, B or N",
        error="Use P (pass), F (fail), B (blocked) or N (not applicable).",
    )
    sheet.add_data_validation(results_dv)
    results_dv.add(tester_range)

    for letter, fill in (
        ("P", PASS_FILL),
        ("F", FAIL_FILL),
        ("B", BLOCK_FILL),
        ("N", NA_FILL),
    ):
        sheet.conditional_formatting.add(
            tester_range,
            CellIsRule(operator="equal", formula=[f'"{letter}"'], fill=fill),
        )
    for text, fill in (
        ("FAILED", FAIL_FILL),
        ("BLOCKED", BLOCK_FILL),
        ("Not tested", NA_FILL),
        ("OK", PASS_FILL),
    ):
        sheet.conditional_formatting.add(
            f"G{first_row}:G{last_row}",
            CellIsRule(operator="equal", formula=[f'"{text}"'], fill=fill),
        )

    sheet.freeze_panes = f"H{first_row}"
    sheet.auto_filter.ref = f"A{header_row}:G{last_row}"
    sheet.page_setup.orientation = "landscape"
    sheet.print_title_rows = f"{header_row}:{name_row}"
    _fit_to_width(sheet)
    return sheet


def _sheet_defects(workbook):
    sheet = workbook.create_sheet("Defect log")
    for column, width in {
        "A": 12,
        "B": 10,
        "C": 9,
        "D": 10,
        "E": 58,
        "F": 16,
        "G": 22,
    }.items():
        sheet.column_dimensions[column].width = width

    _title(sheet, "Defect log", last_column=7)
    _subtitle(
        sheet,
        2,
        "One row per defect. Give the scenario's ID in column B so the two tie together.",
    )

    headers = (
        "Defect ID",
        "Scenario",
        "Area",
        "Severity",
        "What happened, and the steps that produced it",
        "Reported to / date",
        "Status",
    )
    for column, header in enumerate(headers, start=1):
        _header_cell(sheet, 3, column, header)

    for row in range(DEFECT_FIRST_ROW, DEFECT_LAST_ROW + 1):
        for column in range(1, 8):
            cell = sheet.cell(row=row, column=column)
            cell.border = BORDER
            cell.font = BODY_FONT
            cell.alignment = TOP_WRAP if column == 5 else TOP_LEFT
        sheet.row_dimensions[row].height = 24

    for values, target in (
        (SEVERITIES, f"D{DEFECT_FIRST_ROW}:D{DEFECT_LAST_ROW}"),
        (DEFECT_STATUSES, f"G{DEFECT_FIRST_ROW}:G{DEFECT_LAST_ROW}"),
    ):
        picker = DataValidation(
            type="list", formula1=f'"{",".join(values)}"', allow_blank=True
        )
        sheet.add_data_validation(picker)
        picker.add(target)

    sheet.freeze_panes = "A4"
    sheet.page_setup.orientation = "landscape"
    sheet.print_title_rows = "3:3"
    _fit_to_width(sheet)


# The columns the Summary sheet reads from the other two, named once so the
# formulas and the layout cannot drift apart.
RESULTS_FIRST_ROW = 10


def _sheet_summary(workbook, *, areas, scenarios, acceptance) -> None:
    """The roll-up, and an automatic reading of the form's own exit criteria."""
    sheet = workbook.create_sheet("Summary")
    sheet.sheet_view.showGridLines = False
    for column, width in {
        "A": 4,
        "B": 38,
        "C": 12,
        "D": 12,
        "E": 12,
        "F": 12,
        "G": 12,
    }.items():
        sheet.column_dimensions[column].width = width

    first_row = RESULTS_FIRST_ROW
    last_row = first_row + len(scenarios) - 1

    _title(sheet, "Summary — are the acceptance criteria met?", last_column=7)
    row = _subtitle(
        sheet, 2, "Counted live from Results and the Defect log as the section types"
    )

    row += 1
    row = _section(sheet, row, "By tester")
    _table_header(
        sheet, row, ("Tester", "Passed", "Failed", "Blocked", "N/A", "Tested")
    )
    row += 1
    for column in range(8, 8 + TESTER_COLUMNS):
        letter = get_column_letter(column)
        sheet.cell(
            row=row,
            column=2,
            value=f'=IF(Results!{letter}{RESULTS_FIRST_ROW - 1}="","(unnamed)",'
            f"Results!{letter}{RESULTS_FIRST_ROW - 1})",
        ).font = BODY_FONT
        for offset, result in enumerate(RESULTS):
            sheet.cell(
                row=row,
                column=3 + offset,
                value=f'=COUNTIF(Results!{letter}${first_row}:{letter}${last_row},"{result}")',
            ).font = BODY_FONT
        sheet.cell(row=row, column=7, value=f"=SUM(C{row}:F{row})").font = BOLD_FONT
        row += 1

    row += 1
    row = _section(sheet, row, "By scenario")
    _table_header(sheet, row, ("Status", "Count"))
    row += 1
    for status in ("FAILED", "BLOCKED", "OK", "Not tested"):
        sheet.cell(row=row, column=2, value=status).font = BODY_FONT
        sheet.cell(
            row=row,
            column=3,
            value=f'=COUNTIF(Results!G{first_row}:G{last_row},"{status}")',
        ).font = BODY_FONT
        row += 1

    row += 1
    row = _section(sheet, row, "Critical scenarios — all must pass")
    critical = {}
    for key, label, formula in (
        (
            "total",
            "Critical scenarios in total",
            f'=COUNTIF(Results!C{first_row}:C{last_row},"Yes")',
        ),
        (
            "failed",
            "Failed by at least one tester",
            (
                f'=COUNTIFS(Results!C{first_row}:C{last_row},"Yes",'
                f'Results!D{first_row}:D{last_row},">0")'
            ),
        ),
        (
            "blocked",
            "Blocked for at least one tester",
            (
                f'=COUNTIFS(Results!C{first_row}:C{last_row},"Yes",'
                f'Results!E{first_row}:E{last_row},">0")'
            ),
        ),
        (
            "untested",
            "Not tested yet",
            (
                f'=COUNTIFS(Results!C{first_row}:C{last_row},"Yes",'
                f'Results!F{first_row}:F{last_row},"=0")'
            ),
        ),
    ):
        sheet.cell(row=row, column=2, value=label).font = BODY_FONT
        sheet.cell(row=row, column=3, value=formula).font = BOLD_FONT
        critical[key] = row
        row += 1

    row += 1
    row = _section(sheet, row, "Open defects by severity")
    open_defects = {}
    for severity in SEVERITIES:
        sheet.cell(row=row, column=2, value=severity).font = BODY_FONT
        sheet.cell(
            row=row,
            column=3,
            value=(
                f"=COUNTIFS('Defect log'!$D${DEFECT_FIRST_ROW}:$D${DEFECT_LAST_ROW},"
                f'"{severity}",\'Defect log\'!$G${DEFECT_FIRST_ROW}:$G${DEFECT_LAST_ROW},"Open")'
            ),
        ).font = BODY_FONT
        open_defects[severity] = row
        row += 1

    # The verdict reads the same conditions the form's §15 states, by cell
    # reference rather than by arithmetic on the layout.
    row += 1
    sheet.cell(row=row, column=2, value="ACCEPTANCE").font = SUBHEAD_FONT
    conditions = (
        f"C{critical['failed']}>0",
        f"C{critical['blocked']}>0",
        f"C{critical['untested']}>0",
        f"C{open_defects['S1']}>0",
        f"C{open_defects['S2']}>0",
    )
    verdict = sheet.cell(
        row=row,
        column=3,
        value=(
            f"=IF(OR({','.join(conditions)}),"
            f'"NOT YET MET — see the counts above","ACCEPTANCE CRITERIA MET")'
        ),
    )
    verdict.font = VERDICT_FONT
    verdict.alignment = TOP_LEFT
    sheet.merge_cells(start_row=row, start_column=3, end_row=row, end_column=7)
    sheet.row_dimensions[row].height = 22
    row += 1
    note = sheet.cell(
        row=row,
        column=2,
        value=(
            "This checks criteria 1 and 2. Criterion 3 — a workaround for every "
            "other open defect — is a judgement for the section: record it on the "
            "Sign-off sheet."
        ),
    )
    note.font = MUTED_FONT
    note.alignment = TOP_WRAP
    sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)

    row += 2
    row = _section(sheet, row, "The criteria being checked (from the form)")
    for index, text in enumerate(acceptance, start=1):
        sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)
        cell = sheet.cell(row=row, column=2, value=f"{index}. {text}")
        cell.font = BODY_FONT
        cell.alignment = TOP_WRAP
        row += 1

    row += 1
    row = _section(sheet, row, "Scenarios failing for any tester, by area")
    for area in areas:
        sheet.cell(row=row, column=2, value=area).font = BODY_FONT
        sheet.cell(
            row=row,
            column=3,
            value=(
                f'=COUNTIFS(Results!B{first_row}:B{last_row},"{area}",'
                f'Results!G{first_row}:G{last_row},"FAILED")'
            ),
        ).font = BODY_FONT
        row += 1
    sheet.cell(row=row, column=2, value="Defects logged in total").font = BODY_FONT
    sheet.cell(
        row=row,
        column=3,
        value=f"=COUNTA('Defect log'!$A${DEFECT_FIRST_ROW}:$A${DEFECT_LAST_ROW})",
    ).font = BOLD_FONT

    _fit_to_width(sheet)


def _sheet_expected(workbook, expected) -> None:
    sheet = workbook.create_sheet("Expected behaviour")
    for column, width in {"A": 5, "B": 32, "C": 94}.items():
        sheet.column_dimensions[column].width = width

    _title(sheet, "Expected behaviour — do not log these as defects", last_column=3)
    _subtitle(
        sheet,
        2,
        "Deliberate, and confirmed with the section. Note them in your comments only "
        "if they cause a problem for the section's work.",
    )

    for column, header in enumerate(("#", "Behaviour", "What it actually is"), start=1):
        _header_cell(sheet, 4, column, header)

    row = 5
    for offset, (label, detail) in enumerate(expected, start=1):
        number, _, heading = label.partition(". ")
        values = (number or offset, heading or label, detail)
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=row, column=column, value=value)
            cell.font = BOLD_FONT if column in (1, 2) else BODY_FONT
            cell.alignment = TOP_WRAP
            cell.border = BORDER
            if offset % 2:
                cell.fill = BAND_FILL
        sheet.row_dimensions[row].height = max(26, 13 * (len(detail) // 92 + 1))
        row += 1

    sheet.freeze_panes = "A5"
    sheet.page_setup.orientation = "landscape"
    sheet.print_title_rows = "4:4"
    _fit_to_width(sheet)


def _sheet_signoff(workbook) -> None:
    sheet = workbook.create_sheet("Sign-off")
    sheet.sheet_view.showGridLines = False
    for column, width in {"A": 4, "B": 30, "C": 34, "D": 22}.items():
        sheet.column_dimensions[column].width = width

    _title(sheet, "Sign-off", last_column=4)
    row = _subtitle(sheet, 2, "Complete alongside the signed printable form")

    row += 1
    row = _section(sheet, row, "Decision", last_column=4)
    for option in (
        "Accept",
        "Accept with the workarounds recorded in the Defect log",
        "Not accepted — reasons recorded below",
    ):
        sheet.cell(row=row, column=2, value="☐").font = BODY_FONT
        sheet.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
        cell = sheet.cell(row=row, column=3, value=option)
        cell.font = BODY_FONT
        row += 1

    row += 1
    row = _section(
        sheet, row, "Agreed workarounds, and anything carried forward", last_column=4
    )
    row = _blank_rows(sheet, row, rows=4)
    row += 1
    row = _section(sheet, row, "Overall comments", last_column=4)
    row = _blank_rows(sheet, row, rows=6)

    row += 1
    row = _section(sheet, row, "Signatures", last_column=4)
    _table_header(sheet, row, ("", "Name and signature", "Date"))
    row += 1
    for label in (
        "Tested by (tester)",
        "Reviewed by (section leader)",
        "Accepted by (business owner)",
    ):
        sheet.cell(row=row, column=2, value=label).font = SUBHEAD_FONT
        for column in (3, 4):
            sheet.cell(row=row, column=column).border = BORDER
        sheet.row_dimensions[row].height = 34
        row += 1

    row += 1
    row = _section(sheet, row, "Attachments", last_column=4)
    cell = sheet.cell(
        row=row,
        column=2,
        value="List the screenshots and files, each named with its scenario ID",
    )
    cell.font = BODY_FONT
    sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
    row += 1
    row = _blank_rows(sheet, row, rows=3)

    row += 1
    sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
    cell = sheet.cell(
        row=row,
        column=2,
        value=(
            "By signing, the tester confirms the scenarios were carried out as "
            "described and the results recorded are accurate. Return this workbook "
            "with the completed form to the section leader."
        ),
    )
    cell.font = MUTED_FONT
    cell.alignment = TOP_WRAP
    sheet.row_dimensions[row].height = 30

    _fit_to_width(sheet)


# --- Small writing helpers -------------------------------------------------
# Note: openpyxl discards the style of a merged range's non-anchor cells, so only
# anchors are styled. Excel and LibreOffice draw a merged range with the anchor's
# fill and border, which is what makes the heading bands read as one strip.


def _fit_to_width(sheet) -> None:
    sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.page_margins = PageMargins(left=0.4, right=0.4, top=0.5, bottom=0.5)


def _title(sheet, text, *, last_column: int) -> None:
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_column)
    cell = sheet.cell(row=1, column=1, value=text)
    cell.font = TITLE_FONT


def _subtitle(sheet, row: int, text: str) -> int:
    sheet.cell(row=row, column=1, value=text).font = MUTED_FONT
    return row + 1


def _section(sheet, row: int, text: str, *, last_column: int = 7) -> int:
    """A filled heading band across the sheet.

    Only the anchor is styled. openpyxl discards the style of a merge's other
    cells, and Excel and LibreOffice draw a merged range using the anchor's fill
    and border, so the band still reads as one strip.
    """
    sheet.merge_cells(
        start_row=row, start_column=1, end_row=row, end_column=last_column
    )
    cell = sheet.cell(row=row, column=1, value=text)
    cell.fill = HEADING_FILL
    cell.border = BORDER
    cell.font = HEADING_FONT
    cell.alignment = BAND_TEXT
    sheet.row_dimensions[row].height = 18
    return row + 1


def _paragraph(sheet, row: int, text: str) -> int:
    sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
    cell = sheet.cell(row=row, column=2, value=text)
    cell.font = BODY_FONT
    cell.alignment = TOP_WRAP
    sheet.row_dimensions[row].height = max(16, 13 * (len(text) // 108 + 1))
    return row + 2


def _bullet(sheet, row: int, text: str) -> int:
    sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
    cell = sheet.cell(row=row, column=2, value=f"•  {text}")
    cell.font = BODY_FONT
    cell.alignment = TOP_WRAP
    sheet.row_dimensions[row].height = max(16, 13 * (len(text) // 108 + 1))
    return row + 1


def _pair(sheet, row: int, label: str, detail: str) -> int:
    cell = sheet.cell(row=row, column=2, value=label)
    cell.font = SUBHEAD_FONT
    cell.alignment = TOP_LEFT
    detail_cell = sheet.cell(row=row, column=3, value=detail)
    detail_cell.font = BODY_FONT
    detail_cell.alignment = TOP_WRAP
    return row + 1


def _blank_rows(sheet, row: int, *, rows: int) -> int:
    """Bordered writing space, two columns wide."""
    for _ in range(rows):
        for column in (2, 3, 4):
            sheet.cell(row=row, column=column).border = BORDER
        sheet.row_dimensions[row].height = 20
        row += 1
    return row


def _header_cell(sheet, row: int, column: int, text: str) -> None:
    cell = sheet.cell(row=row, column=column, value=text)
    cell.font = SUBHEAD_FONT
    cell.fill = SUBHEAD_FILL
    cell.border = BORDER
    cell.alignment = WRAP_CENTRE


def _table_header(sheet, row: int, labels) -> None:
    for column, label in enumerate(labels, start=2):
        _header_cell(sheet, row, column, label)
