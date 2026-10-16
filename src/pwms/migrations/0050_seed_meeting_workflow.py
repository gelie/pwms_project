"""Seed the *Meeting* (Events) workflow and its RBAC configuration.

Creates, idempotently and matched by natural keys:

* the ``Meeting`` :class:`WorkflowType` and the parliamentary meeting lifecycle
  —
  *Scheduled → In Progress → Completed*, with *Postponed* and *Cancelled*
  branches (both requiring a comment, so the reason is captured in the
  transition log);
* the three ``EventType`` rows the meeting workflow appends to a meeting's
  timeline (attendance recorded, minutes filed, minutes approved);
* the RBAC the brief asks for: the group ``ICT: IT Systems And Application
  Support Unit`` owns the type and the role ``ECM Analyst Programmer`` may
  create its meetings.

As migrations ``0012`` / ``0016`` / ``0046`` do for the IRPD types, the group
and role are **not** created here — they belong to the Oracle organisational
sync — and the ownership/create-role assignment is skipped when either is
absent, so a fresh or test database still gets the workflow machine. A group and
role that *do* exist (the development environment, where the sync has run) are
wired up. ``manage.py seed_demo_data`` creates them for a scratch database.

Existing objects are left untouched (re-running is non-destructive) and reverse
removes exactly what this migration added.
"""

from django.db import migrations

MEETING_TYPE_NAME = "Meeting"
MEETING_TYPE_SLUG = "meeting"

#: Owning group / create role named in the development brief.
RBAC_GROUP_NAME = "ICT: IT Systems And Application Support Unit"
RBAC_ROLE_NAME = "ECM Analyst Programmer"

COLOR_INITIAL = "#efbd47"
COLOR_PROGRESS = "#5b8f22"
COLOR_TERMINAL = "#16341f"
COLOR_POSTPONED = "#b3995d"
COLOR_CANCELLED = "#8c2f2f"

# (name, slug, public_name, order, is_initial, is_terminal, color)
MEETING_STATES = [
    ("Scheduled", "scheduled", "scheduled", 10, True, False, COLOR_INITIAL),
    ("In Progress", "in-progress", "in_progress", 20, False, False, COLOR_PROGRESS),
    ("Completed", "completed", "closed", 30, False, True, COLOR_TERMINAL),
    ("Postponed", "postponed", "postponed", 40, False, False, COLOR_POSTPONED),
    ("Cancelled", "cancelled", "cancelled", 50, False, True, COLOR_CANCELLED),
]

# (from_state, to_state, name, slug, order, requires_comment)
MEETING_TRANSITIONS = [
    ("Scheduled", "In Progress", "Start meeting", "start-meeting", 10, False),
    ("In Progress", "Completed", "Complete meeting", "complete-meeting", 20, False),
    ("Scheduled", "Postponed", "Postpone meeting", "postpone-meeting", 30, True),
    ("Postponed", "Scheduled", "Reschedule meeting", "reschedule-meeting", 40, False),
    ("Scheduled", "Cancelled", "Cancel meeting", "cancel-meeting", 50, True),
    (
        "In Progress",
        "Cancelled",
        "Cancel meeting (in progress)",
        "cancel-meeting-in-progress",
        60,
        True,
    ),
    (
        "Postponed",
        "Cancelled",
        "Cancel meeting (postponed)",
        "cancel-meeting-postponed",
        70,
        True,
    ),
]

MEETING_DESCRIPTION = (
    "Parliamentary meeting lifecycle (Events workflow): Scheduled → In Progress "
    "→ Completed, with Postponed and Cancelled branches. Meetings cover House "
    "Plenary, committee, administration and other sitting types; agenda and "
    "minutes are attached documents and attendance is recorded against a "
    "per-meeting register."
)

# (name, slug, description)
MEETING_EVENT_TYPES = [
    (
        "Meeting attendance recorded",
        "meeting-attendance-recorded",
        "Attendance was recorded for an attendee, during or after the meeting.",
    ),
    (
        "Meeting minutes filed",
        "meeting-minutes-filed",
        "The minutes of the meeting were filed.",
    ),
    (
        "Meeting minutes approved",
        "meeting-minutes-approved",
        "The minutes of the meeting were approved.",
    ),
]


def _ensure_type(WorkflowType):
    workflow_type, _ = WorkflowType.objects.get_or_create(
        name=MEETING_TYPE_NAME,
        defaults={
            "slug": MEETING_TYPE_SLUG,
            "description": MEETING_DESCRIPTION,
            "enabled": True,
        },
    )
    return workflow_type


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
    ) in MEETING_STATES:
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

    for from_name, to_name, name, slug, order, requires_comment in MEETING_TRANSITIONS:
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


def _seed_event_types(EventType):
    for name, slug, description in MEETING_EVENT_TYPES:
        EventType.objects.get_or_create(
            slug=slug,
            defaults={
                "name": name,
                "description": description,
                "is_system": True,
            },
        )


def _assign_rbac(apps, workflow_type):
    """Point the type at the named group and give it the named create role.

    Only existing rows are used (the sync owns them); each half is applied
    independently and only when nothing has been set, so an administrator's
    configuration is never overwritten.
    """
    Group = apps.get_model("pwms", "Group")
    Role = apps.get_model("pwms", "Role")

    group = Group.objects.filter(name=RBAC_GROUP_NAME).first()
    if group is not None and workflow_type.group_id is None:
        workflow_type.group_id = group.pk
        workflow_type.save(update_fields=["group", "updated_at"])

    # A create role must come from the type's group (the admin enforces it), so
    # the role is only attached once the group is.
    role = Role.objects.filter(name=RBAC_ROLE_NAME).first()
    if (
        role is not None
        and workflow_type.group_id is not None
        and not workflow_type.create_roles.exists()
    ):
        workflow_type.create_roles.add(role.pk)


def seed_meeting_workflow(apps, schema_editor):
    WorkflowType = apps.get_model("pwms", "WorkflowType")
    State = apps.get_model("pwms", "State")
    Transition = apps.get_model("pwms", "Transition")
    EventType = apps.get_model("pwms", "EventType")

    workflow_type = _ensure_type(WorkflowType)
    _seed_machine(State, Transition, workflow_type)
    _seed_event_types(EventType)
    _assign_rbac(apps, workflow_type)


def unseed_meeting_workflow(apps, schema_editor):
    """Reverse: drop the meeting workflow and the event types it introduced.

    The owning group and create role are left in place — they are shared
    organisational data a membership may reference, and migration 0023 treats
    seeded groups/roles the same way.
    """
    WorkflowType = apps.get_model("pwms", "WorkflowType")
    EventType = apps.get_model("pwms", "EventType")

    WorkflowType.objects.filter(name=MEETING_TYPE_NAME).delete()
    EventType.objects.filter(
        slug__in=[slug for _, slug, _ in MEETING_EVENT_TYPES]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0049_alter_notification_kind_meeting_meetingattendance_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_meeting_workflow, unseed_meeting_workflow),
    ]
