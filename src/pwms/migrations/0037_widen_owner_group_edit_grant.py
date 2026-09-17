"""Let a type's owning group edit its records (RBAC).

``materialize_group_access`` used to give the type's owning group a view-only row,
so on a fresh instance no group could edit anything: every
``WorkflowGroupAccess`` row started read-only, and only the record's own ``owner``
(or a superuser) could change it. That does not match how the office is arranged
— IRP owns the three international-relations workflow types, LSO the Bill — and
it left members of the owning group without the Add/Edit affordances on their own
unit's records.

The materialised **primary** row now carries ``can_edit`` too, which is what
``materialize_group_access`` creates from here on. This migration applies the
same widening to rows that were materialised before the change: a primary row
that is still view-only is granted ``can_edit``. Nothing else is touched —
non-primary (viewer group) rows stay read-only, and a primary row that already
carries ``can_edit`` is left as it is.

The reverse narrows primary rows again, so rolling the migration back restores
the old default. A grant made by hand in the admin also carries no provenance, so
the reverse cannot tell it from the one this migration applied, and clears it on
primary rows; narrow a row individually in the admin if that matters.
"""

from django.db import migrations


def widen_owner_group_edit(apps, schema_editor):
    """Give each type-owning group's grant the edit right."""
    WorkflowGroupAccess = apps.get_model("pwms", "WorkflowGroupAccess")
    WorkflowGroupAccess.objects.filter(is_primary=True, can_edit=False).update(
        can_edit=True
    )


def narrow_owner_group_edit(apps, schema_editor):
    """Reverse: make the type-owning group's grant view-only again."""
    WorkflowGroupAccess = apps.get_model("pwms", "WorkflowGroupAccess")
    WorkflowGroupAccess.objects.filter(is_primary=True, can_edit=True).update(
        can_edit=False
    )


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0036_delegation_participant_soft_delete"),
    ]

    operations = [
        migrations.RunPython(widen_owner_group_edit, narrow_owner_group_edit),
    ]
