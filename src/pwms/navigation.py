"""Navigation context for the site chrome.

The "Create a workflow" menu is data-driven: it lists only the workflow types
the signed-in user is allowed to create (:meth:`WorkflowType.creatable_by`) and
links each one to the view that creates it.

A type is matched to its create view by **name**, never by its auto-generated
``slug``. The form that backs each create view already pins its type by name
(``WorkflowInstanceFormMixin.initial_workflow_type``), and the create view gates
on that same value (``views._creatable_form_type``), so the menu and the view
agree by construction. The slug cannot play that role: ``AutoSlugField`` writes
it once, on creation, so a type that is renamed — or re-created while an older
row still holds the slug — keeps the old value, and a slug-keyed menu silently
dropped the new type. Register a creatable type in :data:`CREATE_VIEWS`; its
name is read straight from the form so the two can never drift.
"""

from __future__ import annotations

from django.urls import reverse

from .forms import (
    BillForm,
    DelegationReportForm,
    InternationalAgreementForm,
    InternationalResolutionForm,
)
from .models import WorkflowType

#: ``(form, create-view URL name)`` for every workflow type creatable from the
#: site. The type name is taken from the form's ``initial_workflow_type`` — the
#: same value the create view gates on — rather than repeated here, so the two
#: cannot drift out of step.
CREATE_VIEWS = (
    (DelegationReportForm, "pwms:delegation_report_create"),
    (InternationalResolutionForm, "pwms:international_resolution_create"),
    (InternationalAgreementForm, "pwms:international_agreement_create"),
    (BillForm, "pwms:bill_create"),
)

#: Workflow type **name** -> URL name of the view that creates an instance of it.
CREATE_URL_NAMES = {
    form.initial_workflow_type: url_name for form, url_name in CREATE_VIEWS
}


def create_url_name(workflow_type):
    """
    URL name of the create view for ``workflow_type``, or ``None`` if unregistered.

    Keyed on the type's name (not its ``slug``) so a type whose slug diverged —
    renamed, or created while an older row still held the slug — stays reachable.
    """
    return CREATE_URL_NAMES.get(workflow_type.name)


def creatable_workflow_types(user):
    """
    Workflow types ``user`` may create, paired with their resolved create URL.

    Types the user cannot create, and types without a registered create view, are
    omitted. Anonymous users get an empty list (they can create nothing).
    """
    entries = []
    for workflow_type in WorkflowType.creatable_by(user):
        url_name = create_url_name(workflow_type)
        if url_name is None:
            continue
        entries.append({"name": workflow_type.name, "url": reverse(url_name)})
    return entries


def navigation(request):
    """Context processor exposing the dynamic navigation menu entries."""
    return {"creatable_workflow_types": creatable_workflow_types(request.user)}
