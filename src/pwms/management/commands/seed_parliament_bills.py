"""Import the bills listed on parliament.gov.za into the Bill register.

Unlike :command:`seed_demo_data` this command is an *import* of the real thing,
for local development. The B-numbers, titles, descriptions, sponsoring
ministries, introduction dates, stage history, version tables and document
links come from the real parliament.gov.za listing:

* ``https://www.parliament.gov.za/bills`` — "Bills Currently in Parliament";
  the AJAX feed behind it (``POST /bills``) returns one page of rows per tab
  (National Assembly / NCOP / withdrawn). A dated snapshot ships with the
  command at ``data/parliament_bills.json``; ``--fetch`` re-reads the live
  listing and detail pages and rewrites the snapshot.
* ``https://www.parliament.gov.za/bill/<id>`` — the per-bill page, parsed for
  the bill description (long title), the "Introduced by" ministry, the stage
  tracker (with the dates Parliament has published) and the Previous
  Versions table.

What is imported per bill:

* ``bill_number`` — Parliament's B-number including any version suffix
  (``B29-2026``, ``B27B-2018``);
* ``title`` / ``short_title`` / ``description`` — the long title (the listing
  appends the B-number, which is stripped), a derived short title and the
  description published on the bill page;
* ``introduced_date``, ``sponsor_name`` — the introduction date and the
  "Introduced by" ministry from the bill page;
* ``bill_type`` — the site does not classify bills as s74/75/76/77, so the
  import guesses, conservatively (recorded in the bill's notes):
  * ``Constitution ... Amendment`` titles (and the Reserve Bank amendment,
    which amends the Constitution) → Section 74;
  * the money-bill subjects (appropriation, Division of Revenue, taxation,
    revenue laws, rates on monetary amounts) → Section 77;
  * titles signalling provincial competence (local government/municipal,
    traditional leadership, water, housing, basic education, health,
    gambling, liquor, agriculture, land, tobacco, ...) → Section 76;
  * everything else → Section 75 (ordinary).
* ``responsible_committee`` — guessed from the sponsoring ministry
  ("Minister of Health" → Portfolio Committee on Health); when no committee
  with that name exists on *this* environment the field is left empty and
  the miss is reported. Finance bills are refined by subject:
  appropriation/Division-of-Revenue bills to the Standing Committee on
  Appropriations, the rest of Treasury's bills to the Standing Committee on
  Finance;
* ``bill_document_url`` and the ``BillVersion`` history (the Previous
  Versions table, earliest row marking the introduced version).

State mapping — records are placed where Parliament actually says they are:

* stages carry dates on the bill page — created bills are walked through the
  *dated* stages in order, so the history is the published history;
* ``NCOP`` (before the National Council of Provinces) — one further,
  conservative advance to ``Select Committee``, the start of the NCOP leg
  (the site's badge says "NCOP" but not which part of that leg);
* ``WD`` (withdrawn / lapsed) — advanced to ``Withdrawn`` once the dated
  stages are in (the withdrawal itself is undated on the site; it is dated
  between the last stage and now, and the transition comment says so).

``NA`` bills whose page lists no dated stage beyond Introduction simply stay
there — the site's own position for them.

Only creation walks the state machine. A bill that already exists (matched by
``bill_number``) has its state and non-empty profile fields left untouched —
its blank profile fields are filled in, a missing responsible committee is
guessed, and missing versions are recorded — so the import is safe to re-run
over manually edited data. ``--purge-imported`` first removes the bills this
command imported before (matched by workflow type ``Bill`` plus the imported
provenance note, so manually created bills survive), for a clean re-import.

Each imported bill also gets its SharePoint folder under the Bill Base Folder
(``BILL_DRIVE``/``BILL_FOLDER`` in the environment): the B-number folder is
created when missing (via ``pwms.services.bill_folders``) and mirrored onto
``Bill.sharepoint_folder``, so seeded bills are as wired for attachments as
hand-created ones. Filing happens after the import transaction commits — Graph
round trips must not hold the database connection — and is idempotent:
``--purge-imported`` re-runs relink the folders that are already there, and
bills that still carry their mirror row are skipped without a round trip.
Unconfigured environments skip the filing with a note; ``--no-sharepoint``
opts out.

Timestamps are back-dated onto the published dates, so the bills arrive with
a plausible history instead of a single batch stamp.

Usage::

    python manage.py seed_parliament_bills              # import everything
    python manage.py seed_parliament_bills --fetch      # re-read the live site
    python manage.py seed_parliament_bills --limit 10   # only the 10 newest
    python manage.py seed_parliament_bills --purge-imported  # reset & re-import
    python manage.py seed_parliament_bills --no-sharepoint   # skip SP filing
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib import request as urlrequest

from auditlog.context import set_actor
from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from pwms.models import (
    Attachment,
    Bill,
    BillVersion,
    Group,
    Notification,
    Transition,
    TransitionLog,
    User,
    WorkflowEvent,
    WorkflowGroupAccess,
    WorkflowReferral,
    WorkflowType,
)
from pwms.services import bill_folders

from .seed_demo_data import SeededWorkflow, silenced_notifications

#: Snapshot of the listing, fetched from parliament.gov.za. Rebuild with --fetch.
DATA_FILE = Path(__file__).resolve().parent / "data" / "parliament_bills.json"

#: The AJAX feed behind the listing (POST {page, bill_status}; 20 rows a page)
#: and the per-bill page carrying description, sponsor, stages and versions.
LISTING_URL = "https://www.parliament.gov.za/bills"
BILL_URL = "https://www.parliament.gov.za/bill/{bill_id}"
DOCUMENT_URL_HOST = "https://www.parliament.gov.za"

#: The provenance note every imported bill carries; also the purge marker.
IMPORT_NOTE_PREFIX = "Imported from parliament.gov.za bill listing"

#: Stands in for the client address on the seeded audit rows, as in the demo.
IP_ADDRESS = "10.10.4.21"

#: Bill team usernames (the seed_demo_data PEOPLE keys). Each imported record's
#: owner / assignee / actor is picked from the members that exist on this
#: environment, so the register looks worked-in for local development.
OWNER_CANDIDATES = (
    "tmbatha",
    "acupido",
    "zcele",
    "febrahim",
    "aarnold",
    "dkassan",
    "bloots",
    "tmcgivern",
    "thalley-starkey",
    "tgangen",
    "sisaac",
    "fjenkins",
)

#: Source listing status -> conservative *derived* stages. Only used to walk
#: one or two further steps for bills whose page tracker has no dates yet:
#: an ``NCOP`` badge means the NCOP leg starts (``Select Committee``), and a
#: ``WD`` badge means the bill is out (``Withdrawn``).
STATUS_EXTRA_STAGES = {
    "NCOP": ("Select Committee",),
    "WD": ("Withdrawn",),
}

#: The " (B29–2026)" B-number the listing appends to every long title (the
#: source style varies: "B29-2026", "B 23-2025").
B_NUMBER_SUFFIX = re.compile(r"\s*\(\s*B\s*\d+[A-Z]?\s*[–—-]\s*\d{4}\s*\)\s*$")

#: Titles that put a classification beyond doubt (the site publishes none).
SECTION_74_HINTS = ("constitution", "reserve bank amendment")

#: s77 money bills: appropriation / revenue / taxation subjects.
SECTION_77_HINTS = (
    "appropriation",
    "division of revenue",
    "rates on monetary amounts",
    "taxation laws",
    "tax administration laws",
    "amendment of revenue laws",
)

#: s76: titles signalling a provincial competence (Schedule 4/5 areas).
SECTION_76_HINTS = (
    r"\blocal government\b",
    r"\bmunicipal\b",
    r"\btraditional\b",
    "khoi-san",
    r"\bwater\b",
    r"\bhousing\b",
    r"\bhuman settlements\b",
    r"\bbasic education\b",
    r"\bhealth\b",
    r"\bmedical aid\b",
    r"\bliquer\b",
    r"\bgambling\b",
    r"\bagricultur",
    r"\bland\b",
    r"\beviction\b",
    r"\btobacco\b",
    r"\bintergovernmental\b",
    r"\bcooperative governance\b",
    r"\bperforming animals\b",
)

#: "Minister of <X>" -> a portfolio committee name that is spelled differently
#: from the ministry. Ministries whose name matches a committee exactly (e.g.
#: "Minister of Health" -> "Portfolio Committee on Health") resolve without
#: an entry. Only committees that exist as Groups in the database are linked.
MINISTRY_COMMITTEE_ALIASES = {
    "finance": None,  # special-cased by subject (Appropriations / Finance)
    "justice and correctional services": (
        "Portfolio Committee on Justice and Constitutional Development"
    ),
    "forestry, fisheries and the environment": (
        "Portfolio Committee on Forestry, Fisheries and Environment"
    ),
    "forestry, fisheries and environment": (
        "Portfolio Committee on Forestry, Fisheries and Environment"
    ),
    "trade and industry": ("Portfolio Committee on Trade, Industry and Competition"),
    "trade, industry and competition": (
        "Portfolio Committee on Trade, Industry and Competition"
    ),
    "economic development": ("Portfolio Committee on Trade, Industry and Competition"),
    "electricity and energy": "Portfolio Committee on Electricity and Energy",
    "mineral resources and energy": "Portfolio Committee on Mineral and Petroleum Resources",
    "mineral and petroleum resources": "Portfolio Committee on Mineral and Petroleum Resources",
    "mineral resources": "Portfolio Committee on Mineral and Petroleum Resources",
    "petroleum resources": "Portfolio Committee on Mineral and Petroleum Resources",
    "water and sanitation": "Portfolio Committee on Water and Sanitation",
    "human settlements and water": "Portfolio Committee on Human Settlements",
    "land reform and rural development": (
        "Portfolio Committee on Land Reform and Rural Development"
    ),
    "rural development and land reform": (
        "Portfolio Committee on Land Reform and Rural Development"
    ),
    "agriculture, land reform and rural development": (
        "Portfolio Committee on Land Reform and Rural Development"
    ),
    "higher education, science and technology": "Portfolio Committee on Higher Education",
    "higher education, science and innovation": "Portfolio Committee on Higher Education",
    "higher education and training": "Portfolio Committee on Higher Education",
    "science, technology and innovation": (
        "Portfolio Committee on Science, Technology and Innovation"
    ),
    "science and technology": "Portfolio Committee on Science, Technology and Innovation",
    "communications": (
        "Portfolio Committee on Communications and Digital Technologies"
    ),
    "communications and digital technologies": (
        "Portfolio Committee on Communications and Digital Technologies"
    ),
    "telecommunications and postal services": (
        "Portfolio Committee on Communications and Digital Technologies"
    ),
    "police": "Portfolio Committee on Police",
    "cooperative governance and traditional affairs": (
        "Portfolio Committee on Cooperative Governance and Traditional Affairs"
    ),
    "co-operative governance and traditional affairs": (
        "Portfolio Committee on Cooperative Governance and Traditional Affairs"
    ),
    "correctional services": "Portfolio Committee on Correctional Services",
    "the presidency": "Committee on The Presidency",
    "presidency": "Committee on The Presidency",
    "women, youth and persons with disabilities": (
        "Portfolio Committee on Women, Youth and Persons with Disabilities"
    ),
    "women in the presidency": (
        "Portfolio Committee on Women, Youth and Persons with Disabilities"
    ),
}


def strip_b_number_suffix(title):
    """Drop the B-number the listing appends to titles."""
    return B_NUMBER_SUFFIX.sub("", title).strip()


def derive_short_title(title, max_length=255):
    """Derive a short title: "Amendment Bill" -> the substance ("BRS §7A")."""
    short = title.strip()
    short = short.removesuffix(" Bill")
    short = short.removesuffix(" Amendment")
    return short[:max_length]


def parse_introduced_date(value):
    """Parse the listing's ISO introduced date; None when absent."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except TypeError, ValueError:
        return None


