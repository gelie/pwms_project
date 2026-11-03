"""Link each bill to its own SharePoint folder (``Bill.sharepoint_folder``).

``pwms.services.bill_folders.file_bill_folder`` files a B-number folder under
the Bill Base Folder on SharePoint (``POST /drives/{drive-id}/items/
{parent-item-id}/children``, idempotent) and mirrors the Graph item as a
:class:`SharepointFolder` row; the FK written here is what the attachment
picker and uploads treat as the bill's default filing target.

Hand-written rather than generated so it carries only that column and not the
pending help_text rewording sitting in the working tree.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0056_publicstatus"),
    ]

    operations = [
        migrations.AddField(
            model_name="bill",
            name="sharepoint_folder",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "This bill's folder under the Bill Base Folder, filed "
                    "automatically when the bill is saved; attachments default "
                    "to it."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="bills",
                to="pwms.sharepointfolder",
            ),
        ),
    ]
