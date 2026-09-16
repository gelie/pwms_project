"""One workflow instance as a formal, filed-style document.

The register report answers *"what is on the books?"*. This module answers
*"what does this one say?"* — the document an official would print, attach to a
submission or forward about a single delegation report, international
resolution, agreement or bill.

Everything a type has to say about itself is shaped here into three generic
collections:

* **sections** — label/value facts ("At a glance", then the type's own fields);
* **notes** — the free text (description, resolution text, notes);
* **tables** — the lists (participants, BR-03 updates, bill versions, related
  instruments, referrals, documents, state changes, domain events).

Splitting the knowledge that way keeps ``pdf/instrument_export.html`` generic —
it never has to learn about engagement dates or B-numbers — and keeps every
per-instrument rule next to the models it reads. The layout itself (CSS,
masthead, footer) is shared with the register report through
``pdf/_formal_base.html``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import slugify

from ..models import (
    Bill,
    DelegationReport,
    InternationalAgreement,
    InternationalResolution,
)
from .exports import CONTENT_TYPES, logo_data_uri

#: Formats a single instrument document is offered in.
INSTRUMENT_FORMATS = ("pdf", "html")

TEMPLATE = "pwms/pdf/instrument_export.html"


# -- small helpers ----------------------------------------------------------
def _stamp(value):
    """A date or datetime as a printable stamp (``""`` for nothing)."""
    if isinstance(value, dt.datetime):
        return timezone.localtime(value).strftime("%d %b %Y %H:%M")
    if isinstance(value, dt.date):
        return value.strftime("%d %b %Y")
    return "" if value is None else str(value)


def _day(value):
    """A date or datetime as an ISO day (``""`` for nothing)."""
    if isinstance(value, dt.datetime):
        return timezone.localtime(value).strftime("%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    return "" if value is None else str(value)


def _cell(text, css=""):
    """One table cell: the text plus an optional CSS class."""
    return {"text": "" if text is None else str(text), "class": css}


def _field(label, value, css=""):
    """One label/value fact."""
    return {
        "label": label,
        "value": "" if value is None else str(value),
        "class": css,
    }


def _name(user):
    """A user's display label, or ``""`` for nobody."""
    return user.display_name if user is not None else ""


def _related(item):
    """A parent/child instrument as a row, flagging an overdue one."""
    overdue = item.is_overdue
    return [
        _cell(item.identifier or "—"),
        _cell(item.title),
        _cell(item.workflow_type.name),
        _cell(item.current_state.name),
        _cell(_day(item.deadline) or "—", "overdue" if overdue else ""),
        _cell("Yes" if overdue else "No", "overdue" if overdue else ""),
    ]


#: Headers shared by the parent and related-instrument tables.
RELATED_HEADERS = ["Reference", "Title", "Type", "State", "Deadline", "Overdue"]


# -- facts ------------------------------------------------------------------
def _common_facts(instance, children):
    """The facts every instrument carries, whatever its type."""
    state = instance.current_state
    group = instance.workflow_type.group
    overdue = instance.is_overdue
    facts = [
        _field("Reference", instance.identifier or "—"),
        _field("Title", instance.title),
        _field("Workflow type", instance.workflow_type.name),
        _field("Current state", state.name),
        _field("Public status", state.get_public_name_display()),
        _field("Priority", instance.get_priority_display()),
        _field("Owner", _name(instance.owner)),
        _field("Assigned to", _name(instance.assigned_to) or "—"),
        _field("Group", group.name if group is not None else "—"),
        _field(
            "Deadline",
            _stamp(instance.deadline) or "—",
            "overdue" if overdue else "",
        ),
        _field("Overdue", "Yes" if overdue else "No", "overdue" if overdue else ""),
        _field("Closed", "Yes" if state.is_terminal else "No"),
        _field("Created", _stamp(instance.created_at)),
        _field("Last updated", _stamp(instance.updated_at)),
    ]
    if children:
        closed = sum(1 for child in children if child.current_state.is_terminal)
        facts.append(
            _field(
                "Sub-instrument completion",
                f"{closed} of {len(children)} closed "
                f"({round(closed * 100 / len(children))}%)",
            )
        )
    return facts