def classify_bill_type(title):
    """Guess the s74/75/76/77 classification from the long title.

    The site publishes no such classification, so this is a conservative,
    keyword-based guess (recorded in the bill's notes); blank titles return
    empty so nothing is fabricated beyond the title.
    """
    lowered = title.lower()
    if any(hint in lowered for hint in SECTION_74_HINTS):
        return Bill.SECTION_74
    if any(hint in lowered for hint in SECTION_77_HINTS):
        return Bill.SECTION_77
    for hint in SECTION_76_HINTS:
        if re.search(hint, lowered):
            return Bill.SECTION_76
    return Bill.SECTION_75


def committee_candidates(sponsor_name, title):
    """Committee names to try for a bill, best guess first, or an empty list.

    Driven by the sponsoring ministry (``Minister of Health`` → Portfolio
    Committee on Health). The caller matches the guesses against the Group
    table case-insensitively and only links a committee that exists on this
    environment.
    """
    sponsor = (sponsor_name or "").strip()
    match = re.match(
        r"^(?:the\s+)?minister of (.+?)\s*[.)]*\s*$", sponsor, re.IGNORECASE
    )
    if not match:
        return []
    ministry = re.sub(r"\s+", " ", match.group(1)).strip().lower().removesuffix(".")
    # The committees' own names drop a stray "the" ("Forestry, Fisheries
    # and Environment" vs the ministry's "... and the Environment").
    ministry = re.sub(r"\bthe\b\s*", "", ministry).strip()

    if ministry.startswith("finance"):
        money_terms = ("appropriation", "division of revenue")
        if any(term in title.lower() for term in money_terms):
            return ["Standing Committee on Appropriations"]
        return ["Standing Committee on Finance"]

    candidates = ["portfolio committee on " + ministry]
    alias = MINISTRY_COMMITTEE_ALIASES.get(ministry)
    if alias:
        candidates.append(alias)
    return candidates


