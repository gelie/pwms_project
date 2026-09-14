"""Scope the seeded Bill workflow type to its owning group (RBAC).

The Online Bill Tracking BRS names no organisational unit, so migration ``0019``
seeds the *Bill* type without a group and creation is closed to everyone but
superusers. The owning unit is the Management & General section of the Legal
Services Office (*LSO: Legal Services: Man And Gen*), which this migration
records.

The assignment is skipped when that group does not exist: the type is seeded by
``0019``, whereas the group only appears once the Oracle organisational sync has
run. A fresh or test database therefore keeps the type ungrouped. An existing
group assignment is never overwritten, and the reverse only detaches the group
this migration applied.
"""

from django.db import migrations

GROUP_NAME = "LSO: Legal Services: Man And Gen"
BILL_TYPE_NAME = "Bill"


def assign_group(apps, schema_editor):
    """Point the Bill type at the Legal Services group when it exists."""
    Group = apps.get_model("pwms", "Group")
    WorkflowType = apps.get_model("pwms", "WorkflowType")
    group = Group.objects.filter(name__iexact=GROUP_NAME).first()
    if group is None:
        return
    WorkflowType.objects.filter(name=BILL_TYPE_NAME, group__isnull=True).update(
        group=group
    )


def unassign_group(apps, schema_editor):
    """Reverse: detach the Bill type from the Legal Services group."""
    Group = apps.get_model("pwms", "Group")
    WorkflowType = apps.get_model("pwms", "WorkflowType")
    group = Group.objects.filter(name__iexact=GROUP_NAME).first()
    if group is None:
        return
    WorkflowType.objects.filter(name=BILL_TYPE_NAME, group_id=group.pk).update(
        group=None
    )


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0020_remove_bill_current_version_billversion"),
    ]

    operations = [
        migrations.RunPython(assign_group, unassign_group),
    ]
