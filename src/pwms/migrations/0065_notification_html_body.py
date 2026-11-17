from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0064_alter_billversion_document_url_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="html_body",
            field=models.TextField(
                blank=True,
                help_text=(
                    "HTML body sent as the alternative part with ``body``; blank on "
                    "in-app rows and on rows written before HTML alerts shipped."
                ),
            ),
        ),
    ]
