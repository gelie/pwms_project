"""Promote ``State.public_name`` to a first-class ``PublicStatus`` model.

The BRS §12 public vocabulary used to be a ``CharField(choices=…)`` on ``State``.
It becomes a :class:`~pwms.models.PublicStatus` registry row that ``State`` FKs
to, so the vocabulary and its presentation are data an administrator can curate
and every workflow type shares one set of statuses.

The migration seeds one row per existing choice, maps each state's stored
``public_name`` key to the row with the matching label, then drops the column.
"""

import uuid

import django.db.models.deletion
import django_extensions.db.fields
from django.db import migrations, models

# (former public_name key, name, order). The key is only how the values already
# stored on State.public_name are matched to a row; the row's own slug is derived
# from its name. The seven BRS §12 statuses follow the generic buckets they
# share the field with.
PUBLIC_STATUSES = [
    ("new", "New", 10),
    ("scheduled", "Scheduled", 20),
    ("in_progress", "In Progress", 30),
    ("postponed", "Postponed", 40),
    ("referred", "Referred", 50),
    ("withdrawn", "Withdrawn", 60),
    ("on_hold", "On Hold", 70),
    ("cancelled", "Cancelled", 80),
    ("implemented", "Implemented", 90),
    ("closed", "Closed", 100),
    ("introduced", "Introduced", 110),
    ("under_consideration", "Under Parliamentary Consideration", 120),
    ("ncop", "National Council of Provinces", 130),
    ("mediation", "Mediation / Reconsideration", 140),
    ("awaiting_assent", "Awaiting Presidential Assent", 150),
    ("signed_into_law", "Signed into Law", 160),
    ("constitutional_review", "Referred Back / Constitutional Review", 170),
]


def seed_and_map(apps, schema_editor):
    """Create the registry rows, then point every state at the matching one."""
    PublicStatus = apps.get_model("pwms", "PublicStatus")
    State = apps.get_model("pwms", "State")

    by_key = {}
    for key, name, order in PUBLIC_STATUSES:
        status, _ = PublicStatus.objects.get_or_create(
            name=name, defaults={"order": order}
        )
        by_key[key] = status

    for state in State.objects.all():
        status = by_key.get(state.public_name)
        if status is None:
            # A value outside the seeded vocabulary (locally customised data):
            # keep it as its own status rather than lose the mapping.
            status, _ = PublicStatus.objects.get_or_create(
                name=state.public_name or "New", defaults={"order": 1000}
            )
            by_key[state.public_name] = status
        state.public_status = status
        state.save(update_fields=["public_status"])


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0055_meeting_register_source"),
    ]

    operations = [
        migrations.CreateModel(
            name="PublicStatus",
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
                ("name", models.CharField(max_length=100, unique=True)),
                (
                    "slug",
                    django_extensions.db.fields.AutoSlugField(
                        blank=True, editable=False, populate_from="name", unique=True
                    ),
                ),
                (
                    "description",
                    models.TextField(
                        blank=True,
                        help_text="Citizen-facing explanation of what this status means.",
                    ),
                ),
                (
                    "order",
                    models.IntegerField(
                        default=0, help_text="Display order on public-facing views."
                    ),
                ),
                (
                    "color",
                    models.CharField(
                        default="#5b8f22", help_text="Hex color code", max_length=7
                    ),
                ),
            ],
            options={
                "verbose_name": "public status",
                "verbose_name_plural": "public statuses",
                "ordering": ["order", "name"],
            },
        ),
        # Added nullable so the column can exist before the backfill; tightened
        # to NOT NULL once every state points at a status.
        migrations.AddField(
            model_name="state",
            name="public_status",
            field=models.ForeignKey(
                help_text="Simplified citizen-facing status this state publishes as (BRS §12).",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="states",
                to="pwms.publicstatus",
            ),
        ),
        migrations.RunPython(seed_and_map, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="state",
            name="public_status",
            field=models.ForeignKey(
                help_text="Simplified citizen-facing status this state publishes as (BRS §12).",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="states",
                to="pwms.publicstatus",
            ),
        ),
        migrations.RemoveField(
            model_name="state",
            name="public_name",
        ),
    ]
