"""
Link the responsible minister and bill sponsor to real user accounts.

Both fields were free text. Each now points at the PWMS account of the person,
while the original text is kept under a new name (``responsible_minister_name``
/ ``sponsor_name``) for the cases an account cannot cover: a former office
holder, a minister without a PWMS account, or a bill sponsored by an
originating authority such as a committee or department.

The foreign keys are nullable and ``SET_NULL``: losing a user account must never
delete an agreement or a bill. No backfill is attempted — the stored values are
names or offices as recorded on the documents, not account references — so
existing rows keep their text and gain an empty link, which an editor can fill
in from the form.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0023_seed_executive_groups_and_roles"),
    ]

    operations = [
        migrations.RenameField(
            model_name="internationalagreement",
            old_name="responsible_minister",
            new_name="responsible_minister_name",
        ),
        migrations.AlterField(
            model_name="internationalagreement",
            name="responsible_minister_name",
            field=models.CharField(
                blank=True,
                help_text="Minister as recorded on the tabled document - for a former office holder or one without a PWMS account (BR02).",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="internationalagreement",
            name="responsible_minister",
            field=models.ForeignKey(
                blank=True,
                help_text="Responsible Member of the Executive (Minister) submitting the agreement (BR02).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="responsible_agreements",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RenameField(
            model_name="bill",
            old_name="sponsor",
            new_name="sponsor_name",
        ),
        migrations.AlterField(
            model_name="bill",
            name="sponsor_name",
            field=models.CharField(
                blank=True,
                help_text="Sponsor or originating authority as recorded on the bill (BRS §7A) - e.g. a committee or department with no PWMS account.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="bill",
            name="sponsor",
            field=models.ForeignKey(
                blank=True,
                help_text="Sponsor or originating authority with a PWMS account (BRS §7A).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="sponsored_bills",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
