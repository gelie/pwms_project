"""Import meetings from, and export them to, a CSV or Excel (xlsx) file.

One column specification (:data:`MEETING_COLUMNS`) drives both directions, so a
file exported from the calendar can be edited and imported back unchanged. The
full format is documented in ``docs/Meetings Import & Export.md``; the shape is:

* **export** writes one row per meeting in the documented columns;
* **import** reads the same columns and **upserts on** ``Reference`` — a row
  whose reference already exists updates that meeting, otherwise a new meeting
  is created.

Rules that keep an import safe:

* a **blank cell means "not supplied"**: on create the model default applies and
  on update the existing value is left untouched, so a partial file never wipes a
  field it did not mention;
* a row that cannot be parsed (bad date, unknown meeting type / venue / group /
  user) is **skipped and reported**, never half-applied — the rest of the file
  still imports;
* importing **never deletes** a meeting, and it does not email anyone.

Everything here is plain Python, so the same functions serve the web views, a
management command and the tests. The heavier ``openpyxl`` import is deferred
into the xlsx builder so a CSV download never pays for it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, time

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from ..models import Group, Meeting, User, Venue, WorkflowType
from .permissions import EDIT, resolve

#: The workflow type every imported meeting follows.
WORKFLOW_TYPE_NAME = "Meeting"

#: Media types per export format.
CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

#: Formats offered by :func:`export_content` / :func:`template_content`.
EXPORT_FORMATS = tuple(CONTENT_TYPES)

#: Recognised truthy strings for a boolean cell (case-insensitive).
TRUTHY = {"1", "true", "t", "yes", "y"}

#: Day-first datetime shapes accepted on import, tried after the ISO ones.
DAY_FIRST_FORMATS = ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y")


@dataclass(frozen=True)
class Column:
    """One import/export column: its header, model field and documentation."""

    header: str
    key: str
    required: bool = False
    help: str = ""


#: The columns, in order. Drives the header row of every export and template and
#: the parsing of every import. ``key`` is normally the model field; the few
#: synthetic ones (``venue``, ``host_group``, the user columns, ``current_state``)
#: are resolved to their objects by :class:`_ImportContext`.
MEETING_COLUMNS = (
    Column(
        "Reference",
        "reference_number",
        help="Free-text reference. When a meeting with this reference exists it "
        "is updated, otherwise a new meeting is created.",
    ),
    Column("Title", "title", required=True, help="Meeting title (required)."),
    Column(
        "Meeting type",
        "meeting_type",
        required=True,
        help="A type slug or label, e.g. committee or Committee Meeting.",
    ),
    Column(
        "Start",
        "start_datetime",
        required=True,
        help="Start date/time, e.g. 2026-11-12 09:30.",
    ),
    Column("End", "end_datetime", help="End date/time; blank for an open-ended one."),
    Column("All day", "all_day", help="yes/no; blank leaves it unchanged."),
    Column(
        "Attendance mode",
        "attendance_mode",
        help="on_premise / hybrid / virtual, or a label.",
    ),
    Column(
        "Venue",
        "venue",
        help="An on-premises room, written as Building — Room (the exported form).",
    ),
    Column("Off-site venue", "off_site_venue", help="Name of an external venue."),
    Column(
        "Off-site address", "off_site_address", help="Address of an external venue."
    ),
    Column("Meeting link", "meeting_link", help="Join link for hybrid/virtual."),
    Column("Host group", "host_group", help="The group's name."),
    Column("Chaired by", "chaired_by", help="A user's username or email."),
    Column("Secretary", "secretary", help="A user's username or email."),
    Column("Quorum", "quorum", help="Whole number; blank for none."),
    Column("Priority", "priority", help="low / medium / high / urgent, or a label."),
    Column("Assigned to", "assigned_to", help="A user's username or email."),
    Column("Owner", "owner", help="A user's username or email; defaults to you."),
    Column(
        "State", "current_state", help="A state name; defaults to the initial state."
    ),
    Column("Description", "description", help="Free text summary."),
    Column("Agenda", "agenda_summary", help="The meeting agenda (plain text is fine)."),
    Column("Notes", "notes", help="Free text notes."),
)

HEADERS = tuple(column.header for column in MEETING_COLUMNS)
REQUIRED_COLUMNS = tuple(column.header for column in MEETING_COLUMNS if column.required)
_KEY_BY_HEADER = {column.header.casefold(): column.key for column in MEETING_COLUMNS}

#: Keys that hold a foreign key resolved from the file by label rather than pk.
_USER_KEYS = ("owner", "assigned_to", "chaired_by", "secretary")


class ImportFormatError(Exception):
    """The file as a whole is unusable (wrong format, missing required columns)."""


class RowError(Exception):
    """One row cannot be imported; reported and skipped, the file still imports."""


@dataclass
class ImportReport:
    """The outcome of an import: what changed, and everything that did not."""

    total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# -- export -----------------------------------------------------------------
def export_content(meetings, export_format="xlsx"):
    """``(filename, content, media type)`` for ``meetings`` — bytes, always."""
    export_format = (export_format or "xlsx").lower()
    if export_format == "csv":
        return (
            _filename("csv"),
            _csv_bytes(meetings),
            CONTENT_TYPES["csv"],
        )
    if export_format == "xlsx":
        return (
            _filename("xlsx"),
            _xlsx_bytes(meetings),
            CONTENT_TYPES["xlsx"],
        )
    raise ValueError(f"Unknown export format: {export_format!r}")


def export_response(meetings, export_format="xlsx"):
    """An attachment response for ``export_format``."""
    export_format = (export_format or "xlsx").lower()
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f"Unknown export format: {export_format!r}")
    filename, content, media_type = export_content(meetings, export_format)
    return download_response(filename, content, media_type)


def template_content(export_format="csv"):
    """A blank import template: the header row (plus a xlsx \"Read me\" sheet)."""
    export_format = (export_format or "csv").lower()
    if export_format == "csv":
        buffer = io.StringIO()
        csv.writer(buffer).writerow(HEADERS)
        return (
            "meetings-import-template.csv",
            buffer.getvalue().encode("utf-8-sig"),
            CONTENT_TYPES["csv"],
        )
    if export_format == "xlsx":
        return (
            "meetings-import-template.xlsx",
            _template_xlsx_bytes(),
            CONTENT_TYPES["xlsx"],
        )
    raise ValueError(f"Unknown export format: {export_format!r}")


def template_response(export_format="csv"):
    """An attachment response carrying the blank import template."""
    export_format = (export_format or "csv").lower()
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f"Unknown export format: {export_format!r}")
    filename, content, media_type = template_content(export_format)
    return download_response(filename, content, media_type)


