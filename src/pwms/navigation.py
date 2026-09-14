"""Navigation context for the site chrome.

The "Create a workflow" menu is data-driven: it lists only the workflow types
the signed-in user is allowed to create (:meth:`WorkflowType.creatable_by`) and
links each one to the view that creates it. Create views are model-specific, so
the type -> view mapping is registered here; a workflow type only appears in the
menu once a create view exists for it.
"""

from __future__ import annotations

from django.urls import reverse

from .models import WorkflowType

#: Workflow type slug -> URL name of the view that creates an instance of it.
#: The seeded slugs are stable (``AutoSlugField`` is populated once, on creation),
#: so renaming a type in the admin does not break its menu entry.
CREATE_URL_NAMES = {
    "delegation-report": "pwms:delegation_report_create",
    "international-resolution": "pwms:international_resolution_create",
    "international-agreement": "pwms:international_agreement_create",
}


def creatable_workflow_types(user):
    """
    Workflow types ``user`` may create, paired with their resolved create URL.

    Types the user cannot create, and types without a registered create view, are
    omitted. Anonymous users get an empty list (they can create nothing).
    """
    entries = []
    for workflow_type in WorkflowType.creatable_by(user):
        url_name = CREATE_URL_NAMES.get(workflow_type.slug)
        if url_name is None:
            continue
        entries.append({"name": workflow_type.name, "url": reverse(url_name)})
    return entries


def navigation(request):
    """Context processor exposing the dynamic navigation menu entries."""
    return {"creatable_workflow_types": creatable_workflow_types(request.user)}
