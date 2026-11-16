"""Restore ``Group``'s ``BaseModel`` columns and its start/end dates.

Migration 0060 dropped ``public_id``, ``created_at`` and ``updated_at`` when the
model stopped inheriting ``BaseModel``, and dropped ``start_date``/``end_date``.
The model inherits ``BaseModel`` again and carries the two dates, so the columns
have to come back.

``public_id`` is unique, so it is added nullable, filled one row at a time, and
only then made unique: a single ``ADD COLUMN ... DEFAULT`` would copy one UUID
onto every existing group and violate the constraint. The values issued here are
new — the originals were dropped with the column and cannot be recovered.
"""

import uuid

from django.db import migrations, models

PUBLIC_ID_HELP = "Public UUIDv7 identifier for external references and API routes."


def populate_public_id(apps, schema_editor):
    """Give every existing group its own public UUID."""
    Group = apps.get_model("pwms", "Group")
    for pk in Group.objects.values_list("pk", flat=True).iterator():
        Group.objects.filter(pk=pk).update(public_id=uuid.uuid7())


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0060_parliamentterm_remove_group_created_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="group",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True),
        ),
        migrations.AddField(
            model_name="group",
            name="end_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="group",
            name="public_id",
            field=models.UUIDField(
                db_index=True,
                editable=False,
                help_text=PUBLIC_ID_HELP,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="group",
            name="start_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="group",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.RunPython(populate_public_id, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="group",
            name="public_id",
            field=models.UUIDField(
                db_index=True,
                default=uuid.uuid7,
                editable=False,
                help_text=PUBLIC_ID_HELP,
                unique=True,
            ),
        ),
    ]
