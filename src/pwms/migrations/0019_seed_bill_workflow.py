"""Seed the *Bill* workflow definition.

Creates, idempotently and matched by natural keys, the ``Bill``
:class:`WorkflowType` and the internal legislative lifecycle from the Online
Bill Tracking BRS:

    Introduced → Referred to Committee → Public Participation → Committee
    Deliberation → Committee Report → House Debate and Voting → NCOP
    Consideration → Awaiting Presidential Assent → Signed into Law

with the BRS's backwards/loop paths (NCOP disagreement → mediation → back to
the House; presidential referral back → House reconsideration → assent) and
withdrawal as a terminal outcome before assent.

Every state carries the high-level **public status** it maps to (BRS §12), so
the detailed internal machine can be published as the simplified citizen-facing
statuses: *Introduced*, *Under Parliamentary Consideration*, *National Council
of Provinces*, *Mediation / Reconsideration*, *Awaiting Presidential Assent*,
*Signed into Law* and *Referred Back / Constitutional Review*. The committee
stages all publish as *Under Parliamentary Consideration* while remaining
distinct internally.

Withdrawal transitions require a comment, so the reason is captured in the
transition log alongside the required audit record.

Unlike the international workflow types, no owning group is assumed here: no
organisational unit is identified by the BRS, so an administrator assigns the
owning group and its ``create_roles`` per environment (until then only
superusers may create bills).

Existing objects are left untouched, so re-running against a database where an
administrator has customised the definition is non-destructive. Reverse is
intentionally a no-op: deleting a definition that live instances already follow
would corrupt their state machine.
"""

from django.db import migrations

BILL_TYPE_NAME = "Bill"

COLOR_INITIAL = "#efbd47"
COLOR_ASSIGNED = "#b3995d"
COLOR_PROGRESS = "#5b8f22"
COLOR_ACTIVE = "#275937"
COLOR_TERMINAL = "#16341f"
COLOR_WITHDRAWN = "#8c2f2f"

# (name, slug, public_name, order, is_initial, is_terminal, color)
BILL_STATES = [
    ("Introduced", "introduced", "introduced", 10, True, False, COLOR_INITIAL),
    (
        "Referred to Committee",
        "referred-to-committee",
        "under_consideration",
        20,
        False,
        False,
        COLOR_ASSIGNED,
    ),
    (
        "Public Participation",
        "public-participation",
        "under_consideration",
        30,
        False,
        False,
        COLOR_PROGRESS,
    ),
    (
        "Committee Deliberation",
        "committee-deliberation",
        "under_consideration",
        40,
        False,
        False,
        COLOR_PROGRESS,
    ),
    (
        "Committee Report",
        "committee-report",
        "under_consideration",
        50,
        False,
        False,
        COLOR_PROGRESS,
    ),
    (
        "House Debate and Voting",
        "house-debate-and-voting",
        "under_consideration",
        60,
        False,
        False,
        COLOR_ACTIVE,
    ),
    (
        "NCOP Consideration",
        "ncop-consideration",
        "ncop",
        70,
        False,
        False,
        COLOR_ACTIVE,
    ),
    (
        "Mediation / Reconsideration",
        "mediation-reconsideration",
        "mediation",
        80,
        False,
        False,
        COLOR_ACTIVE,
    ),
    (
        "Awaiting Presidential Assent",
        "awaiting-presidential-assent",
        "awaiting_assent",
        90,
        False,
        False,
        COLOR_ACTIVE,
    ),
    (
        "Referred Back / Constitutional Review",
        "referred-back-constitutional-review",
        "constitutional_review",
        100,
        False,
        False,
        COLOR_ASSIGNED,
    ),
    (
        "Signed into Law",
        "signed-into-law",
        "signed_into_law",
        110,
        False,
        True,
        COLOR_TERMINAL,
    ),
    (
        "Withdrawn",
        "withdrawn",
        "withdrawn",
        120,
        False,
        True,
        COLOR_WITHDRAWN,
    ),
]

