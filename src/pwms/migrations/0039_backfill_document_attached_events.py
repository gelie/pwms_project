"""Backfill the ``document-attached`` events lost to a 200-character column.

Before migration ``0038``, ``WorkflowEvent.document_url`` was ``varchar(200)``.
The attachment service files the ``document-attached`` event *after* committing
the ``Attachment`` row, copying ``Attachment.sharepoint_web_url`` into that
column — so when a real Graph ``webUrl`` ran past 200 characters, the attachment
was saved and the event raised, leaving the record showing a document with no
activity for it.

This reconstructs the missing events. A target is only topped up as far as its
own counts allow: an instance should hold at least as many ``document-attached``
events as it has attachments, so the shortfall is created for the oldest
unrecorded attachments and nothing is ever duplicated. Events that carry no
payload — the ones ``seed_demo_data`` writes — therefore count towards a target's
events and are left alone.

A reconstructed event is marked ``origin="system"`` with no actor, and carries
``payload["backfilled"] = True``: the attachment row names who filed the document,
but the system wrote this event long after the fact, and the payload keeps the
snapshot shape ``pwms.services.attachments`` writes so nothing consuming it needs
a special case. Its ``document_url`` is the attachment's SharePoint web URL, so
the restored entry opens the document from the UI like any other.

The reverse deletes the events this migration created, and nothing else.
"""

from collections import defaultdict

from django.db import migrations

#: Content-type model names whose instances carry a domain event log.
WORKFLOW_MODELS = (
    "bill",
    "delegationreport",
    "internationalagreement",
    "internationalresolution",
)


def _snapshot(attachment):
    """The payload shape ``attachments._attachment_payload`` writes, plus a marker."""
    return {
        "attachment_id": str(attachment.public_id),
        "name": attachment.name,
        "item_id": attachment.item_id,
        "drive_id": attachment.drive_id,
        "folder_path": attachment.sharepoint_folder_path,
        "type": attachment.type,
        "size": attachment.size,
        "web_url": attachment.sharepoint_web_url or "",
        "backfilled": True,
    }


def backfill_document_events(apps, schema_editor):
    """Record a ``document-attached`` event for attachments that have none."""
    Attachment = apps.get_model("pwms", "Attachment")
    EventType = apps.get_model("pwms", "EventType")
    WorkflowEvent = apps.get_model("pwms", "WorkflowEvent")

    event_type = EventType.objects.filter(slug="document-attached").first()
    if event_type is None:
        # The registry row is seeded by migration 0031; without it there is
        # nothing to record the events against.
        return

    targets = defaultdict(list)
    for attachment in Attachment.objects.filter(
        content_type__app_label="pwms", content_type__model__in=WORKFLOW_MODELS
    ).order_by("created_at"):
        targets[(attachment.content_type_id, attachment.object_id)].append(attachment)

    for (content_type_id, object_id), attachments in targets.items():
        events = WorkflowEvent.objects.filter(
            content_type_id=content_type_id,
            object_id=object_id,
            event_type=event_type,
        )
        recorded = {(event.payload or {}).get("attachment_id") for event in events}
        unrecorded = [
            attachment
            for attachment in attachments
            if str(attachment.public_id) not in recorded
        ]
        shortfall = max(len(attachments) - events.count(), 0)
        for attachment in unrecorded[:shortfall]:
            WorkflowEvent.objects.create(
                content_type_id=content_type_id,
                object_id=object_id,
                event_type=event_type,
                # The event belongs when the document was filed, not now.
                occurred_at=attachment.created_at,
                actor=None,
                origin="system",
                payload=_snapshot(attachment),
                document_url=(
                    attachment.sharepoint_web_url or attachment.download_url or ""
                ),
            )


def unbackfill_document_events(apps, schema_editor):
    """Reverse: drop the reconstructed events, leaving organically-written ones."""
    WorkflowEvent = apps.get_model("pwms", "WorkflowEvent")
    WorkflowEvent.objects.filter(payload__backfilled=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0038_widen_external_source_columns"),
    ]

    operations = [
        migrations.RunPython(backfill_document_events, unbackfill_document_events),
    ]
