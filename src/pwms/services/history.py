"""One chronological history for a workflow instance.

A workflow's story is written down twice, from two angles:

* ``TransitionLog`` — the *semantic* moves: who took the instrument from one
  state to another, with their comment and IP;
* django-auditlog's ``LogEntry`` — the *CRUD* trail: every create / update /
  delete of the row, with a field-level diff.

Read together they answer one question ("what happened to this record?"), so the
detail page shows them as a single table rather than two. Both are keyed to the
instance by ContentType + internal PK, so merging them is a concatenation and a
sort; ``instance_timeline()`` returns the rows newest first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.db import models

from ..utils.audit_helpers import get_audit_trail_for_instance

#: auditlog action code -> (kind, human label). auditlog's own display values
#: are lower-case ("create"), which reads oddly as a table column.
_AUDIT_ACTIONS = {
    0: ("create", "Created"),
    1: ("update", "Updated"),
    2: ("delete", "Deleted"),
}

#: Shown in the Actor column when nobody is recorded (system/deadline writes).
SYSTEM_ACTOR = "System"

#: auditlog's m2m operation -> the verb used in the timeline.
_M2M_VERBS = {"add": "Added", "remove": "Removed", "clear": "Cleared"}

#: How many rows a detail page renders. The rest stay in the database; the tab
#: says how many were left off rather than paginating a rarely-read history.
TIMELINE_PREVIEW_LIMIT = 50


@dataclass(frozen=True)
class TimelineChange:
    """One field-level change on an audited write."""

    field: str
    before: Any
    after: Any


@dataclass(frozen=True)
class TimelineEntry:
    """One row of the merged history."""

    kind: str  #: transition | create | update | delete
    label: str
    timestamp: Any
    actor: str
    from_state: str = ""
    to_state: str = ""
    action: str = ""
    notes: str = ""
    ip_address: str | None = None
    changes: tuple[TimelineChange, ...] = ()


def instance_timeline(instance, *, limit=None):
    """
    Merge ``TransitionLog`` + auditlog history for ``instance``, newest first.

    ``limit`` caps the rows *after* sorting, so it always keeps the most recent
    ones; ``None`` returns everything.
    """
    entries = [
        _transition_entry(log)
        for log in instance.audit_logs().select_related(
            "from_state", "to_state", "actor"
        )
    ]
    entries.extend(
        _audit_entry(entry, instance)
        for entry in get_audit_trail_for_instance(instance)
    )
    entries.sort(key=lambda entry: entry.timestamp, reverse=True)
    return entries[:limit] if limit is not None else entries


def _transition_entry(log):
    return TimelineEntry(
        kind="transition",
        label="State change",
        timestamp=log.timestamp,
        actor=actor_label(log.actor),
        from_state=log.from_state.name if log.from_state else "",
        to_state=log.to_state.name if log.to_state else "",
        action=log.action,
        notes=log.notes,
        ip_address=log.ip_address,
    )


def _audit_entry(entry, instance):
    kind, label = _AUDIT_ACTIONS.get(entry.action, ("update", "Updated"))
    return TimelineEntry(
        kind=kind,
        label=label,
        timestamp=entry.timestamp,
        actor=actor_label(entry.actor),
        ip_address=entry.remote_addr,
        # Only an update carries a useful diff: a create dumps every field's
        # initial value, and a delete records none.
        changes=_changes(entry, instance) if kind == "update" else (),
    )


def _changes(entry, instance):
    """
    The diff of an audited write, humanised.

    Normal fields go through auditlog's ``changes_display_dict`` (it resolves
    choices, dates and foreign keys). M2M changes are *not* run through it: for
    those auditlog stores a ``{"type": "m2m", ...}`` dict and its display
    helper would render the dict's own keys, so they are summarised here.
    """
    raw = entry.changes or {}
    if not raw:
        return ()
    try:
        displayed = entry.changes_display_dict
    except Exception:  # noqa: BLE001 - a mangled diff must not break the page
        displayed = {}

    changes = []
    for name, value in raw.items():
        field = _field_for(instance, name)
        label = _field_label(field, name)
        if isinstance(value, dict) and value.get("type") == "m2m":
            changes.append(
                TimelineChange(field=label, before="", after=_m2m_summary(value))
            )
            continue
        # auditlog also writes a row per *virtual* field (reverse relations,
        # generic relations, m2m managers). Those hold an empty manager rather
        # than a value, so they are not part of the story.
        if not _is_concrete(field):
            continue
        before, after = _pair(displayed.get(label), value)
        changes.append(TimelineChange(field=label, before=before, after=after))
    return tuple(changes)


def _field_for(instance, name):
    try:
        return instance._meta.get_field(name)
    except FieldDoesNotExist:
        return None


def _is_concrete(field):
    return isinstance(field, models.Field) and field.concrete


def _field_label(field, name):
    """The field's human label, falling back to a tidied field name."""
    label = getattr(field, "verbose_name", None)
    return str(label) if label else str(name).replace("_", " ").capitalize()


def _pair(displayed, raw_value):
    """A ``(before, after)`` pair, preferring auditlog's humanised values."""
    pair = displayed if isinstance(displayed, list) else None
    if pair is None:
        pair = raw_value if isinstance(raw_value, list) else []
    return tuple((list(pair) + [None, None])[:2])


def _m2m_summary(value):
    objects = ", ".join(value.get("objects") or [])
    verb = _M2M_VERBS.get(value.get("operation", ""), "Changed")
    return f"{verb}: {objects}" if objects else verb


def actor_label(user):
    """The label a person carries in a history row ("System" when nobody)."""
    if user is None:
        return SYSTEM_ACTOR
    return getattr(user, "display_name", None) or user.get_username()