def _delegation_sections(instance):
    latest = instance.latest_update
    fields = [
        _field("Engagement", instance.engagement_name or "—"),
        _field("Start date", _day(instance.engagement_start_date) or "—"),
        _field("End date", _day(instance.engagement_end_date) or "—"),
        _field(
            "Country",
            str(instance.location_country) if instance.location_country else "—",
        ),
        _field("City", str(instance.location_city) if instance.location_city else "—"),
        _field("Report document", instance.report_document_url or "—"),
    ]
    if latest is not None:
        fields.extend(
            [
                _field("Latest ATC reference", latest.atc_reference or "—"),
                _field(
                    "Latest ATC publication", _day(latest.atc_publication_date) or "—"
                ),
                _field("Latest ATC page", latest.atc_page_number or "—"),
                _field("Latest ATC document", latest.atc_document_url or "—"),
            ]
        )
    return [{"title": "Engagement", "fields": fields}]


def _resolution_sections(instance):
    return [
        {
            "title": "Resolution",
            "fields": [
                _field("Resolution number", instance.resolution_number),
                _field("Adoption date", _day(instance.adoption_date) or "—"),
                _field(
                    "Responsible group",
                    str(instance.responsible_group)
                    if instance.responsible_group
                    else "—",
                ),
            ],
        }
    ]


def _agreement_sections(instance):
    minister = instance.responsible_minister_display
    committees = ", ".join(
        committee.name for committee in instance.referral_committees.all()
    )
    return [
        {
            "title": "Agreement",
            "fields": [
                _field("Agreement type", instance.get_agreement_type_display() or "—"),
                _field("Submitting department", instance.submitting_department or "—"),
                _field("Responsible minister", str(minister) if minister else "—"),
                _field("ATC tabling date", _day(instance.atc_tabling_date) or "—"),
                _field("ATC reference", instance.atc_reference or "—"),
                _field("Referral committees", committees or "—"),
                _field("Agreement document", instance.agreement_document_url or "—"),
                _field(
                    "Explanatory memorandum",
                    instance.explanatory_memorandum_url or "—",
                ),
            ],
        }
    ]


def _bill_sections(instance):
    sponsor = instance.sponsor_display
    committee = instance.responsible_committee
    return [
        {
            "title": "Bill profile",
            "fields": [
                _field("Bill number", instance.bill_number),
                _field("Short title", instance.short_title or "—"),
                _field("Bill type", instance.get_bill_type_display() or "—"),
                _field(
                    "House of origin", instance.get_house_of_origin_display() or "—"
                ),
                _field("Sponsor", str(sponsor) if sponsor else "—"),
                _field("Introduced", _day(instance.introduced_date) or "—"),
                _field("Responsible committee", str(committee) if committee else "—"),
                _field("Current version", instance.current_version or "—"),
            ],
        },
        {
            "title": "References",
            "fields": [
                _field("ATC reference", instance.atc_reference or "—"),
                _field("Order paper reference", instance.order_paper_reference or "—"),
                _field("Bill document", instance.bill_document_url or "—"),
            ],
        },
    ]


def _delegation_notes(instance):
    return [("Notes / follow-up", instance.notes)]


def _resolution_notes(instance):
    return [
        ("Resolution text", instance.resolution_text),
        ("Implementation progress", instance.implementation_progress),
    ]


def _agreement_notes(instance):
    return [("Notes / follow-up", instance.notes)]


def _bill_notes(instance):
    return [("Notes / sub-events", instance.notes)]


_TYPE_SECTIONS = {
    DelegationReport: _delegation_sections,
    InternationalResolution: _resolution_sections,
    InternationalAgreement: _agreement_sections,
    Bill: _bill_sections,
}

_TYPE_NOTES = {
    DelegationReport: _delegation_notes,
    InternationalResolution: _resolution_notes,
    InternationalAgreement: _agreement_notes,
    Bill: _bill_notes,
}


# -- tables -----------------------------------------------------------------
def _delegation_tables(instance):
    # Only who is on the delegation now: a removed participant is kept on the
    # report as history, but is no longer part of the delegation.
    participants = instance.participants.filter(removed_at__isnull=True).select_related(
        "user"
    )
    updates = instance.updates.select_related("resulting_state", "recorded_by")
    return [
        {
            "title": f"Delegation participants ({participants.count()})",
            "headers": ["Name", "Role", "Kind", "PWMS account"],
            "rows": [
                [
                    _cell(participant.full_name),
                    _cell(participant.delegation_role or "—"),
                    _cell(participant.get_participant_type_display()),
                    _cell(_name(participant.user) or "—"),
                ]
                for participant in participants
            ],
            "empty": "No participants recorded.",
        },
        {
            "title": f"BR03 updates ({updates.count()})",
            "headers": [
                "Date",
                "Resulting status",
                "ATC reference",
                "ATC date",
                "Page",
                "Recorded by",
                "Notes",
            ],
            "rows": [
                [
                    _cell(_day(update.update_date)),
                    _cell(
                        update.resulting_state.name if update.resulting_state else "—"
                    ),
                    _cell(update.atc_reference or "—"),
                    _cell(_day(update.atc_publication_date) or "—"),
                    _cell(update.atc_page_number or "—"),
                    _cell(_name(update.recorded_by) or "—"),
                    _cell(update.notes or "—"),
                ]
                for update in updates
            ],
            "empty": "No updates recorded.",
        },
    ]


