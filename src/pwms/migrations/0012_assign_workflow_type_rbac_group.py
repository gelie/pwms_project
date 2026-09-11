"""Scope the seeded workflow types to their owning group (RBAC).

Both seeded workflow types — *Delegation Report* and *International Resolution* —
are owned by group 98, *IRP: MR: Man And Gen* (the Multilateral Relations unit
that handles international engagements).

The assignment is skipped when that group does not exist: the seeded types are
created by migration 0005, whereas group 98 only appears once the Oracle
organisational sync has run. A fresh or test database therefore keeps the types
ungrouped (which closes creation to everyone but superusers until an
administrator assigns a group and its create roles).
"""

from django.db import migrations

#: Group pk that owns the seeded workflow types (IRP: MR: Man And Gen).
RBAC_GROUP_ID = 98

#: Types seeded by migration 0005.
SEEDED_TYPE_NAMES = ("Delegation Report", "International Resolution")


def assign_group(apps, schema_editor):
    """Point the seeded types at group 98 when that group exists."""
    Group = apps.get_model("pwms", "Group")
    WorkflowType = apps.get_model("pwms", "WorkflowType")
    if not Group.objects.filter(pk=RBAC_GROUP_ID).exists():
        return
    WorkflowType.objects.filter(name__in=SEEDED_TYPE_NAMES, group__isnull=True).update(
        group_id=RBAC_GROUP_ID
    )


def unassign_group(apps, schema_editor):
    """Reverse: detach the seeded types from group 98."""
    WorkflowType = apps.get_model("pwms", "WorkflowType")
    WorkflowType.objects.filter(
        name__in=SEEDED_TYPE_NAMES, group_id=RBAC_GROUP_ID
    ).update(group=None)


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0011_workflowtype_create_roles_workflowtype_group"),
    ]

    operations = [
        migrations.RunPython(assign_group, unassign_group),
    ]
