"""Buildings and venues, and the meeting's attendance mode.

Adds the on-premises venue catalogue (:class:`Building` and :class:`Venue`) and
reshapes the meeting's location fields around it:

* the old free-text ``venue`` is **renamed** to ``off_site_venue`` and the old
  ``location`` to ``off_site_address`` (a rename, not a drop, so any text already
  recorded is kept — it now reads as the off-premises venue);
* a new ``venue`` foreign key points at a room in a building (``NULL`` for an
  off-premises meeting);
* ``is_virtual`` (a boolean) becomes ``attendance_mode`` — *on the premises* /
  *hybrid* / *virtual* — with existing ``True`` rows converted to *virtual*
  before the boolean is dropped.

The rename-and-alter (rather than remove-and-add) is deliberate: Postgres cannot
cast the varchar ``venue``/``location`` columns to the new shapes in place, and a
drop would lose whatever had been captured.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models


def is_virtual_to_attendance_mode(apps, schema_editor):
    """Carry ``is_virtual=True`` rows over to ``attendance_mode='virtual'``."""
    Meeting = apps.get_model("pwms", "Meeting")
    Meeting.objects.filter(is_virtual=True).update(attendance_mode="virtual")


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0052_meeting_agenda_help"),
    ]

    operations = [
        migrations.CreateModel(
            name="Building",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "public_id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid7,
                        editable=False,
                        help_text="Public UUIDv7 identifier for external references and API routes.",
                        unique=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=255, unique=True)),
                (
                    "short_name",
                    models.CharField(
                        blank=True,
                        help_text="Short label, e.g. NA or NCOP.",
                        max_length=50,
                    ),
                ),
                (
                    "address",
                    models.CharField(
                        blank=True,
                        help_text="Street address of the building.",
                        max_length=255,
                    ),
                ),
                ("description", models.TextField(blank=True)),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Inactive buildings are hidden from the venue picker.",
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="Venue",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "public_id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid7,
                        editable=False,
                        help_text="Public UUIDv7 identifier for external references and API routes.",
                        unique=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "name",
                    models.CharField(help_text="Room or chamber name.", max_length=255),
                ),
                ("floor", models.CharField(blank=True, max_length=50)),
                (
                    "capacity",
                    models.PositiveIntegerField(
                        blank=True, help_text="Seating capacity, if known.", null=True
                    ),
                ),
                ("notes", models.TextField(blank=True)),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Inactive venues are hidden from the meeting form.",
                    ),
                ),
                (
                    "building",
                    models.ForeignKey(
                        help_text="Building the room is in.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="venues",
                        to="pwms.building",
                    ),
                ),
            ],
            options={
                "verbose_name": "venue",
                "verbose_name_plural": "venues",
                "ordering": ["building__name", "name"],
            },
        ),
        migrations.AddField(
            model_name="meeting",
            name="attendance_mode",
            field=models.CharField(
                choices=[
                    ("on_premise", "On the premises"),
                    ("hybrid", "Hybrid"),
                    ("virtual", "Virtual"),
                ],
                default="on_premise",
                help_text="How the meeting is attended: on the premises, hybrid, or virtual.",
                max_length=20,
            ),
        ),
        migrations.RunPython(is_virtual_to_attendance_mode, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="meeting",
            name="is_virtual",
        ),
        migrations.RenameField(
            model_name="meeting",
            old_name="venue",
            new_name="off_site_venue",
        ),
        migrations.AlterField(
            model_name="meeting",
            name="off_site_venue",
            field=models.CharField(
                blank=True,
                help_text="Name of an off-premises venue (used when no on-premises room is set).",
                max_length=255,
            ),
        ),
        migrations.RenameField(
            model_name="meeting",
            old_name="location",
            new_name="off_site_address",
        ),
        migrations.AlterField(
            model_name="meeting",
            name="off_site_address",
            field=models.CharField(
                blank=True,
                help_text="Address of the off-premises venue.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="meeting",
            name="venue",
            field=models.ForeignKey(
                blank=True,
                help_text="On-premises room the meeting is held in (a room in a building). Leave blank and use the off-site fields for an external venue.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="meetings",
                to="pwms.venue",
            ),
        ),
        migrations.AlterField(
            model_name="meeting",
            name="meeting_link",
            field=models.URLField(
                blank=True,
                help_text="Join link for a hybrid or virtual meeting.",
                max_length=2048,
            ),
        ),
        migrations.AddIndex(
            model_name="venue",
            index=models.Index(
                fields=["building", "name"], name="pwms_venue_buildin_cfd9b5_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="venue",
            constraint=models.UniqueConstraint(
                fields=("building", "name"), name="venues_venue_building_name_uniq"
            ),
        ),
    ]