# (from_state, to_state, name, slug, order, requires_comment)
BILL_TRANSITIONS = [
    (
        "Introduced",
        "Referred to Committee",
        "Refer to committee",
        "refer-to-committee",
        10,
        False,
    ),
    (
        "Referred to Committee",
        "Public Participation",
        "Open public participation",
        "open-public-participation",
        20,
        False,
    ),
    (
        "Public Participation",
        "Committee Deliberation",
        "Start committee deliberation",
        "start-committee-deliberation",
        30,
        False,
    ),
    (
        "Committee Deliberation",
        "Committee Report",
        "Adopt committee report",
        "adopt-committee-report",
        40,
        False,
    ),
    (
        "Committee Report",
        "House Debate and Voting",
        "Table for House debate",
        "table-for-house-debate",
        50,
        False,
    ),
    (
        "House Debate and Voting",
        "NCOP Consideration",
        "Pass to NCOP",
        "pass-to-ncop",
        60,
        False,
    ),
    (
        "NCOP Consideration",
        "Awaiting Presidential Assent",
        "NCOP agrees – refer for assent",
        "ncop-agrees-refer-for-assent",
        70,
        False,
    ),
    (
        "NCOP Consideration",
        "Mediation / Reconsideration",
        "NCOP disagrees – refer to mediation",
        "ncop-disagrees-refer-to-mediation",
        80,
        False,
    ),
    (
        "Mediation / Reconsideration",
        "House Debate and Voting",
        "Return to House",
        "return-to-house",
        90,
        False,
    ),
    (
        "Mediation / Reconsideration",
        "Awaiting Presidential Assent",
        "Mediation resolved – refer for assent",
        "mediation-resolved-refer-for-assent",
        100,
        False,
    ),
    (
        "Awaiting Presidential Assent",
        "Signed into Law",
        "Assent – signed into law",
        "assent-signed-into-law",
        110,
        False,
    ),
    (
        "Awaiting Presidential Assent",
        "Referred Back / Constitutional Review",
        "Referred back by President",
        "referred-back-by-president",
        120,
        False,
    ),
    (
        "Referred Back / Constitutional Review",
        "House Debate and Voting",
        "Reconsider and amend",
        "reconsider-and-amend",
        130,
        False,
    ),
    (
        "Referred Back / Constitutional Review",
        "Signed into Law",
        "Constitutional concerns resolved – assent",
        "constitutional-concerns-resolved-assent",
        140,
        False,
    ),
    # Withdrawal is possible any time before assent; the reason is mandatory.
    (
        "Introduced",
        "Withdrawn",
        "Withdraw bill",
        "withdraw-bill",
        150,
        True,
    ),
    (
        "Referred to Committee",
        "Withdrawn",
        "Withdraw bill (referred to committee)",
        "withdraw-bill-referred-to-committee",
        160,
        True,
    ),
    (
        "Public Participation",
        "Withdrawn",
        "Withdraw bill (public participation)",
        "withdraw-bill-public-participation",
        170,
        True,
    ),
    (
        "Committee Deliberation",
        "Withdrawn",
        "Withdraw bill (committee deliberation)",
        "withdraw-bill-committee-deliberation",
        180,
        True,
    ),
    (
        "Committee Report",
        "Withdrawn",
        "Withdraw bill (committee report)",
        "withdraw-bill-committee-report",
        190,
        True,
    ),
    (
        "House Debate and Voting",
        "Withdrawn",
        "Withdraw bill (House debate)",
        "withdraw-bill-house-debate",
        200,
        True,
    ),
]

BILL_DESCRIPTION = (
    "Bill lifecycle (Online Bill Tracking BRS): Introduced → Referred to "
    "Committee → Public Participation → Committee Deliberation → Committee "
    "Report → House Debate and Voting → NCOP Consideration → Awaiting "
    "Presidential Assent → Signed into Law, with mediation, presidential "
    "referral back and withdrawal branches. Internal states map to the BRS §12 "
    "public statuses through State.public_name."
)


def _ensure_type(WorkflowType):
    workflow_type, _ = WorkflowType.objects.get_or_create(
        name=BILL_TYPE_NAME,
        defaults={
            "slug": "bill",
            "description": BILL_DESCRIPTION,
            "enabled": True,
        },
    )
    return workflow_type


def _seed_machine(State, Transition, workflow_type):
    states_by_name = {}
    for name, slug, public_name, order, is_initial, is_terminal, color in BILL_STATES:
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

    for from_name, to_name, name, slug, order, requires_comment in BILL_TRANSITIONS:
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


def seed_bill_workflow(apps, schema_editor):
    WorkflowType = apps.get_model("pwms", "WorkflowType")
    State = apps.get_model("pwms", "State")
    Transition = apps.get_model("pwms", "Transition")

    workflow_type = _ensure_type(WorkflowType)
    _seed_machine(State, Transition, workflow_type)


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0018_alter_state_public_name_bill"),
    ]

    operations = [
        migrations.RunPython(seed_bill_workflow, migrations.RunPython.noop),
    ]
