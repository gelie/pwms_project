"""
Seed the attachment (document) event types used by the audit trail.

Creates, idempotently and matched by natural key, one :class:`EventType` per
attachment lifecycle fact recorded by ``pwms.services.attachments``:

* a document was attached to a record;
* a document was detached from it;
* a new version of an already-attached document was filed.

They are ordinary registry rows, so administrators can rename or extend them like
any other event type. Reverse is a no-op: deleting types that events already
reference would corrupt history.
"""

from django.db import migrations

ATTACHMENT_EVENT_TYPES = [
    (
        "Document attached",
        "document-attached",
        "A SharePoint document was attached to the record.",
    ),
    (
        "Document detached",
        "document-detached",
        (
            "A SharePoint document was detached from the record "
            "(the file itself stays in SharePoint)."
        ),
    ),
    (
        "Document version added",
        "document-version-added",
        "A new version of an already-attached document was uploaded to SharePoint.",
    ),
]


def seed_attachment_event_types(apps, schema_editor):
    EventType = apps.get_model("pwms", "EventType")
    for name, slug, description in ATTACHMENT_EVENT_TYPES:
        EventType.objects.get_or_create(
            name=name,
            defaults={
                "slug": slug,
                "description": description,
                "is_system": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0030_attachmentversion"),
    ]

    operations = [
        migrations.RunPython(seed_attachment_event_types, migrations.RunPython.noop),
    ]