def _resolution_tables(instance):
    return []


def _agreement_tables(instance):
    return []


def _bill_tables(instance):
    versions = instance.versions.select_related("recorded_by")
    return [
        {
            "title": f"Bill versions ({versions.count()})",
            "headers": [
                "Label",
                "Kind",
                "Date",
                "Current",
                "Recorded by",
                "Notes",
            ],
            "rows": [
                [
                    _cell(version.version_label),
                    _cell(version.get_version_type_display()),
                    _cell(_day(version.version_date)),
                    _cell("Yes" if version.is_current else "No"),
                    _cell(_name(version.recorded_by) or "—"),
                    _cell(version.notes or "—"),
                ]
                for version in versions
            ],
            "empty": "No versions recorded.",
        }
    ]


_TYPE_TABLES = {
    DelegationReport: _delegation_tables,
    InternationalResolution: _resolution_tables,
    InternationalAgreement: _agreement_tables,
    Bill: _bill_tables,
}


def _hierarchy_tables(parent, children):
    """The parent instrument (if any) and the related instruments beneath this one."""
    tables = []
    if parent is not None:
        tables.append(
            {
                "title": "Parent instrument",
                "headers": RELATED_HEADERS,
                "rows": [_related(parent)],
                "empty": "",
            }
        )
    if children:
        tables.append(
            {
                "title": f"Related instruments ({len(children)})",
                "headers": RELATED_HEADERS,
                "rows": [_related(child) for child in children],
                "empty": "",
            }
        )
    return tables


def _referrals_table(instance):
    referrals = instance.referrals().select_related("referred_to", "referred_by")
    return {
        "title": f"Referrals ({referrals.count()})",
        "headers": [
            "Referred to",
            "Referred by",
            "Raised",
            "Due",
            "Status",
            "Overdue",
        ],
        "rows": [
            [
                _cell(referral.referred_to.name),
                _cell(_name(referral.referred_by) or "—"),
                _cell(_day(referral.referred_at)),
                _cell(_day(referral.due_date) or "—"),
                _cell(referral.get_status_display()),
                _cell(
                    "Yes" if referral.is_overdue else "No",
                    "overdue" if referral.is_overdue else "",
                ),
            ]
            for referral in referrals
        ],
        "empty": "No referrals recorded.",
    }


def _documents_table(instance):
    # ``prefetch_related`` makes the per-attachment version count free.
    attachments = list(instance.attachments.prefetch_related("versions"))
    return {
        "title": f"Documents ({len(attachments)})",
        "headers": ["Document", "Type", "Folder", "Added", "Versions", "Link"],
        "rows": [
            [
                _cell(attachment.name),
                _cell(attachment.get_type_display()),
                _cell(attachment.sharepoint_folder_path or "—"),
                _cell(_day(attachment.created_at)),
                _cell(len(attachment.versions.all())),
                _cell(attachment.get_sharepoint_url() or "—"),
            ]
            for attachment in attachments
        ],
        "empty": "No documents attached.",
    }


def _transitions_table(instance):
    logs = instance.audit_logs().select_related("from_state", "to_state", "actor")
    return {
        "title": "State changes",
        "headers": ["When", "From", "To", "Action", "Actor", "Note"],
        "rows": [
            [
                _cell(_day(log.timestamp) + " " + _time(log.timestamp)),
                _cell(log.from_state.name if log.from_state else "—"),
                _cell(log.to_state.name if log.to_state else "—"),
                _cell(log.action),
                _cell(_name(log.actor) or "—"),
                _cell(log.notes or "—"),
            ]
            for log in logs
        ],
        "empty": "No state changes recorded.",
    }


def _events_table(instance):
    events = instance.events()
    return {
        "title": "Domain events",
        "headers": ["When", "Event", "Origin", "Actor", "Document", "Note"],
        "rows": [
            [
                _cell(_stamp(event.occurred_at)),
                _cell(event.event_type.name),
                _cell(event.get_origin_display()),
                _cell(_name(event.actor) or "PWMS (automatic)"),
                _cell(event.document_url or "—"),
                _cell(event.notes or "—"),
            ]
            for event in events
        ],
        "empty": "No domain events recorded.",
    }


