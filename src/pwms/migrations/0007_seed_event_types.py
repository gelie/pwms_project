"""
Seed the standard workflow event types and wire the first BRS transition guard.

Creates, idempotently and matched by natural key:

* five standard :class:`EventType` rows covering the BRS lifecycle facts
  (report document attached, ATC update published, implementation reported,
  referral created / responded);
* one example guard: the *Delegation Report* transition **Close – House
  approved** now requires a *ATC update published* event (BRS BR03.5.3), so a
  report cannot be closed before its ATC publication has been captured.

Guards are ordinary admin data — remove or extend them per environment.

Reverse is a no-op: deleting event types that transitions may reference (or
events that recorded them) would corrupt history.
"""

from django.db import migrations

STANDARD_EVENT_TYPES = [
    (
        "Report document attached",
        "report-document-attached",
        "Electronic copy of the delegation report uploaded to SharePoint (BR02.3.14).",
    ),
    (
        "ATC update published",
        "atc-update-published",
        "Report update published in the Announcements, Tablings and Committee "
        "Reports, with reference, date and page (BR03.5.3).",
    ),
    (
        "Implementation reported",
        "implementation-reported",
        "Parliament's implementation of a resolution has been reported.",
    ),
    (
        "Referral created",
        "referral-created",
        "Workflow referred to a committee / group for action.",
    ),
    (
        "Referral responded",
        "referral-responded",
        "Referred committee / group responded to the referral.",
    ),
]

# BR03.5.3: the ATC publication is the evidence that supports closing a report.
REPORT_CLOSE_TRANSITION = "Close – House approved"
REPORT_CLOSE_REQUIRES = "atc-update-published"


def seed_event_types(apps, schema_editor):
    EventType = apps.get_model("pwms", "EventType")
    Transition = apps.get_model("pwms", "Transition")

    event_types = {}
    for name, slug, description in STANDARD_EVENT_TYPES:
        event_type, _ = EventType.objects.get_or_create(
            name=name,
            defaults={
                "slug": slug,
                "description": description,
                "is_system": True,
            },
        )
        event_types[slug] = event_type

    close_transition = Transition.objects.filter(
        workflow_type__name="Delegation Report",
        name=REPORT_CLOSE_TRANSITION,
    ).first()
    if close_transition is not None:
        close_transition.required_event_types.add(event_types[REPORT_CLOSE_REQUIRES])


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0006_eventtype_transition_required_event_types_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_event_types, migrations.RunPython.noop),
    ]