def parse_bill_detail(page_html):
    """Extract the per-bill data from a /bill/<id> page.

    Returns a dict with ``description``, ``introduced_by``, ``stages`` (our
    state names -> ISO dates, only the stages the site has dates for) and
    ``versions`` (the Previous Versions table as ``{label, date, url, title}``
    rows in table order).
    """
    description = ""
    match = re.search(r'<div class="cell auto"><p[^>]*>(.*?)</p>', page_html, re.DOTALL)
    if match:
        description = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1)))
        description = html_mod.unescape(description).strip()

    introduced_by = ""
    match = re.search(r"Introduced by:</strong>\s*([^<]+)", page_html)
    if match:
        introduced_by = html_mod.unescape(match.group(1)).strip()

    # Stage tracker: the block repeats for each responsive breakpoint, and the
    # sidebar's own <h6>s leak later, so accept only known stage names and keep
    # the first date seen for each.
    stages: dict[str, date] = {}
    known_stages = {
        "introduction",
        "portfolio committee",
        "national assembly",
        "select committee",
        "national council of provinces",
        "sent for assent",
        "assented",
    }
    for match in re.finditer(
        r"<h6>([^<]+)</h6>(?:.{0,300}?<div class=\"date\">([^<]*)</div>)?",
        page_html,
        re.DOTALL,
    ):
        name = html_mod.unescape(match.group(1)).strip().lower()
        if name not in known_stages:
            continue
        if name not in stages:  # first block wins; later blocks repeat it
            stages[name] = None
        raw_date = (match.group(2) or "").strip()
        if raw_date:
            try:
                # A publication date only; no time is implied.
                stages[name] = datetime.strptime(raw_date, "%d-%m-%Y").date()  # noqa: DTZ007
            except ValueError:
                pass

    versions = []
    match = re.search(r"Previous Versions</strong>(.*?)</table>", page_html, re.DOTALL)
    if match:
        for row in re.finditer(r"<tr>(.*?)</tr>", match.group(1), re.DOTALL):
            label_match = re.search(r"<button[^>]*>([^<]+)</button>", row.group(1))
            if not label_match:
                continue
            label = html_mod.unescape(label_match.group(1)).strip()
            url_match = re.search(r'href="(https?://[^"]+\.pdf[^"]*)"', row.group(1))
            date_match = re.search(r"<td align=center>([^<]*)</td>", row.group(1))
            title_text = ""
            title_search = re.search(r"<td><a[^>]*>([^<]+)</a>", row.group(1))
            if title_search:
                title_text = html_mod.unescape(title_search.group(1)).strip()
            versions.append(
                {
                    "label": label,
                    "date": _version_date.isoformat()
                    if (
                        _version_date := parse_introduced_date(
                            date_match.group(1).strip()
                        )
                    )
                    else None,
                    "url": url_match.group(1) if url_match else "",
                    "title": strip_b_number_suffix(title_text),
                }
            )

    return {
        "description": description,
        "introduced_by": introduced_by,
        "stages": {
            name: value.isoformat() if value else None for name, value in stages.items()
        },
        "versions": versions,
    }


