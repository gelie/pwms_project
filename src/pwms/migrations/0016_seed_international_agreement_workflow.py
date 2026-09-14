"""Seed the *International Agreement* workflow definition and its owner.

Creates, idempotently and matched by natural keys, the ``International
Agreement`` :class:`WorkflowType` and the BRS *International Agreements Tracking
and Monitoring* status lifecycle:

    Submitted for tabling → Agreement Tabled – referred to Committee →
    Committee considering and processing → Committee submitted report for
    tabling → House adopted – referred to Department → Closed – House approved

The initial state is *Agreement Tabled – referred to Committee*, matching the
BRS's automatic status for a newly created agreement (BR02). *Submitted for
tabling* is seeded as a preceding, non-initial state; it remains selectable
when editing an instance. The BRS's *Follow up – due date expired* is not a
state here: BR12 models it as the ``Due date expired – pending follow up``
identifier, exposed by ``InternationalAgreement.overdue_identifier``.

Like the other seeded types, the agreement type is owned by group 98 (*IRP: MR:
Man And Gen*, the Multilateral Relations unit) when that group exists. The
assignment is skipped on a fresh or test database, where group 98 only appears
once the Oracle organisational sync has run.

Existing objects are left untouched, so re-running against a database where an
administrator has customised the definition is non-destructive. Reverse is
intentionally a no-op: deleting a definition that live instances already follow
would corrupt their state machine.
"""

from django.db import migrations

AGREEMENT_TYPE_NAME = "International Agreement"

#: Group pk that owns the seeded workflow types (IRP: MR: Man And Gen).
RBAC_GROUP_ID = 98

COLOR_INITIAL = "#efbd47"
COLOR_ASSIGNED = "#b3995d"
COLOR_PROGRESS = "#5b8f22"
COLOR_ACTIVE = "#275937"
COLOR_TERMINAL = "#16341f"

# (name, slug, public_name, order, is_initial, is_terminal, color)
AGREEMENT_STATES = [
    (
        "Submitted for tabling",
        "submitted-for-tabling",
        "new",
        10,
        False,
        False,
        COLOR_INITIAL,
    ),
    (
        "Agreement Tabled – referred to Committee",
        "agreement-tabled-referred-to-committee",
        "referred",
        20,
        True,
        False,
        COLOR_ASSIGNED,
    ),
    (
        "Committee considering and processing",
        "committee-considering-and-processing",
        "in_progress",
        30,
        False,
        False,
        COLOR_PROGRESS,
    ),
    (
        "Committee submitted report for tabling",
        "committee-submitted-report-for-tabling",
        "in_progress",
        40,
        False,
        False,
        COLOR_PROGRESS,
    ),
    (
        "House adopted – referred to Department",
        "house-adopted-referred-to-department",
        "implemented",
        50,
        False,
        False,
        COLOR_ACTIVE,
    ),
    (
        "Closed – House approved",
        "closed-house-approved",
        "closed",
        60,
        False,
        True,
        COLOR_TERMINAL,
    ),
]

# (from_state, to_state, name, slug, order, requires_comment)
AGREEMENT_TRANSITIONS = [
    (
        "Submitted for tabling",
        "Agreement Tabled – referred to Committee",
        "Table and refer to Committee",
        "table-and-refer-to-committee",
        10,
        False,
    ),
    (
        "Agreement Tabled – referred to Committee",
        "Committee considering and processing",
        "Start committee consideration",
        "start-committee-consideration",
        20,
        False,
    ),
    (
        "Committee considering and processing",
        "Committee submitted report for tabling",
        "Committee submitted report for tabling",
        "committee-submitted-report-for-tabling",
        30,
        False,
    ),
    (
        "Committee submitted report for tabling",
        "House adopted – referred to Department",
        "House adopted – referred to Department",
        "house-adopted-referred-to-department",
        40,
        False,
    ),
    (
        "House adopted – referred to Department",
        "Closed – House approved",
        "Close – House approved",
        "close-house-approved",
        50,
        False,
    ),
]

AGREEMENT_DESCRIPTION = (
    "International agreement lifecycle (BRS International Agreements Tracking "
    "and Monitoring): Submitted for tabling → Agreement Tabled – referred to "
    "Committee → Committee considering and processing → Committee submitted "
    "report for tabling → House adopted – referred to Department → Closed – "
    "House approved."
)


def _ensure_type(WorkflowType):
    workflow_type, created = WorkflowType.objects.get_or_create(
        name=AGREEMENT_TYPE_NAME,
        defaults={
            "slug": "international-agreement",
            "description": AGREEMENT_DESCRIPTION,
            "enabled": True,
        },
    )
    return workflow_type, created


def _seed_machine(State, Transition, workflow_type):
    states_by_name = {}
    for (
        name,
        slug,
        public_name,
        order,
        is_initial,
        is_terminal,
        color,
    ) in AGREEMENT_STATES:
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

    for (
        from_name,
        to_name,
        name,
        slug,
        order,
        requires_comment,
    ) in AGREEMENT_TRANSITIONS:
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


def seed_international_agreement(apps, schema_editor):
    Group = apps.get_model("pwms", "Group")
    WorkflowType = apps.get_model("pwms", "WorkflowType")
    State = apps.get_model("pwms", "State")
    Transition = apps.get_model("pwms", "Transition")

    workflow_type, _ = _ensure_type(WorkflowType)
    _seed_machine(State, Transition, workflow_type)

    # Own the type with the International Relations unit that also owns the
    # Delegation Report and International Resolution types (group 98).
    if (
        workflow_type.group_id is None
        and Group.objects.filter(pk=RBAC_GROUP_ID).exists()
    ):
        workflow_type.group_id = RBAC_GROUP_ID
        workflow_type.save(update_fields=["group", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0015_internationalagreement"),
    ]

    operations = [
        migrations.RunPython(seed_international_agreement, migrations.RunPython.noop),
    ]
