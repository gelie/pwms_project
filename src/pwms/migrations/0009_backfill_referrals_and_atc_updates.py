"""
Phase 2/3 data migration.

1. Seed the remaining standard event types (referral recalled / expired).
2. Backfill ``WorkflowReferral`` rows from the legacy ``referred_to_groups``
   M2M, one *open* referral per existing link, and record a
   ``referral-created`` event for each (the original link had no timestamp, so
   the workflow's ``created_at`` is used as the best available proxy).
3. Backfill ``DelegationReportUpdate`` rows from the legacy single-set
   ``atc_*`` fields on delegation reports and record an
   ``atc-update-published`` event for each so the seeded close guard stays
   satisfied for historical data.

The M2M and the legacy ``atc_*`` fields are removed in the next migration
(0010), after this backfill has run. Reverse is a no-op: the removed columns
cannot be reconstructed from the typed tables without data loss guesses.
"""

from django.db import migrations

EXTRA_EVENT_TYPES = [
    (
        "Referral recalled",
        "referral-recalled",
        "A referral was withdrawn before the committee responded.",
    ),
    (
        "Referral expired",
        "referral-expired",
        "A referral passed its due date without a response.",
    ),
]

BACKFILL_NOTE = "Backfilled from the legacy referred_to_groups field."


def _content_type(ContentType, model_name):
    # NB: modern ContentType has no ``name`` field (removed in
    # contenttypes.0002_remove_content_type_name), so match on app_label+model.
    content_type, _ = ContentType.objects.get_or_create(
        app_label="pwms",
        model=model_name,
    )
    return content_type


def _event_type(EventType, slug):
    return EventType.objects.filter(slug=slug).first()


def _backfill_referrals(apps, ContentType, model_name, label):
    Model = apps.get_model("pwms", model_name)
    WorkflowReferral = apps.get_model("pwms", "WorkflowReferral")
    WorkflowEvent = apps.get_model("pwms", "WorkflowEvent")
    EventType = apps.get_model("pwms", "EventType")

    content_type = _content_type(ContentType, label)
    created_event = _event_type(EventType, "referral-created")

    for obj in Model.objects.all().iterator():
        for group in obj.referred_to_groups.all():
            referral = WorkflowReferral.objects.create(
                content_type=content_type,
                object_id=obj.pk,
                referred_to=group,
                referred_by=obj.owner,
                referred_at=obj.created_at,
                status="open",
                notes=BACKFILL_NOTE,
            )
            if created_event is not None:
                WorkflowEvent.objects.create(
                    content_type=content_type,
                    object_id=obj.pk,
                    event_type=created_event,
                    occurred_at=obj.created_at,
                    actor=obj.owner,
                    origin="system",
                    payload={"backfill": True, "referred_to": group.name},
                    notes=f"{BACKFILL_NOTE} (referral #{referral.pk})",
                )


def _backfill_atc_updates(apps, ContentType):
    DelegationReport = apps.get_model("pwms", "DelegationReport")
    DelegationReportUpdate = apps.get_model("pwms", "DelegationReportUpdate")
    WorkflowEvent = apps.get_model("pwms", "WorkflowEvent")
    EventType = apps.get_model("pwms", "EventType")

    content_type = _content_type(ContentType, "delegationreport")
    atc_event = _event_type(EventType, "atc-update-published")

    reports = DelegationReport.objects.exclude(
        atc_reference="",
        atc_publication_date__isnull=True,
        atc_page_number="",
        atc_document_url="",
    )
    for report in reports.iterator():
        update = DelegationReportUpdate.objects.create(
            delegation_report=report,
            update_date=report.atc_publication_date or report.updated_at.date(),
            atc_reference=report.atc_reference,
            atc_publication_date=report.atc_publication_date,
            atc_page_number=report.atc_page_number,
            atc_document_url=report.atc_document_url,
            notes="Backfilled from the legacy atc_* fields (BR03).",
        )
        if atc_event is not None:
            WorkflowEvent.objects.create(
                content_type=content_type,
                object_id=report.pk,
                event_type=atc_event,
                occurred_at=report.updated_at,
                origin="system",
                payload={
                    "backfill": True,
                    "atc_reference": report.atc_reference,
                    "atc_page_number": report.atc_page_number,
                },
                document_url=report.atc_document_url,
                notes=f"Backfilled from the legacy atc_* fields (update #{update.pk}).",
            )


def backfill(apps, schema_editor):
    EventType = apps.get_model("pwms", "EventType")
    ContentType = apps.get_model("contenttypes", "ContentType")

    for name, slug, description in EXTRA_EVENT_TYPES:
        EventType.objects.get_or_create(
            name=name,
            defaults={"slug": slug, "description": description, "is_system": True},
        )

    _backfill_referrals(apps, ContentType, "DelegationReport", "delegationreport")
    _backfill_referrals(
        apps, ContentType, "InternationalResolution", "internationalresolution"
    )
    _backfill_atc_updates(apps, ContentType)


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0008_delegationreportupdate_transitioncondition_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
