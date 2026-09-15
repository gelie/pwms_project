"""
Stop the same person being listed twice on one delegation report.

A delegate is a ``DelegationParticipant`` row, and nothing stopped two rows on
one report naming the same account. The report form now checks for it, but the
admin and any script or import could still create the duplicate, so this adds a
partial unique index and the rule holds however the row is written.

The index is partial because ``user`` is optional — an official recorded without
a PWMS account is not compared, and may even appear twice. A database that
already holds duplicate account-backed rows must have them merged or removed
first, or creating the index will fail.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0024_responsible_minister_and_sponsor_fks"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="delegationparticipant",
            constraint=models.UniqueConstraint(
                condition=models.Q(("user__isnull", False)),
                fields=("delegation_report", "user"),
                name="workflows_participant_report_user_uniq",
            ),
        ),
    ]
