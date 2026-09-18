"""Widen the workflow document links a user pastes from SharePoint.

Migration ``0038`` widened the columns this app *writes from Graph* — ids, names
and ``WorkflowEvent.document_url`` — and deliberately left these seven alone, on
the grounds that a ``ModelForm`` catches an over-long paste as a friendly field
error rather than a database 500. That reasoning under-weighted how often a
legitimate value is too long: a real Graph ``webUrl`` in this tenant runs to 249
characters, and the ``URLField`` default is 200, so a user copying a document link
out of SharePoint — the workflow's documented way of attaching one — was refused
the link that had just worked everywhere else in the app.

All seven now match the 2048 used by ``Attachment.sharepoint_web_url``,
``SharepointFolder.web_url`` and ``WorkflowEvent.document_url``, so a SharePoint
link is the same length wherever it is pasted. Widening a ``varchar`` is not
destructive: existing rows are untouched, and the browser ``maxlength`` on each
form's ``TextInput`` follows the model field automatically.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0041_workflowtype_owner_capability_policy"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bill",
            name="bill_document_url",
            field=models.URLField(
                blank=True,
                help_text="SharePoint link to the bill document (BRS §7B).",
                max_length=2048,
            ),
        ),
        migrations.AlterField(
            model_name="billversion",
            name="document_url",
            field=models.URLField(
                blank=True,
                help_text="SharePoint link to this version's document (BRS §7B).",
                max_length=2048,
            ),
        ),
        migrations.AlterField(
            model_name="delegationreport",
            name="report_document_url",
            field=models.URLField(
                blank=True,
                help_text="SharePoint link to the delegation report document (BR02.3.14).",
                max_length=2048,
            ),
        ),
        migrations.AlterField(
            model_name="delegationreportupdate",
            name="atc_document_url",
            field=models.URLField(
                blank=True,
                help_text="SharePoint link to the ATC / update document (BR03.5.5).",
                max_length=2048,
            ),
        ),
        migrations.AlterField(
            model_name="internationalagreement",
            name="agreement_document_url",
            field=models.URLField(
                blank=True,
                help_text="SharePoint link to the uploaded agreement document (BR02).",
                max_length=2048,
            ),
        ),
        migrations.AlterField(
            model_name="internationalagreement",
            name="explanatory_memorandum_url",
            field=models.URLField(
                blank=True,
                help_text="SharePoint link to the explanatory memorandum (BR02).",
                max_length=2048,
            ),
        ),
        migrations.AlterField(
            model_name="workflowreferral",
            name="response_document_url",
            field=models.URLField(blank=True, max_length=2048),
        ),
    ]
