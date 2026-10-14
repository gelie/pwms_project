# Meetings — import & export

The Meetings calendar can **export** the meetings you may see to a CSV or Excel
(`.xlsx`) file, and **import** meetings back from a file in the same shape. One
column specification drives both directions, so a file exported from the calendar
can be edited (in Excel, LibreOffice, a script) and imported straight back.

| Where | What |
| --- | --- |
| `/pwms/meetings/` → **Export** | Downloads the meetings matching the current filters (type / *mine* / search), as `.xlsx` or `.csv`. |
| `/pwms/meetings/export/` | The same download by URL: `?format=xlsx\|csv`, plus the optional filters `type`, `mine`, `q` and a date window `start` / `end` (`YYYY-MM-DD`). |
| `/pwms/meetings/import/` | The upload page (a create/update upsert). |
| `/pwms/meetings/import/template/` | A blank template: `?format=csv\|xlsx`. The `.xlsx` also carries a *Read me* sheet describing every column. |

The behaviour lives in `pwms/services/meetings_io.py` (functions
`export_content`, `import_meetings`, `template_content`), so it is usable from the
web views, a management command or a script.

## From the command line

The same import runs as a management command — handy for a scripted or
scheduled load, or when the file is large:

```bash
python manage.py import_meetings meetings.csv
python manage.py import_meetings meetings.xlsx --dry-run
python manage.py import_meetings meetings.csv --actor jsmith
```

`--actor` names the user new meetings are owned by and attributed to (default:
the first superuser); `--dry-run` reports what would change without writing. The
command ends with a created / updated / skipped / errors summary.

## File format

* **CSV** — UTF-8 (a byte-order mark is accepted and stripped). The **first row
  is the header**; one meeting per row.
* **XLSX** — the **first worksheet** is read; its first row is the header. The
  template's *Read me* sheet is documentation only.
* Unknown extra columns are **ignored** — you may add your own working columns
  without breaking the import.

## Columns

The header names are matched **case-insensitively** and must be exactly these
(with a missing *required* column the whole file is rejected):

| Column | Required | Meaning and accepted values |
| --- | --- | --- |
| `Reference` | No | Free text. The **upsert key**: when a meeting with this reference (case-insensitive) already exists it is updated, otherwise a new meeting is created. Leave blank to always create. |
| `Title` | **Yes** | The meeting title. |
| `Meeting type` | **Yes** | A type **slug or label**, e.g. `committee` or `Committee Meeting`. See *Meeting types* below. |
| `Start` | **Yes** | Start date/time (see *Dates and times*). |
| `End` | No | End date/time. Must not be before `Start`. |
| `All day` | No | `yes`/`no` (also `1`/`true`/`t`/`y`). Blank leaves it unchanged. |
| `Attendance mode` | No | `on_premise`, `hybrid` or `virtual` (or the label, e.g. `On the premises`). Default `on_premise`. |
| `Venue` | No | An **on-premises room**, written as `Building — Room` (the form the export uses, e.g. `Marks Building — Committee Room M314`). A bare room name is accepted when it is unambiguous. Leave blank for an off-site meeting. |
| `Off-site venue` | No | Name of an external venue. |
| `Off-site address` | No | Address of an external venue. |
| `Meeting link` | No | Join URL for a hybrid/virtual meeting. |
| `Host group` | No | The **group's name**. The attendance register is generated from its members. |
| `Chaired by` | No | A user's **username or email**. |
| `Secretary` | No | A user's **username or email**. |
| `Quorum` | No | A whole number. Blank for none. |
| `Priority` | No | `low`, `medium`, `high` or `urgent` (or the label). Default `medium`. |
| `Assigned to` | No | A user's **username or email**. |
| `Owner` | No | A user's **username or email**. On create this defaults to **you** (the importer). |
| `State` | No | A workflow state **name** for the Meeting workflow (e.g. `Scheduled`). Defaults to the initial state. |
| `Description` | No | Free text summary. |
| `Agenda` | No | The meeting agenda. Plain text is fine — it is rendered as limited rich text. |
| `Notes` | No | Free text notes. |

### Meeting types

`Meeting type` accepts the slug or the label, in any case:

| Slug | Label |
| --- | --- |
| `plenary` | House Plenary |
| `joint_sitting` | Joint Sitting |
| `committee` | Committee Meeting |
| `portfolio_committee` | Portfolio Committee |
| `select_committee` | Select Committee |
| `ad_hoc_committee` | Ad Hoc Committee |
| `administration` | Administration Meeting |
| `management` | Management Meeting |
| `task_team` | Task Team |
| `workshop` | Workshop |
| `public_participation` | Public Participation |
| `other` | Other |

## Dates and times

Written in **local time** (`Africa/Johannesburg`). Accepted shapes:

* ISO — `2026-11-12 09:30`, `2026-11-12T09:30`, `2026-11-12 09:30:00`, and a bare
  date `2026-11-12` (meaning midnight);
* day-first — `12/11/2026 09:30`, `12/11/2026`.

The export writes `YYYY-MM-DD HH:MM`, which re-imports unchanged.

## How a row is applied

1. The row is **validated on its own**. Anything that cannot be resolved — an
   unknown meeting type, an unknown venue / host group / user, an unparseable
   date, an `End` before `Start`, a missing required cell — is reported
   (`line N: …`) and the row is **skipped**. The rest of the file still imports.
2. If `Reference` matches an existing meeting, that meeting is **updated**
   (only the cells you supplied — a blank cell leaves the field untouched). You
   need edit rights on that meeting, or the row is skipped.
3. Otherwise a meeting is **created** with the values supplied; `Owner` defaults
   to you and `State` to the workflow's initial state. Its attendance register is
   generated from the host group, exactly as the *New meeting* form does.

An import **never deletes** a meeting, and it **does not email** anyone — an
import of many meetings will not spam the audience. (Document-change and
status-change alerts still fire for changes you make afterwards through the UI.)

## Example (CSV)

```csv
Reference,Title,Meeting type,Start,End,Attendance mode,Venue,Host group,Chaired by,Priority
NA/PC-HEALTH/2026-11-05,Portfolio Committee on Health,committee,2026-11-05 09:30,2026-11-05 12:00,on_premise,Marks Building — Committee Room M314,ICT: IT Systems And Application Support Unit,organiser,high
,Ad hoc budget briefing,ad_hoc_committee,2026-11-18 14:00,,virtual,,,,medium
```

The first row updates the meeting with that reference (if it exists); the second
creates a new one.

## Permissions

* **Export** is scoped to what you may view, exactly like the calendar — a file
  can never contain a meeting whose own page would refuse you.
* **Import** requires the role that may create meetings (the same capability the
  *New meeting* button requires). Creating the meeting is checked once for the
  whole file; a row that would update a meeting you may not edit is skipped and
  reported.

## Getting a template

Download one from the import page, or from the header the page documents:

```
/pwms/meetings/import/template/?format=csv
/pwms/meetings/import/template/?format=xlsx
```

A template holds only the header row (and, for `.xlsx`, a *Read me* sheet), so
importing it by mistake does nothing.