def download_response(filename, content, media_type):
    """An ``HttpResponse`` that downloads ``content`` as ``filename``."""
    response = HttpResponse(content, content_type=media_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _filename(extension):
    return f"meetings-{timezone.localdate().isoformat()}.{extension}"


def _value_for(column, meeting):
    """One meeting's cell for ``column`` (a ``datetime`` only for the date ones)."""
    key = column.key
    if key in ("start_datetime", "end_datetime"):
        value = getattr(meeting, key)
        return timezone.localtime(value).replace(tzinfo=None) if value else ""
    if key == "all_day":
        return "yes" if meeting.all_day else "no"
    if key in ("meeting_type", "attendance_mode", "priority"):
        return _choice_label(key, getattr(meeting, key))
    if key == "current_state":
        return meeting.current_state.name if meeting.current_state_id else ""
    if key == "venue":
        return meeting.venue_display
    if key == "host_group":
        return meeting.host_group.name if meeting.host_group_id else ""
    if key in _USER_KEYS:
        user = getattr(meeting, key, None)
        return user.username if user else ""
    if key == "quorum":
        return meeting.quorum if meeting.quorum is not None else ""
    value = getattr(meeting, key, "")
    return "" if value is None else value


def _choice_label(field_name, value):
    """The human label for a choice field's stored value (falling back to it)."""
    field = Meeting._meta.get_field(field_name)
    for slug, label in field.choices:
        if slug == value:
            return str(label)
    return value


def _csv_bytes(meetings):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADERS)
    for meeting in meetings:
        writer.writerow(
            [_cell(_value_for(column, meeting)) for column in MEETING_COLUMNS]
        )
    # A UTF-8 BOM so Excel opens accented names correctly; the reader strips it.
    return buffer.getvalue().encode("utf-8-sig")


