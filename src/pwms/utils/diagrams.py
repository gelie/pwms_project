"""Locate the state-machine diagram generated for a workflow type.

``manage.py generate_diagrams`` writes one image per enabled ``WorkflowType``
into ``WORKFLOW_DIAGRAM_OUTPUT_DIR`` (see the Management Commands doc). The
detail page's Diagram tab serves that file, so this module owns the one place
that knows how a file is named and where to look — keep it in step with
``generate_diagrams``.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

#: The suffix every diagram file shares.
FILE_SUFFIX = "workflow"


def workflow_type_stem(workflow_type):
    """
    The filename stem ``generate_diagrams`` writes for ``workflow_type``.

    The command lower-cases the type's *name* and underscores its spaces. It is
    deliberately not derived from the slug: the two differ once a type is
    renamed, and the file on disk keeps the name it was rendered with.
    """
    return f"{workflow_type.name.lower().replace(' ', '_')}_{FILE_SUFFIX}"


def workflow_type_diagram_path(workflow_type, *, fmt="svg"):
    """
    Absolute path of the generated diagram, or ``None`` when there is none.

    Every directory in ``WORKFLOW_DIAGRAM_DIRS`` is checked in order, so a
    deployment that ships the images elsewhere (or renders them on demand) still
    finds them.
    """
    filename = f"{workflow_type_stem(workflow_type)}.{fmt}"
    for directory in settings.WORKFLOW_DIAGRAM_DIRS:
        candidate = Path(directory) / filename
        if candidate.is_file():
            return candidate
    return None
