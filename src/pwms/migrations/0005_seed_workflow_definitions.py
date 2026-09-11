"""
Seed the BRS workflow definitions.

Creates, idempotently and matched by natural keys:

* ``Delegation Report`` — the four BRS BR02.8 statuses
  (*Awaiting PGIR approval → Submitted for tabling → Tabled and referred to
  Committee → Closed – House approved*) and the transition between each step;
* ``International Resolution`` — the implementation-tracking lifecycle
  (*Captured → Assigned → In Progress → Implemented → Closed*).

The International Resolution type declares the Delegation Report type as its
parent type, which is what lets a ``DelegationReport`` contain
``InternationalResolution`` instances (and only those) through
``WorkflowRelationship``.

Existing objects are left untouched, so re-running against a database where an
administrator has customised these definitions is non-destructive.

Reverse is intentionally a no-op: deleting definitions that live workflow
instances may already follow would corrupt their state machines.
"""

from django.db import migrations

REPORT_TYPE_NAME = "Delegation Report"
RESOLUTION_TYPE_NAME = "International Resolution"

COLOR_INITIAL = "#efbd47"
COLOR_ASSIGNED = "#b3995d"
COLOR_PROGRESS = "#5b8f22"
COLOR_ACTIVE = "#275937"
COLOR_TERMINAL = "#16341f"

# (name, slug, public_name, order, is_initial, is_terminal, color)
REPORT_STATES = [
    (
        "Awaiting PGIR approval",
        "awaiting-pgir-approval",
        "new",
        10,
        True,
        False,
        COLOR_INITIAL,
    ),
    (
        "Submitted for tabling",
        "submitted-for-tabling",
        "in_progress",
        20,
        False,
        False,
        COLOR_PROGRESS,
    ),
    (
        "Tabled and referred to Committee",
        "tabled-and-referred-to-committee",
        "referred",
        30,
        False,
        False,
        COLOR_ACTIVE,
    ),
    (
        "Closed – House approved",
        "closed-house-approved",
        "closed",
        40,
        False,
        True,
        COLOR_TERMINAL,
    ),
]

# (from_state, to_state, name, slug, order, requires_comment)
REPORT_TRANSITIONS = [
    (
        "Awaiting PGIR approval",
        "Submitted for tabling",
        "Submit for tabling",
        "submit-for-tabling",
        10,
        False,
    ),
    (
        "Submitted for tabling",
        "Tabled and referred to Committee",
        "Table and refer to Committee",
        "table-and-refer-to-committee",
        20,
        False,
    ),
    (
        "Tabled and referred to Committee",
        "Closed – House approved",
        "Close – House approved",
        "close-house-approved",
        30,
        False,
    ),
]

RESOLUTION_STATES = [
    ("Captured", "captured", "new", 10, True, False, COLOR_INITIAL),
    ("Assigned", "assigned", "in_progress", 20, False, False, COLOR_ASSIGNED),
    ("In Progress", "in-progress", "in_progress", 30, False, False, COLOR_PROGRESS),
    ("Implemented", "implemented", "implemented", 40, False, False, COLOR_ACTIVE),
    ("Closed", "closed", "closed", 50, False, True, COLOR_TERMINAL),
]

RESOLUTION_TRANSITIONS = [
    (
        "Captured",
        "Assigned",
        "Assign for implementation",
        "assign-for-implementation",
        10,
        False,
    ),
    (
        "Assigned",
        "In Progress",
        "Start implementation",
        "start-implementation",
        20,
        False,
    ),
    (
        "In Progress",
        "Implemented",
        "Report implementation",
        "report-implementation",
        30,
        False,
    ),
    ("Implemented", "Closed", "Close", "close", 40, False),
]


def _ensure_type(WorkflowType, *, name, slug, description, parent_type=None):
    workflow_type, created = WorkflowType.objects.get_or_create(
        name=name,
        defaults={
            "slug": slug,
            "description": description,
            "enabled": True,
            "parent_type": parent_type,
        },
    )
    # Pre-existing type: only fill in a missing parent link, never overwrite.
    if not created and parent_type is not None and workflow_type.parent_type_id is None:
        workflow_type.parent_type = parent_type
        workflow_type.save(update_fields=["parent_type", "updated_at"])
    return workflow_type


def _seed_machine(State, Transition, workflow_type, states, transitions):
    states_by_name = {}
    for name, slug, public_name, order, is_initial, is_terminal, color in states:
        state, _ = State.objects.get_or_create(
            workflow_type=workflow_type,
            name=name,
            defaults={
                "slug": slug,
                "public_name": public_name,
                "order": order,
                "is_initial": is_initial,
                "is_terminal": is_terminal,
                "color": color,
            },
        )
        states_by_name[name] = state

    for from_name, to_name, name, slug, order, requires_comment in transitions:
        Transition.objects.get_or_create(
            workflow_type=workflow_type,
            from_state=states_by_name[from_name],
            to_state=states_by_name[to_name],
            defaults={
                "name": name,
                "slug": slug,
                "order": order,
                "requires_comment": requires_comment,
            },
        )


def seed_workflow_definitions(apps, schema_editor):
    WorkflowType = apps.get_model("pwms", "WorkflowType")
    State = apps.get_model("pwms", "State")
    Transition = apps.get_model("pwms", "Transition")

    report_type = _ensure_type(
        WorkflowType,
        name=REPORT_TYPE_NAME,
        slug="delegation-report",
        description=(
            "Delegation report lifecycle (BRS BR02): Awaiting PGIR approval → "
            "Submitted for tabling → Tabled and referred to Committee → "
            "Closed – House approved."
        ),
    )
    resolution_type = _ensure_type(
        WorkflowType,
        name=RESOLUTION_TYPE_NAME,
        slug="international-resolution",
        description=(
            "International resolution implementation tracking: Captured → "
            "Assigned → In Progress → Implemented → Closed."
        ),
        parent_type=report_type,
    )

    _seed_machine(State, Transition, report_type, REPORT_STATES, REPORT_TRANSITIONS)
    _seed_machine(
        State, Transition, resolution_type, RESOLUTION_STATES, RESOLUTION_TRANSITIONS
    )


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0004_alter_delegationreport_options_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_workflow_definitions, migrations.RunPython.noop),
    ]
