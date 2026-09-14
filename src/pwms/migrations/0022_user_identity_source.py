"""
Add ``User.identity_source`` (default ``erp``).

The field records which system owns a user row: ``erp`` rows are created,
updated or deactivated by ``sync_users_oracle``; ``local`` rows (Ministers and
other office holders appointed from outside the ERP) are managed in PWMS and
the sync leaves them alone.

The default preserves the previous behaviour for every existing row — the sync
owned all of them — so the protection is opt-in per user and the flag is what
``createsuperuser``, the admin and the committee scraper go on producing. No
backfill is possible from the data itself: ``idno_hmac`` is only populated when
``IDNO_HMAC_KEY`` is configured, and those rows are ERP-sourced anyway.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0021_assign_bill_workflow_type_group"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="identity_source",
            field=models.CharField(
                choices=[("erp", "ERP (Oracle)"), ("local", "Locally managed")],
                default="erp",
                help_text="Which system owns this identity. 'erp' (the default) leaves the user to sync_users_oracle; set 'local' for identities managed in PWMS only - e.g. a Minister appointed from outside the ERP, who must not be deactivated when Oracle does not know them.",
                max_length=10,
            ),
        ),
    ]