def _cell(value):
    """Make a value safe for a CSV cell (datetimes formatted, ``None`` blank)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return value


def _xlsx_bytes(meetings):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Meetings"
    _write_header(sheet, HEADERS)
    for meeting in meetings:
        sheet.append(
            [_xlsx_cell(_value_for(column, meeting)) for column in MEETING_COLUMNS]
        )
    _autosize(sheet, HEADERS)
    return _save(workbook)


def _template_xlsx_bytes():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Meetings"
    _write_header(sheet, HEADERS)
    _autosize(sheet, HEADERS)

    readme = workbook.create_sheet("Read me")
    _write_header(readme, ("Column", "Required", "Notes"))
    for column in MEETING_COLUMNS:
        readme.append([column.header, "yes" if column.required else "no", column.help])
    _autosize(readme, ("Column", "Required", "Notes"))
    return _save(workbook)


def _write_header(sheet, headers):
    from openpyxl.styles import Alignment, Font, PatternFill

    sheet.append(list(headers))
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(
            start_color="275937", end_color="275937", fill_type="solid"
        )
        cell.alignment = Alignment(horizontal="center")


def _autosize(sheet, headers):
    from openpyxl.utils import get_column_letter

    for index, header in enumerate(headers, 1):
        letter = get_column_letter(index)
        longest = max(
            (
                len(str(sheet.cell(row=row, column=index).value or ""))
                for row in range(1, sheet.max_row + 1)
            ),
            default=len(str(header)),
        )
        sheet.column_dimensions[letter].width = min(max(longest + 4, 12), 60)


def _xlsx_cell(value):
    return "" if value is None else value


def _save(workbook):
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer.getvalue()


# -- import -----------------------------------------------------------------
def read_rows(file_obj, filename=""):
    """Read a CSV/XLSX import file into ``(lineno, {key: str})`` pairs.

    Raises :class:`ImportFormatError` for an unknown format or a missing required
    column. Unknown extra columns are ignored.
    """
    name = (filename or getattr(file_obj, "name", "") or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        fieldnames, rows = _read_xlsx(file_obj)
    elif name.endswith((".csv", ".txt")):
        fieldnames, rows = _read_csv(file_obj)
    else:
        raise ImportFormatError("Unsupported file type — upload a .csv or .xlsx file.")

    normalised = []
    for header in fieldnames:
        key = _KEY_BY_HEADER.get((header or "").strip().casefold())
        normalised.append(key)

    present = {key for key in normalised if key}
    missing = [
        column.header
        for column in MEETING_COLUMNS
        if column.required and column.key not in present
    ]
    if missing:
        raise ImportFormatError(
            "The file is missing required column(s): "
            + ", ".join(missing)
            + ". Expected header: "
            + ", ".join(HEADERS)
        )

    pairs = []
    for offset, raw in enumerate(rows, start=2):
        row = {
            key: (raw[index] if index < len(raw) else "")
            for index, key in enumerate(normalised)
            if key
        }
        pairs.append((offset, row))
    return pairs


def import_meetings(file_obj, *, actor, filename="", dry_run=False):
    """Import (upsert) meetings from ``file_obj`` as ``actor``.

    Returns an :class:`ImportReport`. The caller is responsible for checking that
    ``actor`` may create meetings (the web view does); a row that would *update*
    an existing meeting the actor cannot edit is skipped and reported.
    """
    rows = read_rows(file_obj, filename)
    workflow_type = WorkflowType.objects.filter(name=WORKFLOW_TYPE_NAME).first()
    if workflow_type is None:
        raise ImportFormatError(
            f"The '{WORKFLOW_TYPE_NAME}' workflow type is not configured."
        )
    initial_state = workflow_type.get_initial_state()
    if initial_state is None:
        raise ImportFormatError("The Meeting workflow has no initial state.")

    context = _ImportContext(workflow_type, initial_state)
    report = ImportReport(total=len(rows), dry_run=dry_run)

    for lineno, row in rows:
        try:
            fields = _parse_row(row, context)
        except RowError as error:
            report.errors.append(f"line {lineno}: {error}")
            report.skipped += 1
            continue
        if _apply_row(fields, lineno, actor, context, dry_run, report) is None:
            report.skipped += 1
    return report


def _apply_row(fields, lineno, actor, context, dry_run, report):
    """Create or update one meeting; records the outcome on ``report``."""
    reference = fields.pop("reference_number", "")
    existing = None
    if reference:
        existing = Meeting.objects.filter(reference_number__iexact=reference).first()

    if existing is None:
        fields.setdefault("owner", actor)
        fields.setdefault("current_state", context.initial_state)
        if reference:
            fields["reference_number"] = reference
        if dry_run:
            report.created += 1
            return True
        meeting = Meeting(workflow_type=context.workflow_type, **fields)
        with transaction.atomic():
            meeting.save()
            if meeting.host_group_id is not None:
                meeting.regenerate_attendance(actor=actor)
        report.created += 1
        return True

    if not resolve(actor, existing, EDIT):
        report.errors.append(
            f"line {lineno}: no permission to update '{existing.title}'"
        )
        return None

    changed = {
        name: value
        for name, value in fields.items()
        if getattr(existing, name) != value
    }
    if not changed:
        return None
    if dry_run:
        report.updated += 1
        return True
    with transaction.atomic():
        for name, value in changed.items():
            setattr(existing, name, value)
        existing.save(update_fields=[*changed, "updated_at"])
        if "host_group" in changed:
            existing.regenerate_attendance(actor=actor)
    report.updated += 1
    return True


def _parse_row(row, context):
    """Turn one raw row into model-field values, raising :class:`RowError`."""
    fields = {}
    for column in MEETING_COLUMNS:
        raw = (row.get(column.key) or "").strip()
        if not raw:
            if column.required:
                raise RowError(f"'{column.header}' is required")
            continue
        try:
            fields[column.key] = _parse_value(column.key, raw, context)
        except ValueError as error:
            raise RowError(f"'{column.header}': {error}") from None

    start = fields.get("start_datetime")
    end = fields.get("end_datetime")
    if start and end and end < start:
        raise RowError("'End' is before 'Start'")
    return fields


def _parse_value(key, raw, context):
    if key in ("start_datetime", "end_datetime"):
        return _parse_datetime(raw)
    if key == "all_day":
        return raw.casefold() in TRUTHY
    if key == "quorum":
        try:
            quorum = int(raw)
        except ValueError:
            raise ValueError(f"'{raw}' is not a whole number") from None
        if quorum < 0:
            raise ValueError("quorum cannot be negative")
        return quorum
    if key == "meeting_type":
        return context.choice("meeting_type", raw)
    if key == "attendance_mode":
        return context.choice("attendance_mode", raw)
    if key == "priority":
        return context.choice("priority", raw)
    if key == "venue":
        return context.venue(raw)
    if key == "host_group":
        return context.group(raw)
    if key == "current_state":
        return context.state(raw)
    if key in _USER_KEYS:
        return context.user(raw)
    return raw


def _parse_datetime(raw):
    """Parse a cell into an aware datetime (local time), or raise ``ValueError``."""
    parsed = parse_datetime(raw)
    if parsed is not None:
        return _aware(parsed)
    day = parse_date(raw)
    if day is not None:
        return _aware(datetime.combine(day, time.min))
    for fmt in DAY_FIRST_FORMATS:
        try:
            return _aware(datetime.strptime(raw, fmt))  # noqa: DTZ007 - localised next
        except ValueError:
            continue
    raise ValueError(f"unrecognised date/time '{raw}'")


def _aware(value):
    """Attach the current timezone to a naive wall-clock value."""
    return timezone.make_aware(value) if timezone.is_naive(value) else value


def _normalise_label(text):
    """Fold a venue label for comparison: any dash, collapsed space, lowercase."""
    spaced = (text or "").replace("–", "-").replace("—", "-")
    return " ".join(spaced.split()).casefold()


class _ImportContext:
    """Resolves the file's labels to model objects, caching each lookup."""

    def __init__(self, workflow_type, initial_state):
        self.workflow_type = workflow_type
        self.initial_state = initial_state
        self._choices = {}
        self._venues = None
        self._groups = None
        self._users = None

    def choice(self, field_name, raw):
        if field_name not in self._choices:
            mapping = {}
            for slug, label in Meeting._meta.get_field(field_name).choices:
                mapping[str(slug).casefold()] = slug
                mapping[str(label).casefold()] = slug
            self._choices[field_name] = mapping
        slug = self._choices[field_name].get(raw.casefold())
        if slug is None:
            valid = ", ".join(
                str(slug)
                for slug, _label in Meeting._meta.get_field(field_name).choices
            )
            raise ValueError(f"unknown value '{raw}' (expected one of: {valid})")
        return slug

    def venue(self, raw):
        if self._venues is None:
            self._venues = list(Venue.objects.select_related("building"))
        wanted = _normalise_label(raw)
        matches = [v for v in self._venues if _normalise_label(str(v)) == wanted]
        if not matches:
            matches = [v for v in self._venues if _normalise_label(v.name) == wanted]
        if not matches:
            raise ValueError(
                f"no on-premises room '{raw}' (write it as Building — Room, "
                "or leave it blank and use the off-site columns)"
            )
        if len(matches) > 1:
            raise ValueError(f"room '{raw}' is ambiguous — write it as Building — Room")
        return matches[0]

    def group(self, raw):
        if self._groups is None:
            self._groups = {}
            for group in Group.objects.all():
                self._groups.setdefault(group.name.casefold(), group)
        group = self._groups.get(raw.casefold())
        if group is None:
            raise ValueError(f"no group named '{raw}'")
        return group

    def user(self, raw):
        if self._users is None:
            self._users = {}
            for user in User.objects.all():
                self._users.setdefault(user.username.casefold(), user)
                if user.email:
                    self._users.setdefault(user.email.casefold(), user)
        user = self._users.get(raw.casefold())
        if user is None:
            raise ValueError(f"no user with username or email '{raw}'")
        return user

    def state(self, raw):
        state = self.workflow_type.states.filter(name__iexact=raw).first()
        if state is None:
            raise ValueError(f"no '{raw}' state on the Meeting workflow")
        return state


def _read_csv(file_obj):
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    data = file_obj.read()
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(data))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportFormatError("The file is empty.") from None
    return [str(name).strip() for name in header], [
        [("" if cell is None else str(cell)) for cell in row] for row in reader
    ]


def _read_xlsx(file_obj):
    from openpyxl import load_workbook

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    workbook = load_workbook(file_obj, read_only=True, data_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows)
    except StopIteration:
        raise ImportFormatError("The workbook is empty.") from None
    fieldnames = ["" if name is None else str(name).strip() for name in header_row]
    data = []
    for row in rows:
        if row is None or all(cell is None for cell in row):
            continue
        data.append([_xlsx_to_text(cell) for cell in row])
    return fieldnames, data


def _xlsx_to_text(cell):
    if cell is None:
        return ""
    if isinstance(cell, datetime):
        return cell.strftime("%Y-%m-%d %H:%M")
    return str(cell)
