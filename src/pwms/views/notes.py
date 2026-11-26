import logging

from django.core.exceptions import PermissionDenied
from django.http import (
    Http404,
)
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from ..forms import (
    WorkflowNoteForm,
)
from ..models import (
    WorkflowNote,
)
from ..services.permissions import (
    EDIT,
    resolve,
)

logger = logging.getLogger(__name__)

from .common import _record_counters, _target_identifiers, _workflow_target


def _can_change_note(user, note, record):
    """
    Whether ``user`` may edit or delete ``note``.

    A note is its author's to change, and only while they may still edit the
    record it is on. A note whose author's account has since been deleted (the
    FK is nulled with the account) is left to the record's editors, or nothing
    could ever correct or remove it.
    """
    if note.author_id is not None and note.author_id != user.pk:
        return False
    return resolve(user, record, EDIT)


def _notes_context(request, instance):
    """
    Template context for a record's note log and the form that adds to it.

    Notes are rows (:class:`WorkflowNote`), so every instrument can carry them
    and each one keeps its own author. ``note_rows`` pairs each note with whether
    this reader may change it — its author, while they may still edit the record
    (see :func:`_can_change_note`). ``record_notes`` carries the record's own
    ``notes`` column where a type has one — the BRS attribute (BR02.3.13) edited
    with the record — so the panel can show it above the log.
    """
    can_edit = resolve(request.user, instance, EDIT)
    notes = list(instance.note_log().select_related("author"))
    context = {
        **_target_identifiers(instance),
        "note_rows": [
            {
                "note": note,
                "can_change": _can_change_note(request.user, note, instance),
            }
            for note in notes
        ],
        "record_notes": getattr(instance, "notes", ""),
        "can_edit_notes": can_edit,
    }
    if can_edit:
        context["notes_form"] = WorkflowNoteForm()
    return context


def _notes_response(request, target, *, success="", error="", form=None):
    """Re-render the notes panel, announcing a change when there is one."""
    context = _notes_context(request, target)
    context.update(_record_counters(target))
    if form is not None:
        context["notes_form"] = form
    if error:
        context["notes_status"] = {"level": "danger", "message": error}
    elif success:
        context["notes_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/notes_section.html", context)


@require_POST
@require_POST
def workflow_notes_add(request):
    """Record a note against a record (HTMX fragment)."""
    target = _workflow_target(request, EDIT)
    form = WorkflowNoteForm(request.POST)
    if not form.is_valid():
        return _notes_response(request, target, form=form)
    note = form.save(commit=False)
    note.content_object = target
    note.author = request.user
    note.save()
    return _notes_response(request, target, success="Note added.")


def _note_for_change(request, public_id):
    """Load a note and its record, refusing unless the reader may change it."""
    note = get_object_or_404(
        WorkflowNote.objects.select_related("author"), public_id=public_id
    )
    target = note.content_object
    if target is None:
        raise Http404("Note is not linked to a record.")
    if not _can_change_note(request.user, note, target):
        raise PermissionDenied(_("You may not change this note."))
    return note, target


@require_POST
def workflow_note_edit(request, public_id):
    """Rewrite one note (HTMX fragment).

    An empty body is the only way this can fail, and the note itself is left
    untouched, so the refusal is reported as a status message rather than by
    re-rendering the row's own form.
    """
    note, target = _note_for_change(request, public_id)
    form = WorkflowNoteForm(request.POST, instance=note)
    if not form.is_valid():
        return _notes_response(request, target, error="A note cannot be empty.")
    form.save()
    return _notes_response(request, target, success="Note updated.")


@require_POST
def workflow_note_delete(request, public_id):
    """Delete one note (HTMX fragment)."""
    note, target = _note_for_change(request, public_id)
    note.delete()
    return _notes_response(request, target, success="Note deleted.")
