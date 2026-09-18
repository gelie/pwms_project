"""Let each workflow type declare what its owning group may do (RBAC).

Materialisation granted the owning group view, edit and transition in code, so an
office that wanted a different policy for one of its types — no edit on a
read-only register, or delete on a type whose records are disposable — had to edit
every instance's ``WorkflowGroupAccess`` row by hand, however many there were.
The owning group's capabilities are now fields on ``WorkflowType``:
``materialize_group_access`` reads them when it seeds a new instance, and
``sync_type_group_access --update-existing`` re-applies them to instances that
predate a change.

Schema only — the defaults reproduce the previous behaviour exactly (edit and
transition on, delete/share/comment/manage off), so no existing access row needs
backfilling and nothing changes for instances already created. Migration ``0040``
remains the one that widened ``can_transition`` onto the rows materialised before
it was a default.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0040_widen_owner_group_transition_grant"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowtype",
            name="owner_can_comment",
            field=models.BooleanField(
                default=False,
                help_text="New instances let this group comment on the record.",
                verbose_name="Owning group may comment",
            ),
        ),
        migrations.AddField(
            model_name="workflowtype",
            name="owner_can_delete",
            field=models.BooleanField(
                default=False,
                help_text="New instances let this group delete the record.",
                verbose_name="Owning group may delete",
            ),
        ),
        migrations.AddField(
            model_name="workflowtype",
            name="owner_can_edit",
            field=models.BooleanField(
                default=True,
                help_text="New instances let this group change fields.",
                verbose_name="Owning group may edit",
            ),
        ),
        migrations.AddField(
            model_name="workflowtype",
            name="owner_can_manage",
            field=models.BooleanField(
                default=False,
                help_text="New instances let this group manage the record's access.",
                verbose_name="Owning group may manage access",
            ),
        ),
        migrations.AddField(
            model_name="workflowtype",
            name="owner_can_share",
            field=models.BooleanField(
                default=False,
                help_text="New instances let this group share the record.",
                verbose_name="Owning group may share",
            ),
        ),
        migrations.AddField(
            model_name="workflowtype",
            name="owner_can_transition",
            field=models.BooleanField(
                default=True,
                help_text="New instances let this group move the record on.",
                verbose_name="Owning group may transition",
            ),
        ),
    ]