class Command(BaseCommand):
    help = (
        "Import the bills published on parliament.gov.za (real B-numbers, "
        "titles, sponsors, dates, stage history and document links) for "
        "local development."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--fetch",
            action="store_true",
            help=(
                "Re-read the live listing and bill pages from parliament.gov.za, "
                f"and rewrite the bundled snapshot ({DATA_FILE.name})."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            metavar="N",
            help="Only import the N most recently introduced bills.",
        )
        parser.add_argument(
            "--purge-imported",
            action="store_true",
            help=(
                "First delete the bills this command imported before (its "
                "own records only; manually created bills are kept), for a "
                "clean re-import."
            ),
        )
        parser.add_argument(
            "--no-sharepoint",
            action="store_true",
            help=(
                "Skip filing each bill's SharePoint folder under the Bill "
                "Base Folder (default: file it when it does not exist)."
            ),
        )

    def handle(self, *args, **options):
        self.now = timezone.now()
        self.records: list[SeededWorkflow] = []
        self.created_count = 0
        self.updated_count = 0
        self.unchanged_count = 0
        self.unmatched_committees: set[str] = set()
        # SharePoint filing: the bills this run processed plus its outcome(s).
        # Filing happens after the transaction commits (see the docstring).
        self.filing_bills: list[Bill] = []
        self.folder_created_count = 0
        self.folder_relinked_count = 0
        self.folder_failures: list[tuple[str, str]] = []
        self.folder_skipped = ""

        bills = self._fetch_listing() if options["fetch"] else self._load_snapshot()
        if options["limit"] is not None:
            bills = bills[: max(options["limit"], 0)]

        self._load_reference_data()

        self.stdout.write(f"Importing {len(bills)} bills from parliament.gov.za...")

        with transaction.atomic(), silenced_notifications():
            if options["purge_imported"]:
                self._purge_imported()
            for spec in bills:
                self._import_bill(spec)

        if options["no_sharepoint"]:
            self.folder_skipped = "skipped (--no-sharepoint)"
        else:
            self._file_sharepoint_folders()

        if self.records:
            self._backdate()

        self._report()

    # -- reference data ------------------------------------------------------
    def _load_reference_data(self):
        try:
            self.bill_type_model = WorkflowType.objects.get(name="Bill")
        except WorkflowType.DoesNotExist:
            raise CommandError(
                "Workflow type 'Bill' is not registered. Migrate/seed the "
                "workflow definitions before importing."
            )
        self.initial_state = self.bill_type_model.get_initial_state()
        self.states = {s.name.lower(): s for s in self.bill_type_model.states.all()}
        # Case-insensitive index of every committee in the system: a guessed
        # committee is only linked when its name exists as a Group here.
        self.committees = {
            group.name.casefold(): group
            for group in Group.objects.filter(name__icontains="committee")
        }

        by_username = {
            user.username: user
            for user in User.objects.filter(
                username__in=OWNER_CANDIDATES, is_active=True
            )
        }
        self.candidates = [
            by_username[username]
            for username in OWNER_CANDIDATES
            if username in by_username
        ]
        if not self.candidates:
            raise CommandError(
                "None of the bill-team accounts were found on this database: "
                + ", ".join(OWNER_CANDIDATES)
            )

    def _pick_user(self, bill_number):
        """Deterministic bill-team member for this bill (stable across runs)."""
        digest = int(hashlib.sha1(bill_number.encode()).hexdigest(), 16)
        return self.candidates[digest % len(self.candidates)]

    def _state(self, name):
        """A state of the Bill machine by (case-insensitive) name."""
        state = self.states.get(name.lower())
        if state is None:
            raise CommandError(
                f"The Bill workflow type has no state '{name}'. Has the "
                "definition been customised?"
            )
        return state

    # -- source data ---------------------------------------------------------
    def _load_snapshot(self):
        try:
            payload = json.loads(DATA_FILE.read_text())
        except OSError as error:
            raise CommandError(f"Cannot read {DATA_FILE}: {error}")
        except json.JSONDecodeError as error:
            raise CommandError(f"{DATA_FILE} is not valid JSON: {error}")
        bills = payload.get("bills") or []
        self.snapshot = payload
        self.stdout.write(
            f"Loaded a snapshot of {len(bills)} bills "
            f"(fetched {payload.get('fetched_at', '?')}, "
            f"{payload.get('source', LISTING_URL)})."
        )
        return self._sort_specs(bills)

    def _fetch_listing(self):
        """Re-read the live listing, then enrich from the per-bill pages."""
        self.stdout.write(f"Fetching the live bill listing from {LISTING_URL}...")
        seen: dict[str, dict] = {}
        try:
            for bill_status in (1, 2, 3):
                page = 1
                while True:
                    request = urlrequest.Request(
                        LISTING_URL,
                        data=json.dumps(
                            {"page": page, "bill_status": bill_status, "query": {}}
                        ).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                    with urlrequest.urlopen(request, timeout=30) as response:
                        body = json.load(response)
                    if not body.get("success"):
                        raise RuntimeError(
                            f"the feed signalled failure (tab {bill_status})"
                        )
                    for raw in body.get("bills") or []:
                        seen.setdefault(raw["bill_no"], raw)
                    total = body.get("total_count") or 0
                    if page * 20 >= total:
                        break
                    page += 1
        except (OSError, RuntimeError, json.JSONDecodeError, KeyError) as error:
            raise CommandError(
                f"Could not read the live listing ({error}). Re-run without "
                "--fetch to use the bundled snapshot."
            )

        bills = list(seen.values())
        self.stdout.write(f"Fetching {len(bills)} bill detail pages...")
        for raw in bills:
            bill_id = raw.get("id")
            if not bill_id:
                continue
            try:
                with urlrequest.urlopen(
                    BILL_URL.format(bill_id=bill_id), timeout=30
                ) as response:
                    page_html = response.read().decode("utf-8", "replace")
                raw["detail"] = parse_bill_detail(page_html)
            except OSError as error:
                self.stdout.write(
                    self.style.WARNING(
                        f"  ! {raw['bill_no']}: detail page unavailable ({error})"
                    )
                )

        payload = {
            "source": "https://www.parliament.gov.za/bills",
            "endpoint": (
                "POST /bills {page, bill_status} + GET /bill/<id> (AJAX feed "
                "behind the Bills Currently in Parliament page)"
            ),
            "fetched_at": timezone.localdate().isoformat(),
            "notes": (
                "Snapshot of the bills listed on parliament.gov.za (tabs: "
                "National Assembly, NCOP, Withdrawn/Lapsed). bill_status: NA = "
                "before the National Assembly, NCOP = before the National "
                "Council of Provinces, WD = withdrawn/lapsed. bill_no_URL is "
                "the parliament.gov.za path to the bill PDF; detail carries "
                "the bill page's description, Introduced by, stage dates and "
                "Previous Versions rows."
            ),
            "count": len(bills),
            "bills": self._sort_specs(bills),
        }
        try:
            DATA_FILE.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            )
            self.stdout.write(f"Snapshot rewritten to {DATA_FILE.name}.")
        except OSError as error:
            self.stdout.write(
                self.style.WARNING(f"  ! could not rewrite {DATA_FILE.name} ({error})")
            )
        self.snapshot = payload
        return payload["bills"]

    @staticmethod
    def _sort_specs(raw_bills):
        """Newest introduction first, one row per B-number."""
        return sorted(
            raw_bills,
            key=lambda raw: (raw.get("introduced_date") or "", raw["bill_no"]),
            reverse=True,
        )

    # -- import --------------------------------------------------------------
    def _import_bill(self, spec):
        bill_number = (spec.get("bill_no") or "").strip()
        if not bill_number:
            self.stdout.write(self.style.WARNING("  skipped a row with no B-number"))
            return
        raw_title = (spec.get("name") or "").strip()
        title = strip_b_number_suffix(raw_title) or raw_title
        detail = spec.get("detail") or {}
        introduced_date = parse_introduced_date(spec.get("introduced_date"))
        sponsor = (detail.get("introduced_by") or "").strip()
        status = (spec.get("bill_status") or "").strip().upper()
        owner = self._pick_user(bill_number)

        if introduced_date and introduced_date > timezone.localdate():
            raise CommandError(
                f"{bill_number}: introduction date {introduced_date} is in the "
                "future; refusing to import it."
            )

        committee = next(
            (
                group
                for group in (
                    self.committees.get(candidate.casefold())
                    for candidate in committee_candidates(sponsor, title)
                )
                if group is not None
            ),
            None,
        )
        if sponsor and committee is None:
            self.unmatched_committees.add(sponsor)

        fields = {
            "title": title[:500],
            "short_title": derive_short_title(title),
            "description": (detail.get("description") or "")[:20000],
            "introduced_date": introduced_date,
            "sponsor_name": sponsor[:255],
            "bill_type": classify_bill_type(title),
            "house_of_origin": Bill.NA if status in ("NA", "NCOP") else "",
            "bill_document_url": (
                DOCUMENT_URL_HOST + spec["bill_no_URL"]
                if spec.get("bill_no_URL")
                else ""
            ),
            "notes": self._build_notes(spec, status, committee),
        }

        defaults = {
            "workflow_type": self.bill_type_model,
            "current_state": self.initial_state,
            "owner": owner,
            "responsible_committee": committee,
            **fields,
        }

        with set_actor(owner, remote_addr=IP_ADDRESS):
            bill, created = Bill.objects.get_or_create(
                bill_number=bill_number, defaults=defaults
            )

        if created:
            record = SeededWorkflow(
                model=Bill,
                instance=bill,
                created_at=self.moment_on(introduced_date),
            )
            self.records.append(record)
            self._walk_states(record, spec, status)
            if bill.responsible_committee_id:
                self._grant_view(bill, bill.responsible_committee)
            self.created_count += 1
            committee_note = (
                f" -> {bill.responsible_committee.name}"
                if bill.responsible_committee_id
                else ""
            )
            self.stdout.write(
                f"  + {bill_number} [{status or '?'}]{committee_note} {title[:52]}"
            )
        else:
            self._fill_gaps(bill, owner, fields)
            if bill.responsible_committee_id is None and committee:
                bill.responsible_committee = committee
                with set_actor(owner, remote_addr=IP_ADDRESS):
                    bill.save(update_fields=["responsible_committee", "updated_at"])
                self._grant_view(bill, committee)
                self.stdout.write(f"      {bill_number}: committee -> {committee.name}")

        self._ensure_versions(bill, owner, spec)
        self.filing_bills.append(bill)

    # -- sharepoint filing ----------------------------------------------------
    def _file_sharepoint_folders(self):
        """File each imported bill's folder under the Bill Base Folder.

        Through :func:`pwms.services.bill_folders.file_bill_folder`: creates
        the B-number folder when missing (``POST
        /drives/{drive-id}/items/{parent-item-id}/children``) or resolves the
        folder that is already there, and mirrors both onto
        ``Bill.sharepoint_folder``. Filing is best-effort per bill — one
        failed folder never stops the import — and idempotent, so re-runs
        (including after ``--purge-imported``, which resets the links)
        converge without duplicating anything on SharePoint. Bills that still
        carry their mirror row are skipped: nothing to confirm, no round trip.
        """
        if not bill_folders.filing_configured():
            self.folder_skipped = "skipped (BILL_DRIVE/BILL_FOLDER are not configured)"
            return

        to_file = [
            bill for bill in self.filing_bills if bill.sharepoint_folder_id is None
        ]
        relinkable = len(self.filing_bills) - len(to_file)
        if not to_file:
            self.folder_relinked_count = relinkable
            return
        self.stdout.write(
            f"Filing {len(to_file)} bill folder{'s' if len(to_file) != 1 else ''} "
            f"in SharePoint (skipping {relinkable} already linked)..."
        )
        for bill in to_file:
            try:
                # Mirror rows/audit entries attribute to the bill's owner, as
                # the mechanical writes in this command do.
                with set_actor(bill.owner, remote_addr=IP_ADDRESS):
                    status = bill_folders.file_bill_folder(bill)
            except bill_folders.BillFolderError as error:
                self.folder_failures.append((bill.bill_number, str(error)))
                continue
            if status == bill_folders.CREATED:
                self.folder_created_count += 1
            else:
                self.folder_relinked_count += 1

    def _build_notes(self, spec, status, committee):
        """The import provenance plus any listing flags."""
        detail = spec.get("detail") or {}
        lines = [f"{IMPORT_NOTE_PREFIX} (status {status!r})."]
        lines.append(
            "bill_type is a title-based guess; the site publishes no "
            "s74/75/76/77 classification."
        )
        sponsor = (detail.get("introduced_by") or "").strip()
        if sponsor:
            if committee:
                lines.append(f"responsible committee inferred from '{sponsor}'")
            else:
                lines.append(
                    f"no committee in the system matches '{sponsor}'; field left empty"
                )
        if spec.get("referred_nhtl"):
            lines.append("Referred to the National House of Traditional Leaders.")
        dated_stages = sorted(
            (day, name) for name, day in (detail.get("stages") or {}).items() if day
        )
        if dated_stages:
            listing = ", ".join(f"{name} {day}" for day, name in dated_stages)
            lines.append(f"Stage dates from the bill page: {listing}.")
        return "\n".join(lines)

    def _walk_states(self, record, spec, status):
        """Place a new bill where Parliament says it is.

        The bill page's stage tracker carries a date for every stage the
        listing has progressed through; only those dated stages are walked
        (the site's own evidence). A gap in the tracker (an undated earlier
        stage) is bridged by the machine's own path. The listing badge adds
        one (or both) final, undated position: ``NCOP`` (the NCOP leg) and
        ``WD`` (out of the House, withdrawn / lapsed).
        """
        detail = spec.get("detail") or {}
        stages = detail.get("stages") or {}
        # "Introduction" is the creation point (already the created_at); the
        # dated stages afterwards are the transitions.
        for state_name in (
            "Portfolio Committee",
            "National Assembly",
            "Select Committee",
            "National Council Of Provinces",
            "Sent For Assent",
            "Assented",
        ):
            stage_date = parse_introduced_date(
                (stages.get(state_name.lower()) or "").strip()
            )
            if stage_date is None:
                continue  # Parliament has not dated this stage for this bill
            self._route(record, state_name, when=self.moment_on(stage_date))

        for extra in STATUS_EXTRA_STAGES.get(status, ()):
            target = self._state(extra)
            already = (
                not target.is_terminal
                and target.order < record.instance.current_state.order
            )
            if not already:
                self._route(record, extra)

    def _route(self, record, target_name, *, when=None):
        """Advance the (new) bill to ``target_name``, chaining as needed.

        A dated stage (``when`` set) is stamped with its published date; a
        derived position (``when`` None, from the listing badge) is spaced
        between the last stage and now. Where the machine has no direct
        transition, the path is found and intermediate hops are interpolated;
        their comments say they are derived.
        """
        instance = record.instance
        target = self._state(target_name)
        if when is not None:
            when = min(max(when, self.moment_on(instance.introduced_date)), self.now)
        path = self._path_between(instance, target)
        if path is None:
            self.stdout.write(
                self.style.WARNING(
                    f"  ! {instance.bill_number}: no path from "
                    f"'{instance.current_state.name}' to '{target_name}'; "
                    "left there"
                )
            )
            return
        if not path:  # already at the target
            return

        previous = (
            record.steps[-1]
            if record.steps
            else self.moment_on(instance.introduced_date)
        )
        if when is None:
            gap = timedelta(days=30 if target.is_terminal else 1)
            step_times = [
                min(previous + gap * (index + 1), self.now)
                for index in range(len(path))
            ]
        else:
            window = max((when - previous).total_seconds(), 0)
            step_times = [
                max(
                    min(
                        previous + timedelta(seconds=window * (index + 1) / len(path)),
                        when,
                    ),
                    previous,
                )
                for index in range(len(path))
            ]

        owner = instance.owner
        for transition, step_when in zip(path, step_times):
            with set_actor(owner, remote_addr=IP_ADDRESS):
                instance.perform_transition(
                    transition,
                    actor=owner,
                    comment=self._step_comment(instance, transition, target, when),
                    ip_address=IP_ADDRESS,
                )
            record.steps.append(step_when)

    def _path_between(self, instance, target):
        """The shortest chain of transitions from current state to target.

        Breadth-first over the machine's transition graph (canonical order
        preferred); None when the target cannot be reached at all.
        """
        if instance.current_state_id == target.pk:
            return []
        transitions = list(
            Transition.objects.filter(workflow_type=instance.workflow_type).order_by(
                "order"
            )
        )
        graph: dict[int, list[Transition]] = {}
        for transition in transitions:
            graph.setdefault(transition.from_state_id, []).append(transition)
        frontier = [(instance.current_state_id, [])]
        visited = {instance.current_state_id}
        while frontier:
            state_id, chain = frontier.pop(0)
            for transition in graph.get(state_id, []):
                next_chain = [*chain, transition]
                if transition.to_state_id == target.pk:
                    return next_chain
                if transition.to_state_id not in visited and not (
                    transition.to_state.is_terminal
                ):
                    visited.add(transition.to_state_id)
                    frontier.append((transition.to_state_id, next_chain))
        return None

    def _step_comment(self, instance, transition, target, when):
        """What the step means and where it came from."""
        bill_number = instance.bill_number
        if transition.to_state_id != target.pk:
            # An intermediate hop bridging an undated stage in the tracker.
            return (
                f"{bill_number} reaches '{target.name}' (dated on "
                "parliament.gov.za) but this intermediate stage carries no "
                "date there; recorded along the machine's path."
            )
        if when is None:
            if target.name == "Withdrawn":
                return (
                    f"{bill_number} is in the withdrawn/lapsed listing on "
                    "parliament.gov.za; the withdrawal date is not published "
                    "— recorded between its last stage and the import date."
                )
            return (
                f"{bill_number} is listed before the NCOP on parliament.gov.za "
                "but no date is published for this stage; recorded along the "
                "machine's path."
            )
        return "Stage dated on parliament.gov.za (imported)."

    def _fill_gaps(self, bill, owner, fields):
        """Update an existing bill's *blank* profile fields only."""
        fillable = {}
        for name, value in fields.items():
            current = getattr(bill, name)
            if not value:
                continue
            if current is None or (isinstance(current, str) and not current.strip()):
                fillable[name] = value
        if not fillable:
            self.unchanged_count += 1
            self.stdout.write(f"  = {bill.bill_number}: unchanged")
            return
        for name, value in fillable.items():
            setattr(bill, name, value)
        self.updated_count += 1
        self.stdout.write(
            f"  ~ {bill.bill_number}: filled {', '.join(sorted(fillable))}"
        )
        with set_actor(owner, remote_addr=IP_ADDRESS):
            bill.save(update_fields=[*fillable, "updated_at"])

    def _ensure_versions(self, bill, owner, spec):
        """Record the version history (BRS §15A) when rows are missing.

        The site's Previous Versions table is authoritative: the earliest
        dated row is the introduced version, later B-numbered prints are
        amendment prints, and the row matching the bill's own B-number is the
        current one.
        """
        rows = (spec.get("detail") or {}).get("versions") or []
        existing = set(bill.versions.values_list("version_label", flat=True))
        ordered = sorted(rows, key=lambda row: row.get("date") or date.max)
        for index, row in enumerate(ordered):
            label = row["label"][:100]
            if label in existing:
                continue
            row_date = parse_introduced_date(row.get("date"))
            # Current only when the row matches the bill's own B-number and
            # no version has been flagged yet (rows are append-only history).
            is_current = (
                label == bill.bill_number
                and not bill.versions.filter(is_current=True).exists()
            )
            version_type = BillVersion.INTRODUCED if index == 0 else BillVersion.AMENDED
            BillVersion.objects.get_or_create(
                bill=bill,
                version_label=label,
                defaults={
                    "version_type": version_type,
                    "version_date": row_date
                    or bill.introduced_date
                    or timezone.localdate(),
                    "document_url": row.get("url") or bill.bill_document_url,
                    "is_current": is_current,
                    "notes": "Version as published on parliament.gov.za.",
                    "recorded_by": owner,
                },
            )
        self._ensure_current_version_fallback(bill, owner)

    def _ensure_current_version_fallback(self, bill, owner):
        """Guarantee at least one introduced version, flagged current.

        Bills without a Previous Versions table (or whose own B-number is not
        a row in it) still get one version: the bill as introduced.
        """
        if bill.versions.filter(is_current=True).exists():
            return
        latest = bill.versions.order_by("-version_date").first()
        if latest is not None:
            latest.is_current = True
            latest.save(update_fields=["is_current"])
            return
        version = BillVersion.objects.create(
            bill=bill,
            version_label=bill.bill_number,
            version_type=BillVersion.INTRODUCED,
            version_date=bill.introduced_date or timezone.localdate(),
            is_current=True,
            notes="Bill as published on parliament.gov.za.",
            recorded_by=owner,
        )
        when = self.moment_on(bill.introduced_date)
        BillVersion.objects.filter(pk=version.pk).update(
            created_at=when, updated_at=when
        )

    def _grant_view(self, bill, committee):
        """Let the responsible committee see the bill (BRS §13.2)."""
        WorkflowGroupAccess.objects.get_or_create(
            content_type=ContentType.objects.get_for_model(bill),
            object_id=bill.pk,
            group=committee,
            defaults={"can_view": True},
        )

    # -- reset ---------------------------------------------------------------
    def _purge_imported(self):
        """Delete the bills this command imported before (and nothing else).

        Import provenance lives in ``notes`` and every import uses the *Bill*
        workflow type, so manually created bills (other workflow types,
        other notes) are excluded.
        """
        content_type = ContentType.objects.get_for_model(Bill)
        bill_ids = list(
            Bill.objects.filter(
                workflow_type=self.bill_type_model,
                notes__istartswith=IMPORT_NOTE_PREFIX,
            ).values_list("pk", flat=True)
        )
        if not bill_ids:
            self.stdout.write("Nothing previously imported to purge.")
            return
        for queryset, ids in (
            (WorkflowEvent.objects, bill_ids),
            (WorkflowReferral.objects, bill_ids),
            (TransitionLog.objects, bill_ids),
            (Notification.objects, bill_ids),
            (WorkflowGroupAccess.objects, bill_ids),
            (Attachment.objects, [str(pk) for pk in bill_ids]),
            (LogEntry.objects, [str(pk) for pk in bill_ids]),
        ):
            queryset.filter(content_type=content_type, object_id__in=ids).delete()
        BillVersion.objects.filter(bill_id__in=bill_ids).delete()
        Bill.objects.filter(pk__in=bill_ids).delete()
        self.stdout.write(f"Purged {len(bill_ids)} previously imported bills.")

    # -- timing --------------------------------------------------------------
    def moment_on(self, day):
        """09:15 on ``day`` (aware), clamped to now when ``day`` is today."""
        if day is None:
            day = timezone.localdate()
        return min(timezone.make_aware(datetime.combine(day, time(9, 15))), self.now)

    def _backdate(self):
        """Move the "now" stamps onto the introduction / stage times.

        As in :command:`seed_demo_data`: per imported bill, the audit entries
        align to the create time and to the transitions (in order), and
        ``TransitionLog`` rows align the same way.
        """
        for record in self.records:
            content_type = ContentType.objects.get_for_model(record.model)
            last_moment = record.steps[-1] if record.steps else record.created_at

            record.model.objects.filter(pk=record.instance.pk).update(
                created_at=record.created_at,
                updated_at=min(last_moment, self.now),
            )

            logs = list(
                TransitionLog.objects.filter(
                    content_type=content_type, object_id=record.instance.pk
                ).order_by("id")
            )
            for log, when in zip(logs, record.steps):
                TransitionLog.objects.filter(pk=log.pk).update(timestamp=when)

            step_times = iter(record.steps)
            entries = LogEntry.objects.filter(
                content_type=content_type, object_id=record.instance.pk
            ).order_by("id")
            for entry in entries:
                when = (
                    record.created_at
                    if entry.action == 0
                    else next(step_times, last_moment)
                )
                LogEntry.objects.filter(pk=entry.pk).update(timestamp=when)

    # -- summary -------------------------------------------------------------
    def _report(self):
        distribution: dict[str, int] = {}
        for state_name in Bill.objects.filter(
            workflow_type=self.bill_type_model
        ).values_list("current_state__name", flat=True):
            distribution[state_name] = distribution.get(state_name, 0) + 1
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Parliament bills imported."))
        self.stdout.write(f"   Created: {self.created_count}")
        self.stdout.write(f"   Updated: {self.updated_count}")
        self.stdout.write(f"   Unchanged: {self.unchanged_count}")
        if self.folder_skipped:
            self.stdout.write(f"   SharePoint folders: {self.folder_skipped}.")
        else:
            self.stdout.write(
                f"   SharePoint folders: created {self.folder_created_count}, "
                f"relinked {self.folder_relinked_count}, "
                f"failed {len(self.folder_failures)}"
            )
        for number, error in self.folder_failures:
            self.stdout.write(
                self.style.WARNING(f"     {number}: SharePoint filing failed ({error})")
            )
        self.stdout.write("   Bill register by state:")
        for state_name, count in sorted(distribution.items()):
            self.stdout.write(f"     {state_name}: {count}")
        if self.unmatched_committees:
            self.stdout.write(
                "   Sponsors with no matching committee in the system "
                "(committee left empty):"
            )
            for sponsor in sorted(self.unmatched_committees):
                self.stdout.write(f"     {sponsor}")
