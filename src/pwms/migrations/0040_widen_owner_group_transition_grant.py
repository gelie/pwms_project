"""Give a type's owning group the transition right as well (RBAC).

``materialize_group_access`` gave the owning group view and edit but not
``can_transition``, so the unit that owns a workflow type could change a record's
fields but never move it on. The *Status* menu, the Referrals tab's transition
links and the ``workflow_transition`` view all resolve that capability, and
nothing granted it — in practice leaving every transition to superusers, because
grants live per instance and there was no way to state the policy once for a type
(see Functional Design §4).

The materialised **primary** row now carries ``can_transition`` too, which is what
``materialize_group_access`` creates from here on. This migration applies the same
widening to rows materialised before the change: a primary row still without the
right is granted it. Nothing else is touched — non-primary (viewer group) rows stay
read-only, and a primary row that already carries the right is left as it is.

The reverse narrows primary rows again, so rolling back restores the old default.
A grant made by hand in the admin carries no provenance, so the reverse cannot tell
it from the one this migration applied, and clears it on primary rows; narrow a row
individually in the admin if that matters.
"""

from django.db import migrations


def widen_owner_group_transition(apps, schema_editor):
    """Give each type-owning group's grant the transition right."""
    WorkflowGroupAccess = apps.get_model("pwms", "WorkflowGroupAccess")
    WorkflowGroupAccess.objects.filter(is_primary=True, can_transition=False).update(
        can_transition=True
    )


def narrow_owner_group_transition(apps, schema_editor):
    """Reverse: take the transition right back off the primary rows."""
    WorkflowGroupAccess = apps.get_model("pwms", "WorkflowGroupAccess")
    WorkflowGroupAccess.objects.filter(is_primary=True, can_transition=True).update(
        can_transition=False
    )


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0039_backfill_document_attached_events"),
    ]

    operations = [
        migrations.RunPython(
            widen_owner_group_transition, narrow_owner_group_transition
        ),
    ]
