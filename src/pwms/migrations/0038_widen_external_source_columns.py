"""Widen the columns that hold values this app does not generate and cannot bound.

A Graph identifier or URL is whatever Graph returns, and both are routinely
longer than the 200 characters Django gives a ``CharField``/``URLField`` by
default. Because these values are written by services rather than by a form,
there is no ``max_length`` validation to catch them first, so an over-long value
is not a friendly error — it is a 500 from the database.

Two live failures prompted this:

* ``SharepointFolder.folder_id`` is a folder's driveItem id. No folder could be
  mirrored, so attaching a document *inside* a folder failed with
  ``DataError: value too long for type character varying(200)``. Only links at a
  drive root worked, as those skip the folder mirror entirely.
* ``WorkflowEvent.document_url`` receives ``Attachment.sharepoint_web_url`` when
  the attachment service records a ``document-attached`` event — and a real
  Graph ``webUrl`` in this tenant is already 249 characters. The attachment was
  created and then the event insert raised, so the record showed the document
  but no activity for it.

The ids are sized as ``Attachment``'s ids (512) and the URLs and names as its
URL (2048) and name (500) columns, since the mirror stores the same values for
the same documents. Widening a ``varchar`` is not destructive: existing rows are
untouched. Fields that *are* form-validated — the workflow ``*_document_url``
fields a user pastes into — are deliberately left alone: an over-long value
there already surfaces as a field error, not a crash.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0037_widen_owner_group_edit_grant"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sharepointdrive",
            name="drive_id",
            field=models.CharField(max_length=512),
        ),
        migrations.AlterField(
            model_name="sharepointdrive",
            name="name",
            field=models.CharField(max_length=500),
        ),
        migrations.AlterField(
            model_name="sharepointfolder",
            name="folder_id",
            field=models.CharField(help_text="Sharepoint folder ID", max_length=512),
        ),
        migrations.AlterField(
            model_name="sharepointfolder",
            name="name",
            field=models.CharField(max_length=500),
        ),
        migrations.AlterField(
            model_name="sharepointfolder",
            name="web_url",
            field=models.URLField(
                blank=True,
                help_text="Direct URL to folder in Sharepoint",
                max_length=2048,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="sharepointsite",
            name="name",
            field=models.CharField(max_length=500),
        ),
        migrations.AlterField(
            model_name="sharepointsite",
            name="site_id",
            field=models.CharField(max_length=512),
        ),
        migrations.AlterField(
            model_name="sharepointsite",
            name="url",
            field=models.URLField(max_length=2048),
        ),
        migrations.AlterField(
            model_name="workflowevent",
            name="document_url",
            field=models.URLField(
                blank=True,
                help_text="Optional SharePoint / document reference.",
                max_length=2048,
            ),
        ),
    ]