def _time(value):
    """The time-of-day part of a datetime (``""`` otherwise)."""
    if isinstance(value, dt.datetime):
        return timezone.localtime(value).strftime("%H:%M")
    return ""


# -- the document -----------------------------------------------------------
@dataclass
class InstrumentDocument:
    """A single instrument shaped for the formal document template."""

    instance: object
    document_title: str
    document_subtitle: str
    reference: str
    state: str
    priority: str
    generated_on: dt.datetime
    generated_by: object = None
    generated_by_label: str = ""
    sections: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    tables: list = field(default_factory=list)


def build_instrument_document(instance, user=None, *, now=None):
    """Shape ``instance`` into a :class:`InstrumentDocument`."""
    now = now or timezone.now()
    children = instance.sub_workflows
    parent = instance.parent_workflow

    sections = [{"title": "At a glance", "fields": _common_facts(instance, children)}]
    sections.extend(_TYPE_SECTIONS[type(instance)](instance))

    notes = []
    if instance.description:
        notes.append({"title": "Description", "text": instance.description})
    for title, text in _TYPE_NOTES[type(instance)](instance):
        if text:
            notes.append({"title": title, "text": text})

    tables = []
    tables.extend(_hierarchy_tables(parent, children))
    tables.extend(_TYPE_TABLES[type(instance)](instance))
    tables.append(_referrals_table(instance))
    tables.append(_documents_table(instance))
    tables.append(_transitions_table(instance))
    tables.append(_events_table(instance))

    return InstrumentDocument(
        instance=instance,
        document_title=instance.title or str(instance),
        document_subtitle=_subtitle(instance),
        reference=instance.identifier or "",
        state=instance.current_state.name,
        priority=instance.get_priority_display(),
        generated_on=now,
        generated_by=user,
        generated_by_label=getattr(user, "display_name", "") if user else "—",
        sections=sections,
        notes=notes,
        tables=tables,
    )


def _subtitle(instance):
    """The identifying line under the title: type, reference, and any overdue flag."""
    parts = [instance.workflow_type.name]
    if instance.identifier:
        parts.append(instance.identifier)
    if instance.is_overdue:
        parts.append("OVERDUE")
    return " · ".join(parts)


def suggested_instrument_filename(instance, extension):
    """A stable, descriptive filename (``dr-2026-0001-delegation-report-2026-09-15.pdf``)."""
    stem = (
        slugify(
            " ".join(filter(None, [instance.identifier, instance._meta.verbose_name]))
        )
        or "instrument"
    )
    stamp = timezone.localtime(timezone.now()).strftime("%Y-%m-%d")
    return f"{stem}-{stamp}.{extension}"


def render_instrument_html(document):
    """The formal document as a standalone HTML string."""
    # No request: the document is self-contained (inline CSS, embedded logo) and
    # must render identically from a share, a cron job or a worker.
    context = {
        "document": document,
        "document_title": document.document_title,
        "document_subtitle": document.document_subtitle,
        "generated_on": document.generated_on,
        "generated_by": document.generated_by,
        "generated_by_label": document.generated_by_label,
        "logo_url": logo_data_uri(),
    }
    return render_to_string(TEMPLATE, context)


def instrument_content(instance, user=None, export_format="pdf"):
    """``(filename, content, media type)`` for one instrument document."""
    export_format = (export_format or "pdf").lower()
    if export_format not in INSTRUMENT_FORMATS:
        raise ValueError(f"Unknown document format: {export_format!r}")

    document = build_instrument_document(instance, user)
    html = render_instrument_html(document)

    if export_format == "html":
        return (
            suggested_instrument_filename(instance, "html"),
            html.encode("utf-8"),
            CONTENT_TYPES["html"],
        )

    # Imported here: WeasyPrint drags in cairo/pango and is not needed to render
    # the page itself.
    from weasyprint import HTML

    return (
        suggested_instrument_filename(instance, "pdf"),
        HTML(string=html).write_pdf(),
        CONTENT_TYPES["pdf"],
    )


def response_for_instrument(instance, user=None, export_format="pdf"):
    """An attachment response holding ``instance`` as a formal document."""
    filename, content, media_type = instrument_content(instance, user, export_format)
    response = HttpResponse(content, content_type=media_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
