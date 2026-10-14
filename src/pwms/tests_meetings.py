"""Meetings (the Events workflow): model, RBAC, notifications, UI and API.

The feature is a concrete workflow instrument plus a child attendance register:

* the **model** — a :class:`Meeting` created through the shared machine, with its
  meeting types, scheduling facts and the derived calendar state;
* the **register** — a :class:`MeetingAttendance` row per invitee, carrying their
  RSVP and the attendance recorded during or after the sitting, audited on both
  the domain event log and ``auditlog``;
* **RBAC** — an invitee may view the meeting they are on the register for, and a
  member of the owning unit holding its create role may create one;
* **notifications** — creation, status changes and document changes each raise an
  alert to the meeting's audience;
* the **UI and API** — the calendar dashboard, the meeting CRUD, the attendance
  actions and the REST endpoints (including "my meetings by membership").

The ``Meeting`` workflow type, its states/transitions and the development RBAC
(``ICT: IT Systems And Application Support Unit`` owning it, ``ECM Analyst
Programmer`` able to create) are seeded by migration ``0050``, so the tests read
the seeded rows rather than rebuilding them.
"""

import csv
import io
import tempfile
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Attachment,
    Building,
    EventType,
    Group,
    GroupMembership,
    Meeting,
    MeetingAttendance,
    Notification,
    Role,
    TransitionLog,
    User,
    Venue,
    WorkflowEvent,
    WorkflowType,
)
from .models.meetings import DEFAULT_MEETING_COLOR, MEETING_TYPE_COLORS
from .notifications import notify_meeting_created
from .services import attachments as attachments_service
from .services.meetings_io import HEADERS
from .services.permissions import EDIT, VIEW, resolve, visible_instances
from .utils.richtext import render_rich_text, rich_text_to_text, sanitize_html

ICT_GROUP_NAME = "ICT: IT Systems And Application Support Unit"
ECM_ROLE_NAME = "ECM Analyst Programmer"


class RichTextSanitizerTests(SimpleTestCase):
    """The agenda allowlist: keep the editor's vocabulary, drop everything else."""

    def test_scripts_and_their_content_are_removed(self):
        cleaned = sanitize_html(
            "<p>Hello <strong>world</strong></p><script>alert(1)</script>"
        )
        self.assertIn("<strong>world</strong>", cleaned)
        self.assertNotIn("<script", cleaned)
        self.assertNotIn("alert(1)", cleaned)

    def test_event_handlers_and_unsafe_urls_are_dropped(self):
        cleaned = sanitize_html(
            '<a href="javascript:alert(1)" onclick="x()">x</a><img src="x" onerror="y">'
        )
        self.assertNotIn("javascript:", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertNotIn("onerror", cleaned)
        self.assertNotIn("<img", cleaned)

    def test_safe_and_relative_links_survive(self):
        cleaned = sanitize_html(
            '<a href="https://parliament.gov.za" title="P">P</a>'
            ' <a href="/pwms/meetings/">M</a>'
        )
        self.assertIn('href="https://parliament.gov.za"', cleaned)
        self.assertIn('href="/pwms/meetings/"', cleaned)

    def test_plain_text_keeps_its_line_breaks(self):
        self.assertEqual(render_rich_text("line one\nline two"), "line one<br>line two")

    def test_rich_text_to_text_strips_markup(self):
        self.assertEqual(rich_text_to_text("<p>Hello <b>all</b></p>"), "Hello all")


class MeetingFixture(TestCase):
    """The seeded Meeting type, an organiser in the owning unit, and an invitee."""

    @classmethod
    def setUpTestData(cls):
        cls.meeting_type = WorkflowType.objects.get(name="Meeting")
        # The owning group and create role are wired by migration 0050 when the
        # Oracle sync has already created them; a test database has neither, so
        # they are created here the way the migration would find them.
        cls.ict_group, _ = Group.objects.get_or_create(
            name=ICT_GROUP_NAME, parent=None, defaults={"group_type": "section"}
        )
        cls.ecm_role, _ = Role.objects.get_or_create(
            name=ECM_ROLE_NAME,
            defaults={"description": "Enterprise Content Management analyst"},
        )
        cls.meeting_type.group = cls.ict_group
        cls.meeting_type.save(update_fields=["group"])
        cls.meeting_type.create_roles.add(cls.ecm_role)

        cls.organiser = User.objects.create_user(
            username="organiser", email="organiser@example.com", password="pw"
        )
        GroupMembership.objects.create(
            user=cls.organiser, group=cls.ict_group, role=cls.ecm_role
        )
        cls.attendee = User.objects.create_user(
            username="attendee", email="attendee@example.com", password="pw"
        )
        cls.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="pw"
        )

        cls.scheduled = cls.meeting_type.get_initial_state()
        cls.in_progress = cls.meeting_type.states.get(name="In Progress")
        cls.cancelled = cls.meeting_type.states.get(name="Cancelled")
        cls.start = cls.meeting_type.transitions.get(name="Start meeting")

    def _meeting(self, **overrides):
        fields = {
            "workflow_type": self.meeting_type,
            "current_state": self.scheduled,
            "owner": self.organiser,
            "title": "Portfolio Committee on Health",
            "meeting_type": "committee",
            "reference_number": "NA/PC-HEALTH/2026-11-05",
            "start_datetime": timezone.now() + timedelta(days=2),
            "end_datetime": timezone.now() + timedelta(days=2, hours=2),
            "off_site_venue": "Cape Town Lodge",
        }
        fields.update(overrides)
        return Meeting.objects.create(**fields)


class SeededMeetingWorkflowTests(TestCase):
    """Migration 0050 seeds the machine, the event types and the dev RBAC."""

    def test_meeting_workflow_type_and_machine_are_seeded(self):
        workflow_type = WorkflowType.objects.get(name="Meeting")
        self.assertTrue(workflow_type.enabled)
        states = list(
            workflow_type.states.order_by("order").values_list("name", flat=True)
        )
        self.assertEqual(
            states,
            ["Scheduled", "In Progress", "Completed", "Postponed", "Cancelled"],
        )
        self.assertEqual(workflow_type.get_initial_state().name, "Scheduled")
        self.assertTrue(workflow_type.states.get(name="Completed").is_terminal)
        self.assertTrue(workflow_type.states.get(name="Cancelled").is_terminal)
        transitions = set(workflow_type.transitions.values_list("name", flat=True))
        self.assertIn("Start meeting", transitions)
        self.assertIn("Postpone meeting", transitions)
        # Postponing/cancelling must capture a reason.
        self.assertTrue(
            workflow_type.transitions.get(name="Postpone meeting").requires_comment
        )

    def test_meeting_event_types_are_seeded(self):
        for slug in (
            "meeting-attendance-recorded",
            "meeting-minutes-filed",
            "meeting-minutes-approved",
        ):
            self.assertTrue(EventType.objects.filter(slug=slug).exists(), slug)


class PremisesTests(MeetingFixture):
    """The building/venue catalogue and the meeting's on/off-premises venue + mode."""

    def test_seeded_premises_are_available(self):
        self.assertTrue(Building.objects.filter(name="Marks Building").exists())
        self.assertTrue(Venue.objects.filter(building__name="Marks Building").exists())

    def test_on_premises_venue_display_and_mode(self):
        venue = Venue.objects.filter(building__name="Marks Building").first()
        meeting = self._meeting(
            venue=venue, off_site_venue="", attendance_mode="on_premise"
        )
        self.assertIn("Marks Building", meeting.venue_display)
        self.assertIn(venue.name, meeting.venue_display)
        self.assertTrue(meeting.is_on_premise)
        self.assertFalse(meeting.is_virtual)

    def test_off_premises_venue_display(self):
        meeting = self._meeting(
            venue=None,
            off_site_venue="Stellenbosch Town Hall",
            off_site_address="12 Plein Street",
        )
        self.assertEqual(
            meeting.venue_display, "Stellenbosch Town Hall, 12 Plein Street"
        )

    def test_attendance_mode_drives_the_virtual_and_hybrid_flags(self):
        virtual = self._meeting(attendance_mode="virtual")
        hybrid = self._meeting(attendance_mode="hybrid")
        self.assertTrue(virtual.is_virtual)
        self.assertFalse(virtual.is_hybrid)
        self.assertTrue(hybrid.is_hybrid)
        self.assertFalse(hybrid.is_virtual)

    def test_form_offers_active_venues_and_the_off_site_fields(self):
        from .forms import MeetingForm

        venue = Venue.objects.filter(building__name="Marks Building").first()
        form = MeetingForm(user=self.organiser)
        self.assertIn(venue, form.fields["venue"].queryset)
        self.assertIn("off_site_venue", form.fields)
        self.assertIn("off_site_address", form.fields)
        self.assertIn("attendance_mode", form.fields)

    def test_api_exposes_the_mode_and_venue_display(self):
        venue = Venue.objects.filter(building__name="Marks Building").first()
        meeting = self._meeting(venue=venue)
        self.client.force_login(self.organiser)
        response = self.client.get(f"/api/meetings/{meeting.public_id}/")
        payload = response.json()
        self.assertEqual(payload["attendance_mode"], "on_premise")
        self.assertEqual(payload["attendance_mode_label"], "On the premises")
        self.assertIn("Marks Building", payload["venue_display"])


class MeetingModelTests(MeetingFixture):
    """Scheduling facts, attendance tallying and the auditable register."""

    def test_identifier_and_absolute_url_follow_the_shared_base(self):
        meeting = self._meeting()
        self.assertEqual(meeting.identifier, "NA/PC-HEALTH/2026-11-05")
        self.assertEqual(
            meeting.get_absolute_url(),
            reverse("pwms:meeting_detail", kwargs={"public_id": meeting.public_id}),
        )

    def test_calendar_state_reflects_time_and_terminal_state(self):
        upcoming = self._meeting(start_datetime=timezone.now() + timedelta(days=1))
        self.assertEqual(upcoming.calendar_state, "scheduled")

        running = self._meeting(
            start_datetime=timezone.now() - timedelta(hours=1),
            end_datetime=timezone.now() + timedelta(hours=1),
        )
        self.assertEqual(running.calendar_state, "running")

        finished = self._meeting(
            start_datetime=timezone.now() - timedelta(days=2),
            end_datetime=timezone.now() - timedelta(days=2, hours=-2),
        )
        self.assertEqual(finished.calendar_state, "past")

        cancelled = self._meeting(current_state=self.cancelled)
        self.assertEqual(cancelled.calendar_state, "cancelled")

    def test_colour_is_mapped_from_the_meeting_type(self):
        self.assertEqual(
            self._meeting(meeting_type="plenary").color,
            MEETING_TYPE_COLORS["plenary"],
        )
        self.assertEqual(
            self._meeting(meeting_type="workshop").color,
            MEETING_TYPE_COLORS["workshop"],
        )

    def test_colour_falls_back_to_the_default_for_an_unmapped_type(self):
        self.assertEqual(
            self._meeting(meeting_type="not-a-real-type").color,
            DEFAULT_MEETING_COLOR,
        )

    def test_attendance_counts_tally_statuses_and_quorum(self):
        meeting = self._meeting(quorum=2)
        present = MeetingAttendance.objects.create(
            meeting=meeting, user=self.organiser, attendance_status="present"
        )
        MeetingAttendance.objects.create(
            meeting=meeting, user=self.attendee, attendance_status="absent"
        )
        MeetingAttendance.objects.create(
            meeting=meeting, guest_name="Guest Speaker", attendance_status="present"
        )
        counts = meeting.attendance_counts()
        self.assertEqual(counts["invited"], 3)
        self.assertEqual(counts["present"], 2)
        self.assertEqual(counts["absent"], 1)
        self.assertTrue(counts["quorum_met"])
        # The row is a real record, not a snapshot.
        self.assertEqual(str(present.meeting), str(meeting))

    def test_recording_attendance_is_audited_and_emits_an_event(self):
        meeting = self._meeting()
        attendance = MeetingAttendance.objects.create(
            meeting=meeting, user=self.attendee
        )
        attendance.record(
            "present", actor=self.organiser, ip_address="10.0.0.9", note="arrived"
        )
        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_status, "present")
        self.assertEqual(attendance.recorded_by, self.organiser)
        self.assertIsNotNone(attendance.recorded_at)
        self.assertEqual(attendance.recorded_ip, "10.0.0.9")
        self.assertEqual(attendance.note, "arrived")

        # The domain event log carries the fact...
        self.assertTrue(
            WorkflowEvent.objects.filter(
                event_type__slug="meeting-attendance-recorded",
                content_type=ContentType.objects.get_for_model(meeting),
                object_id=meeting.pk,
            ).exists()
        )
        # ...and auditlog keeps a field-level trail of the row itself.
        attendance_ct = ContentType.objects.get_for_model(MeetingAttendance)
        self.assertTrue(
            LogEntry.objects.filter(
                content_type=attendance_ct, object_id=attendance.pk
            ).exists()
        )

    def test_domain_event_log_is_not_written_for_other_instruments(self):
        # Only a Meeting exposes the attendance hook; a plain Model must not break.
        meeting = self._meeting()
        self.assertTrue(hasattr(meeting, "record_attendance_event"))
        self.assertTrue(hasattr(meeting, "notify_document_change"))

    def test_recording_minutes_sets_status_and_event(self):
        meeting = self._meeting()
        attachment = Attachment.objects.create(
            content_type=ContentType.objects.get_for_model(meeting),
            object_id=str(meeting.pk),
            name="Minutes 2026-11-05.pdf",
            drive_id="drive",
            item_id="item-minutes",
            type="minutes",
        )
        meeting.record_minutes_filed(actor=self.organiser, attachment=attachment)
        meeting.refresh_from_db()
        self.assertEqual(meeting.minutes_status, "filed")
        self.assertIsNotNone(meeting.minutes_filed_at)
        self.assertTrue(
            WorkflowEvent.objects.filter(
                event_type__slug="meeting-minutes-filed"
            ).exists()
        )

        meeting.record_minutes_approved(actor=self.organiser, comment="adopted")
        meeting.refresh_from_db()
        self.assertEqual(meeting.minutes_status, "approved")

    def test_for_member_matches_register_group_and_named_people(self):
        via_register = self._meeting(title="With attendee")
        MeetingAttendance.objects.create(meeting=via_register, user=self.attendee)
        via_group = self._meeting(title="Convened by the unit")
        via_group.host_group = self.ict_group
        via_group.save(update_fields=["host_group", "updated_at"])

        self.assertIn(via_register, Meeting.objects.for_member(self.attendee))
        self.assertNotIn(via_register, Meeting.objects.for_member(self.outsider))
        self.assertIn(via_group, Meeting.objects.for_member(self.organiser))
        self.assertNotIn(via_group, Meeting.objects.for_member(self.attendee))


class MeetingPermissionTests(MeetingFixture):
    """The invitee/owner grants layered over the shared group/Role/State chain."""

    def test_owner_may_view_and_edit(self):
        meeting = self._meeting()
        self.assertTrue(resolve(self.organiser, meeting, VIEW))
        self.assertTrue(resolve(self.organiser, meeting, EDIT))

    def test_invitee_may_view_but_not_edit(self):
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=self.attendee)
        self.assertTrue(resolve(self.attendee, meeting, VIEW))
        self.assertFalse(resolve(self.attendee, meeting, EDIT))

    def test_outsider_cannot_view(self):
        meeting = self._meeting()
        self.assertFalse(resolve(self.outsider, meeting, VIEW))
        self.assertNotIn(
            meeting, visible_instances(self.outsider, Meeting.objects.all())
        )

    def test_member_of_int_the_unit_may_create(self):
        self.assertTrue(self.meeting_type.can_create(self.organiser))
        self.assertFalse(self.meeting_type.can_create(self.outsider))

    def test_development_group_owns_the_type_and_role_may_create(self):
        self.assertEqual(self.meeting_type.group, self.ict_group)
        self.assertIn(self.ecm_role, self.meeting_type.create_roles.all())


class MeetingNotificationTests(MeetingFixture):
    """Creation, status and document changes alert the meeting's audience."""

    def test_meeting_created_alerts_attendees_and_organisers(self):
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=self.attendee)

        notify_meeting_created(meeting, actor=self.organiser)

        recipients = set(
            Notification.objects.filter(
                kind="meeting-created", channel="in_app"
            ).values_list("recipient_id", flat=True)
        )
        # The attendee hears about it; the actor does not hear about their own act.
        self.assertIn(self.attendee.pk, recipients)
        self.assertNotIn(self.organiser.pk, recipients)
        self.assertTrue(
            Notification.objects.filter(
                kind="meeting-created", channel="email", recipient=self.attendee
            ).exists()
        )

    def test_status_change_uses_the_meeting_kind(self):
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=self.attendee)
        meeting.perform_transition(self.start, actor=self.organiser, comment="")

        self.assertEqual(meeting.current_state, self.in_progress)
        self.assertTrue(
            TransitionLog.objects.filter(
                content_type=ContentType.objects.get_for_model(meeting),
                object_id=meeting.pk,
            ).exists()
        )
        self.assertTrue(Notification.objects.filter(kind="meeting-status").exists())
        # The generic workflow kind must not be used for a meeting.
        self.assertFalse(
            Notification.objects.filter(kind="workflow-transition").exists()
        )

    def test_document_change_alerts_the_audience(self):
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=self.attendee)
        attachment = Attachment.objects.create(
            content_type=ContentType.objects.get_for_model(meeting),
            object_id=str(meeting.pk),
            name="Order Paper.pdf",
            drive_id="drive",
            item_id="item-op",
            type="agenda",
        )
        meeting.notify_document_change("attached", attachment, actor=self.organiser)

        row = Notification.objects.filter(kind="meeting-document").first()
        self.assertIsNotNone(row)
        self.assertIn("Order Paper.pdf", row.body)

    def test_minutes_document_uses_the_minutes_kind(self):
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=self.attendee)
        attachment = Attachment.objects.create(
            content_type=ContentType.objects.get_for_model(meeting),
            object_id=str(meeting.pk),
            name="Minutes.pdf",
            drive_id="drive",
            item_id="item-minutes-2",
            type="minutes",
        )
        meeting.notify_document_change("attached", attachment, actor=self.organiser)
        self.assertTrue(Notification.objects.filter(kind="meeting-minutes").exists())

    def test_attachment_service_hook_reaches_the_meeting(self):
        """The service calls the target's hook after recording the domain event."""
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=self.attendee)
        attachment = Attachment.objects.create(
            content_type=ContentType.objects.get_for_model(meeting),
            object_id=str(meeting.pk),
            name="Supporting paper.pdf",
            drive_id="drive",
            item_id="item-doc",
            type="document",
        )
        attachments_service._notify(meeting, "attached", attachment, self.organiser)
        self.assertTrue(Notification.objects.filter(kind="meeting-document").exists())


class MeetingWebViewTests(MeetingFixture):
    """The calendar dashboard, meeting CRUD and the attendance/minutes actions."""

    def test_calendar_requires_sign_in(self):
        response = self.client.get(reverse("pwms:meetings"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("pwms:login"), response.url)

    def test_calendar_renders_for_a_signed_in_reader(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        response = self.client.get(reverse("pwms:meetings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, meeting.title)

    def test_calendar_json_is_permission_scoped(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        response = self.client.get(reverse("pwms:meeting_calendar_json"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["public_id"], str(meeting.public_id))

        self.client.force_login(self.outsider)
        response = self.client.get(reverse("pwms:meeting_calendar_json"))
        self.assertEqual(response.json()["count"], 0)

    def test_calendar_renders_the_month_week_and_day_layouts(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        markers = {
            "month": "meeting-month__grid",
            "week": "meeting-time__grid",
            "day": "meeting-time--single",
        }
        for view, marker in markers.items():
            response = self.client.get(reverse("pwms:meetings"), {"view": view})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["view"], view)
            self.assertContains(response, meeting.title)
            self.assertContains(response, marker)

    def test_calendar_falls_back_to_the_month_for_an_unknown_view(self):
        self.client.force_login(self.organiser)
        response = self.client.get(reverse("pwms:meetings"), {"view": "decade"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["view"], "month")

    def test_next_up_lists_the_soonest_meetings_first(self):
        now = timezone.now()
        self._meeting(
            title="Later upcoming",
            start_datetime=now + timedelta(days=10),
            end_datetime=now + timedelta(days=10, hours=1),
        )
        self._meeting(
            title="Sooner upcoming",
            start_datetime=now + timedelta(days=1),
            end_datetime=now + timedelta(days=1, hours=1),
        )
        self.client.force_login(self.organiser)
        response = self.client.get(reverse("pwms:meetings"))
        titles = [m.title for m in response.context["upcoming_meetings"]]
        self.assertEqual(titles[0], "Sooner upcoming")
        self.assertIn("Later upcoming", titles)

    def test_week_view_places_a_timed_meeting_on_the_axis(self):
        meeting = self._meeting()
        day = timezone.localtime(meeting.start_datetime).date()
        self.client.force_login(self.organiser)
        response = self.client.get(
            reverse("pwms:meetings"), {"view": "week", "day": day.isoformat()}
        )
        self.assertContains(response, "meeting-event")
        self.assertContains(response, f"--event-color: {meeting.color}")

    def test_calendar_week_starts_on_monday_by_default(self):
        self.client.force_login(self.organiser)
        anchor = date(2026, 11, 12)
        response = self.client.get(
            reverse("pwms:meetings"), {"view": "week", "day": anchor.isoformat()}
        )
        self.assertEqual(response.context["week_start"], 0)
        self.assertEqual(response.context["weekdays"][0], "Mon")
        days = response.context["days"]
        self.assertEqual(days[0]["date"].weekday(), 0)
        self.assertEqual(len(days), 7)
        self.assertLessEqual(days[0]["date"], anchor)
        self.assertGreaterEqual(days[-1]["date"], anchor)

    @override_settings(MEETING_CALENDAR_WEEK_START="sunday")
    def test_calendar_week_start_follows_the_setting(self):
        self.client.force_login(self.organiser)
        anchor = date(2026, 11, 12)
        response = self.client.get(
            reverse("pwms:meetings"), {"view": "week", "day": anchor.isoformat()}
        )
        self.assertEqual(response.context["week_start"], 6)
        self.assertEqual(response.context["weekdays"][0], "Sun")
        days = response.context["days"]
        self.assertEqual(days[0]["date"].weekday(), 6)
        self.assertLessEqual(days[0]["date"], anchor)
        self.assertGreaterEqual(days[-1]["date"], anchor)

    def test_calendar_week_start_can_be_overridden_per_request(self):
        self.client.force_login(self.organiser)
        response = self.client.get(
            reverse("pwms:meetings"), {"view": "month", "week_start": "sunday"}
        )
        self.assertEqual(response.context["week_start"], 6)
        self.assertEqual(response.context["weekdays"][0], "Sun")
        self.assertEqual(response.context["weekdays"][-1], "Sat")

    def test_calendar_ignores_an_invalid_week_start(self):
        self.client.force_login(self.organiser)
        response = self.client.get(
            reverse("pwms:meetings"), {"view": "week", "week_start": "funday"}
        )
        self.assertEqual(response.context["week_start"], 0)

    def test_calendar_search_matches_a_venue_name(self):
        building = Building.objects.create(name="Calendar Test Building")
        venue = Venue.objects.create(name="Calendar Test Chamber", building=building)
        meeting = self._meeting(venue=venue, off_site_venue="")
        self.client.force_login(self.organiser)
        response = self.client.get(
            reverse("pwms:meetings"), {"q": "Calendar Test Chamber"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, meeting.title)

    def test_calendar_json_feed_is_week_aligned_and_reports_the_week_start(self):
        self.client.force_login(self.organiser)
        payload = self.client.get(
            reverse("pwms:meeting_calendar_json"), {"month": "2026-11"}
        ).json()
        self.assertEqual(payload["week_start"], 0)
        self.assertEqual(payload["week_start_label"], "Monday")
        start = date.fromisoformat(payload["start"])
        end = date.fromisoformat(payload["end"])
        # Monday-first weeks open on a Monday and close on a Sunday, and the
        # window spans the whole of November 2026 (as the grid draws it).
        self.assertEqual(start.weekday(), 0)
        self.assertEqual(end.weekday(), 6)
        self.assertLessEqual(start, date(2026, 11, 1))
        self.assertGreaterEqual(end, date(2026, 11, 30))

    def test_calendar_json_feed_honours_the_week_start(self):
        self.client.force_login(self.organiser)
        payload = self.client.get(
            reverse("pwms:meeting_calendar_json"),
            {"month": "2026-11", "week_start": "sunday"},
        ).json()
        self.assertEqual(payload["week_start"], 6)
        self.assertEqual(payload["week_start_label"], "Sunday")
        # Sunday-first weeks open on a Sunday and close on the Saturday.
        self.assertEqual(date.fromisoformat(payload["start"]).weekday(), 6)
        self.assertEqual(date.fromisoformat(payload["end"]).weekday(), 5)

    def test_calendar_json_orders_meetings_chronologically(self):
        tz = timezone.get_current_timezone()
        self._meeting(
            title="Second sitting",
            start_datetime=datetime(2026, 11, 10, 14, 0, tzinfo=tz),
            end_datetime=datetime(2026, 11, 10, 15, 0, tzinfo=tz),
        )
        self._meeting(
            title="First sitting",
            start_datetime=datetime(2026, 11, 3, 9, 0, tzinfo=tz),
            end_datetime=datetime(2026, 11, 3, 10, 0, tzinfo=tz),
        )
        self.client.force_login(self.organiser)
        payload = self.client.get(
            reverse("pwms:meeting_calendar_json"), {"month": "2026-11"}
        ).json()
        self.assertEqual(
            [entry["title"] for entry in payload["results"]],
            ["First sitting", "Second sitting"],
        )

    def test_day_panel_lists_a_days_meetings_by_time(self):
        tz = timezone.get_current_timezone()
        self._meeting(
            title="Afternoon sitting",
            start_datetime=datetime(2026, 11, 10, 14, 0, tzinfo=tz),
            end_datetime=datetime(2026, 11, 10, 15, 0, tzinfo=tz),
        )
        self._meeting(
            title="Morning sitting",
            start_datetime=datetime(2026, 11, 10, 9, 0, tzinfo=tz),
            end_datetime=datetime(2026, 11, 10, 10, 0, tzinfo=tz),
        )
        self.client.force_login(self.organiser)
        response = self.client.get(
            reverse("pwms:meetings"),
            {"view": "month", "month": "2026-11", "day": "2026-11-10"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [m.title for m in response.context["selected_meetings"]],
            ["Morning sitting", "Afternoon sitting"],
        )

    def test_calendar_json_carries_the_colour_and_time_label(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        entry = self.client.get(reverse("pwms:meeting_calendar_json")).json()[
            "results"
        ][0]
        self.assertEqual(entry["color"], meeting.color)
        start = timezone.localtime(meeting.start_datetime)
        end = timezone.localtime(meeting.end_datetime)
        self.assertEqual(entry["time_label"], f"{start:%H:%M}\u2013{end:%H:%M}")
        self.assertIn("Committee Meeting", entry["subtitle"])

    def test_create_prefills_the_schedule_from_a_calendar_date(self):
        self.client.force_login(self.organiser)
        response = self.client.get(
            reverse("pwms:meeting_create"), {"date": "2026-11-12", "time": "14:30"}
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        start = form.initial["start_datetime"]
        self.assertEqual(
            timezone.localtime(start).strftime("%Y-%m-%d %H:%M"), "2026-11-12 14:30"
        )
        self.assertEqual(
            timezone.localtime(form.initial["end_datetime"])
            - timezone.localtime(start),
            timedelta(hours=2),
        )

    def test_create_meeting(self):
        # A colleague in the owning unit so the creation alert has a recipient
        # other than the actor (nobody is told about their own act).
        colleague = User.objects.create_user(
            username="colleague", email="colleague@example.com", password="pw"
        )
        GroupMembership.objects.create(
            user=colleague, group=self.ict_group, role=self.ecm_role
        )
        self.client.force_login(self.organiser)
        response = self.client.post(
            reverse("pwms:meeting_create"),
            {
                "workflow_type": str(self.meeting_type.pk),
                "title": "House Plenary",
                "meeting_type": "plenary",
                "attendance_mode": "on_premise",
                "start_datetime": "2026-11-12 09:30",
                "priority": "high",
            },
        )
        self.assertEqual(response.status_code, 302)
        meeting = Meeting.objects.get(title="House Plenary")
        self.assertEqual(meeting.owner, self.organiser)
        self.assertEqual(meeting.current_state, self.scheduled)
        # Creation raises the alert to the unit's officers.
        self.assertTrue(
            Notification.objects.filter(
                kind="meeting-created", channel="in_app", recipient=colleague
            ).exists()
        )

    def test_create_is_refused_to_a_user_without_the_create_role(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("pwms:meeting_create"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("pwms:meetings"))

    def test_detail_requires_view_permission(self):
        meeting = self._meeting()
        url = reverse("pwms:meeting_detail", kwargs={"public_id": meeting.public_id})
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.organiser)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_attendance_can_be_added_and_recorded(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        add_url = reverse(
            "pwms:meeting_attendance_add", kwargs={"public_id": meeting.public_id}
        )
        response = self.client.post(
            add_url, {"user": str(self.attendee.pk), "capacity": "member"}
        )
        self.assertEqual(response.status_code, 200)
        attendance = meeting.attendances.get(user=self.attendee)

        record_url = reverse(
            "pwms:meeting_attendance_record",
            kwargs={
                "public_id": meeting.public_id,
                "attendance_id": attendance.public_id,
            },
        )
        prefix = f"a{attendance.pk}"
        response = self.client.post(
            record_url,
            {
                f"{prefix}-rsvp_status": "accepted",
                f"{prefix}-attendance_status": "present",
                f"{prefix}-note": "checked in",
            },
        )
        self.assertEqual(response.status_code, 200)
        attendance.refresh_from_db()
        self.assertEqual(attendance.rsvp_status, "accepted")
        self.assertEqual(attendance.attendance_status, "present")
        self.assertEqual(attendance.recorded_by, self.organiser)

    def test_minutes_status_can_be_advanced(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        url = reverse(
            "pwms:meeting_minutes_status", kwargs={"public_id": meeting.public_id}
        )
        response = self.client.post(url, {"status": "filed"})
        self.assertEqual(response.status_code, 200)
        meeting.refresh_from_db()
        self.assertEqual(meeting.minutes_status, "filed")

    def test_instrument_document_renders_for_a_meeting(self):
        # The shared instrument route spans the registry, so a meeting must shape
        # into the formal document rather than raising a KeyError.
        meeting = self._meeting()
        MeetingAttendance.objects.create(
            meeting=meeting, user=self.attendee, attendance_status="present"
        )
        self.client.force_login(self.organiser)
        response = self.client.get(
            f"/pwms/instruments/{meeting.public_id}/document/?format=html"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, meeting.title)
        self.assertContains(response, "Attendance register")

    def test_detail_renders_sanitised_agenda_and_the_upload_button(self):
        meeting = self._meeting(
            agenda_summary="<p>Item one</p><script>alert(1)</script>"
        )
        self.client.force_login(self.organiser)
        response = self.client.get(
            reverse("pwms:meeting_detail", kwargs={"public_id": meeting.public_id})
        )
        self.assertEqual(response.status_code, 200)
        # The agenda is rendered as HTML (sanitised), not escaped text.
        self.assertContains(response, "<p>Item one</p>")
        self.assertContains(response, "Upload agenda")
        # The unsafe fragment is dropped, not echoed into the page.
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_meeting_form_exposes_the_rich_text_editor(self):
        self.client.force_login(self.organiser)
        response = self.client.get(reverse("pwms:meeting_create"))
        self.assertContains(response, "data-rich-text")

    def test_attendance_register_is_collapsed_by_default_and_opens_on_a_change(self):
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=self.attendee)
        self.client.force_login(self.organiser)

        detail = self.client.get(
            reverse("pwms:meeting_detail", kwargs={"public_id": meeting.public_id})
        )
        self.assertEqual(detail.status_code, 200)
        # The register starts collapsed...
        self.assertContains(detail, 'class="collapse" id="meeting-attendance-register"')
        self.assertNotContains(
            detail, 'class="collapse show" id="meeting-attendance-register"'
        )
        # ...but the header, the hosting note and the counts stay visible.
        self.assertContains(detail, "Host group")
        self.assertContains(detail, "Not recorded")
        self.assertContains(detail, "Attendance register")

        # A change re-renders the fragment expanded so the table is not hidden.
        response = self.client.post(
            reverse(
                "pwms:meeting_attendance_add", kwargs={"public_id": meeting.public_id}
            ),
            {"guest_name": "Guest Two", "capacity": "guest"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'class="collapse show" id="meeting-attendance-register"'
        )

    def test_agenda_inline_editor_loads_and_saves(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        edit_url = reverse(
            "pwms:meeting_agenda_edit", kwargs={"public_id": meeting.public_id}
        )

        editor = self.client.get(edit_url)
        self.assertEqual(editor.status_code, 200)
        self.assertContains(editor, "data-rich-text")

        saved = self.client.post(
            edit_url,
            {"agenda_summary": "<p>Item one</p><script>alert(1)</script>"},
        )
        self.assertEqual(saved.status_code, 200)
        meeting.refresh_from_db()
        self.assertEqual(
            meeting.agenda_summary, "<p>Item one</p><script>alert(1)</script>"
        )
        # The panel is re-rendered with the agenda sanitised, not raw.
        self.assertContains(saved, "<p>Item one</p>")
        self.assertNotContains(saved, "<script>alert(1)</script>")

    def test_agenda_read_partial_renders_the_agenda(self):
        meeting = self._meeting(agenda_summary="<p>Standing items</p>")
        self.client.force_login(self.organiser)
        response = self.client.get(
            reverse("pwms:meeting_agenda", kwargs={"public_id": meeting.public_id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Standing items")

    def test_agenda_edit_requires_the_edit_capability(self):
        meeting = self._meeting()
        self.client.force_login(self.outsider)
        url = reverse(
            "pwms:meeting_agenda_edit", kwargs={"public_id": meeting.public_id}
        )
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(
            self.client.post(url, {"agenda_summary": "x"}).status_code, 403
        )
        meeting.refresh_from_db()
        self.assertEqual(meeting.agenda_summary, "")


class AttendanceGenerationTests(MeetingFixture):
    """The register = host-group members (generated) + invitees (by hand)."""

    def _member(self, username):
        user = User.objects.create_user(
            username=username, email=f"{username}@example.com", password="pw"
        )
        GroupMembership.objects.create(
            user=user, group=self.ict_group, role=self.ecm_role
        )
        return user

    def test_regenerate_generates_members_and_keeps_invitees(self):
        self._member("hostcolleague")
        meeting = self._meeting(host_group=self.ict_group)
        guest = MeetingAttendance.objects.create(
            meeting=meeting, guest_name="External Expert"
        )

        summary = meeting.regenerate_attendance(actor=self.organiser)

        generated = set(
            meeting.attendances.filter(source="host_group").values_list(
                "user__username", flat=True
            )
        )
        self.assertEqual(generated, {"organiser", "hostcolleague"})
        self.assertEqual(summary["added"], 2)
        guest.refresh_from_db()
        self.assertEqual(guest.source, "invitee")
        self.assertIsNotNone(meeting.register_generated_at)

    def test_regenerate_is_idempotent(self):
        self._member("hostidem")
        meeting = self._meeting(host_group=self.ict_group)
        meeting.regenerate_attendance()
        before = meeting.attendances.count()

        summary = meeting.regenerate_attendance()

        self.assertEqual(summary["added"], 0)
        self.assertEqual(summary["removed"], 0)
        self.assertEqual(meeting.attendances.count(), before)

    def test_regenerate_removes_members_who_left_the_group(self):
        colleague = self._member("hostleft")
        meeting = self._meeting(host_group=self.ict_group)
        meeting.regenerate_attendance()
        self.assertTrue(meeting.attendances.filter(user=colleague).exists())

        colleague.memberships.update(is_active=False)
        summary = meeting.regenerate_attendance()

        self.assertEqual(summary["removed"], 1)
        self.assertFalse(meeting.attendances.filter(user=colleague).exists())

    def test_a_host_member_invited_by_hand_is_not_duplicated(self):
        colleague = self._member("hostinvited")
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=colleague)
        meeting.host_group = self.ict_group
        meeting.save(update_fields=["host_group", "updated_at"])

        meeting.regenerate_attendance()

        rows = meeting.attendances.filter(user=colleague)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().source, "invitee")

    def test_counts_split_host_group_and_invitees(self):
        self._member("hostcount")
        meeting = self._meeting(host_group=self.ict_group)
        MeetingAttendance.objects.create(meeting=meeting, guest_name="Guest")
        meeting.regenerate_attendance()

        counts = meeting.attendance_counts()

        self.assertEqual(counts["host_group"], 2)
        self.assertEqual(counts["invitee"], 1)
        self.assertEqual(counts["invited"], 3)

    def test_regenerate_view_rebuilds_the_register(self):
        self._member("hostview")
        meeting = self._meeting(host_group=self.ict_group)
        self.client.force_login(self.organiser)

        response = self.client.post(
            reverse(
                "pwms:meeting_attendance_regenerate",
                kwargs={"public_id": meeting.public_id},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(meeting.attendances.filter(source="host_group").count(), 2)

    def test_regenerate_view_reports_a_missing_host_group(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        response = self.client.post(
            reverse(
                "pwms:meeting_attendance_regenerate",
                kwargs={"public_id": meeting.public_id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No hosting group")

    def test_create_view_generates_the_register(self):
        self._member("hostcreate")
        self.client.force_login(self.organiser)
        response = self.client.post(
            reverse("pwms:meeting_create"),
            {
                "workflow_type": str(self.meeting_type.pk),
                "title": "Generated register",
                "meeting_type": "committee",
                "attendance_mode": "on_premise",
                "host_group": str(self.ict_group.pk),
                "start_datetime": "2026-11-20 09:00",
                "priority": "medium",
            },
        )
        self.assertEqual(response.status_code, 302)
        meeting = Meeting.objects.get(title="Generated register")
        self.assertEqual(meeting.attendances.filter(source="host_group").count(), 2)

    def test_api_create_generates_and_regenerate_keeps_invitees(self):
        self._member("hostapi")
        self.client.force_login(self.organiser)
        create = self.client.post(
            "/api/meetings/",
            {
                "workflow_type": str(self.meeting_type.pk),
                "title": "API meeting",
                "meeting_type": "committee",
                "attendance_mode": "on_premise",
                "host_group": self.ict_group.pk,
                "start_datetime": (timezone.now() + timedelta(days=1)).isoformat(),
            },
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 201)
        meeting = Meeting.objects.get(title="API meeting")
        self.assertEqual(meeting.attendances.filter(source="host_group").count(), 2)

        invitee = MeetingAttendance.objects.create(
            meeting=meeting, guest_name="API Guest"
        )
        regenerate = self.client.post(
            f"/api/meetings/{meeting.public_id}/attendance/regenerate/",
            content_type="application/json",
        )
        self.assertEqual(regenerate.status_code, 200)
        self.assertTrue(meeting.attendances.filter(pk=invitee.pk).exists())


class MeetingApiTests(MeetingFixture):
    """The REST endpoints: list, detail, membership search, attendance, minutes."""

    def test_list_is_permission_scoped(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        response = self.client.get("/api/meetings/")
        self.assertEqual(response.status_code, 200)
        ids = [row["public_id"] for row in response.json()["results"]]
        self.assertIn(str(meeting.public_id), ids)

        self.client.force_login(self.outsider)
        response = self.client.get("/api/meetings/")
        self.assertEqual(response.json()["count"], 0)

    def test_detail_endpoint_carries_attendance_and_summary(self):
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=self.attendee)
        self.client.force_login(self.organiser)
        response = self.client.get(f"/api/meetings/{meeting.public_id}/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["meeting_type"], "committee")
        self.assertEqual(payload["attendance_summary"]["invited"], 1)
        self.assertEqual(len(payload["attendance"]), 1)

    def test_mine_returns_meetings_by_membership(self):
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=self.attendee)
        self.client.force_login(self.attendee)
        response = self.client.get("/api/meetings/mine/")
        self.assertEqual(response.status_code, 200)
        ids = [row["public_id"] for row in response.json()["results"]]
        self.assertIn(str(meeting.public_id), ids)

    def test_member_filter_searches_by_username(self):
        meeting = self._meeting()
        MeetingAttendance.objects.create(meeting=meeting, user=self.attendee)
        self.client.force_login(self.organiser)
        response = self.client.get("/api/meetings/?member=attendee")
        ids = [row["public_id"] for row in response.json()["results"]]
        self.assertIn(str(meeting.public_id), ids)

    def test_attendance_add_and_record_actions(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        add = self.client.post(
            f"/api/meetings/{meeting.public_id}/attendance/add/",
            {"user": self.attendee.pk, "capacity": "member"},
            content_type="application/json",
        )
        self.assertEqual(add.status_code, 201)
        attendance = meeting.attendances.get(user=self.attendee)

        record = self.client.post(
            f"/api/meetings/{meeting.public_id}/attendance/record/",
            {
                "attendance": str(attendance.public_id),
                "attendance_status": "present",
            },
            content_type="application/json",
        )
        self.assertEqual(record.status_code, 200)
        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_status, "present")
        self.assertEqual(attendance.recorded_by, self.organiser)

    def test_minutes_action_advances_the_status(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        response = self.client.post(
            f"/api/meetings/{meeting.public_id}/minutes/",
            {"status": "filed"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        meeting.refresh_from_db()
        self.assertEqual(meeting.minutes_status, "filed")


class MeetingImportExportTests(MeetingFixture):
    """CSV/XLSX export and import of meetings (``pwms.services.meetings_io``)."""

    def _csv_bytes(self, records):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(HEADERS)
        for record in records:
            writer.writerow([record.get(header, "") for header in HEADERS])
        return buffer.getvalue().encode("utf-8-sig")

    def _upload(self, content, name="meetings.csv", **extra):
        upload = SimpleUploadedFile(name, content, content_type="text/csv")
        data = {"file": upload}
        data.update(extra)
        return self.client.post(reverse("pwms:meeting_import"), data)

    def _write(self, content, suffix=".csv"):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / f"meetings{suffix}"
        path.write_bytes(content)
        return path

    # -- export ------------------------------------------------------------
    def test_export_csv_writes_the_header_and_the_meetings(self):
        meeting = self._meeting()
        self.client.force_login(self.organiser)
        response = self.client.get(reverse("pwms:meeting_export"), {"format": "csv"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(rows[0], list(HEADERS))
        self.assertIn(meeting.title, [row[1] for row in rows[1:]])

    def test_export_xlsx_is_a_readable_workbook(self):
        from openpyxl import load_workbook

        meeting = self._meeting()
        self.client.force_login(self.organiser)
        response = self.client.get(reverse("pwms:meeting_export"), {"format": "xlsx"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        workbook = load_workbook(io.BytesIO(response.content))
        sheet = workbook.active
        self.assertEqual([cell.value for cell in sheet[1]], list(HEADERS))
        titles = [row[1] for row in sheet.iter_rows(min_row=2, values_only=True)]
        self.assertIn(meeting.title, titles)

    def test_export_is_permission_scoped(self):
        self._meeting()
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("pwms:meeting_export"), {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(rows, [list(HEADERS)])

    def test_export_honours_the_type_filter(self):
        self._meeting(title="Committee", meeting_type="committee")
        self._meeting(title="Plenary", meeting_type="plenary")
        self.client.force_login(self.organiser)
        response = self.client.get(
            reverse("pwms:meeting_export"), {"format": "csv", "type": "plenary"}
        )
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual({row[1] for row in rows[1:]}, {"Plenary"})

    def test_export_rejects_an_unknown_format(self):
        self.client.force_login(self.organiser)
        response = self.client.get(reverse("pwms:meeting_export"), {"format": "pdf"})
        self.assertEqual(response.status_code, 400)

    # -- template ----------------------------------------------------------
    def test_template_csv_is_the_header_row_only(self):
        self.client.force_login(self.organiser)
        response = self.client.get(
            reverse("pwms:meeting_import_template"), {"format": "csv"}
        )
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(rows, [list(HEADERS)])

    # -- import ------------------------------------------------------------
    def test_import_creates_meetings_and_the_register(self):
        content = self._csv_bytes(
            [
                {
                    "Reference": "IMP/2026-11-20",
                    "Title": "Imported committee",
                    "Meeting type": "committee",
                    "Start": "2026-11-20 09:30",
                    "End": "2026-11-20 11:00",
                    "Host group": self.ict_group.name,
                    "Priority": "high",
                }
            ]
        )
        self.client.force_login(self.organiser)
        response = self._upload(content)
        self.assertEqual(response.status_code, 200)
        report = response.context["report"]
        self.assertEqual((report.created, report.updated, report.skipped), (1, 0, 0))
        meeting = Meeting.objects.get(reference_number="IMP/2026-11-20")
        self.assertEqual(meeting.title, "Imported committee")
        self.assertEqual(meeting.priority, "high")
        self.assertEqual(meeting.owner, self.organiser)
        self.assertEqual(meeting.host_group, self.ict_group)
        # The register is generated from the host group, exactly as the form does.
        self.assertEqual(
            set(meeting.attendances.values_list("user__username", flat=True)),
            {"organiser"},
        )

    def test_import_upserts_on_the_reference(self):
        meeting = self._meeting(reference_number="IMP/1", title="Original")
        content = self._csv_bytes(
            [
                {
                    "Reference": "IMP/1",
                    "Title": "Renamed",
                    "Meeting type": "committee",
                    "Start": "2026-11-20 09:30",
                }
            ]
        )
        self.client.force_login(self.organiser)
        report = self._upload(content).context["report"]
        self.assertEqual((report.created, report.updated), (0, 1))
        meeting.refresh_from_db()
        self.assertEqual(meeting.title, "Renamed")
        self.assertEqual(Meeting.objects.filter(reference_number="IMP/1").count(), 1)

    def test_import_blank_cells_leave_existing_values_untouched(self):
        meeting = self._meeting(reference_number="IMP/2", title="Keep", notes="keep me")
        content = self._csv_bytes(
            [
                {
                    "Reference": "IMP/2",
                    "Title": "Keep",
                    "Meeting type": "committee",
                    "Start": "2026-11-21 10:00",
                }
            ]
        )
        self.client.force_login(self.organiser)
        self._upload(content)
        meeting.refresh_from_db()
        self.assertEqual(meeting.notes, "keep me")

    def test_import_reports_a_bad_row_and_imports_the_rest(self):
        content = self._csv_bytes(
            [
                {
                    "Title": "Good",
                    "Meeting type": "committee",
                    "Start": "2026-11-20 09:30",
                },
                {
                    "Title": "Bad type",
                    "Meeting type": "nonsense",
                    "Start": "2026-11-20 09:30",
                },
                {
                    "Title": "Bad date",
                    "Meeting type": "committee",
                    "Start": "not-a-date",
                },
            ]
        )
        self.client.force_login(self.organiser)
        report = self._upload(content).context["report"]
        self.assertEqual(report.created, 1)
        self.assertEqual(report.skipped, 2)
        self.assertEqual(len(report.errors), 2)
        self.assertTrue(Meeting.objects.filter(title="Good").exists())

    def test_import_rejects_a_file_missing_a_required_column(self):
        buffer = io.StringIO()
        csv.writer(buffer).writerow(["Title", "Start"])  # no Meeting type
        upload = SimpleUploadedFile(
            "bad.csv", buffer.getvalue().encode(), content_type="text/csv"
        )
        self.client.force_login(self.organiser)
        response = self.client.post(reverse("pwms:meeting_import"), {"file": upload})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["report"])
        self.assertContains(response, "missing required column")

    def test_import_dry_run_changes_nothing(self):
        content = self._csv_bytes(
            [
                {
                    "Title": "Phantom",
                    "Meeting type": "committee",
                    "Start": "2026-11-20 09:30",
                }
            ]
        )
        self.client.force_login(self.organiser)
        report = self._upload(content, dry_run="on").context["report"]
        self.assertTrue(report.dry_run)
        self.assertEqual(report.created, 1)
        self.assertFalse(Meeting.objects.filter(title="Phantom").exists())

    def test_meetings_page_offers_export_and_import(self):
        self.client.force_login(self.organiser)
        response = self.client.get(reverse("pwms:meetings"))
        self.assertContains(response, reverse("pwms:meeting_export"))
        self.assertContains(response, reverse("pwms:meeting_import"))

    def test_import_page_documents_the_format(self):
        self.client.force_login(self.organiser)
        response = self.client.get(reverse("pwms:meeting_import"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import format")
        self.assertContains(response, "Meeting type")
        self.assertContains(response, "Reference")

    def test_import_requires_the_create_role(self):
        content = self._csv_bytes(
            [
                {
                    "Title": "Nope",
                    "Meeting type": "committee",
                    "Start": "2026-11-20 09:30",
                }
            ]
        )
        self.client.force_login(self.outsider)
        response = self._upload(content)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("pwms:meetings"))
        self.assertFalse(Meeting.objects.filter(title="Nope").exists())

    def test_import_reads_an_xlsx_file(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(list(HEADERS))
        record = {header: "" for header in HEADERS}
        record.update(
            {
                "Title": "From workbook",
                "Meeting type": "workshop",
                "Start": "2026-11-25 13:00",
            }
        )
        sheet.append([record[header] for header in HEADERS])
        buffer = io.BytesIO()
        workbook.save(buffer)
        upload = SimpleUploadedFile(
            "meetings.xlsx",
            buffer.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        self.client.force_login(self.organiser)
        response = self.client.post(reverse("pwms:meeting_import"), {"file": upload})
        self.assertEqual(response.context["report"].created, 1)
        self.assertTrue(Meeting.objects.filter(title="From workbook").exists())

    def test_import_resolves_an_on_premises_venue(self):
        building = Building.objects.create(name="Import Building")
        venue = Venue.objects.create(name="Room 1", building=building)
        content = self._csv_bytes(
            [
                {
                    "Title": "In a room",
                    "Meeting type": "committee",
                    "Start": "2026-11-20 09:30",
                    # A plain hyphen instead of the exporter's em dash still matches.
                    "Venue": "Import Building - Room 1",
                }
            ]
        )
        self.client.force_login(self.organiser)
        report = self._upload(content).context["report"]
        self.assertEqual(report.created, 1)
        self.assertEqual(Meeting.objects.get(title="In a room").venue, venue)

    def test_import_reports_an_unknown_venue(self):
        content = self._csv_bytes(
            [
                {
                    "Title": "Nowhere",
                    "Meeting type": "committee",
                    "Start": "2026-11-20 09:30",
                    "Venue": "Somewhere Building — Room 9",
                }
            ]
        )
        self.client.force_login(self.organiser)
        report = self._upload(content).context["report"]
        self.assertEqual(report.skipped, 1)
        self.assertIn("no on-premises room", report.errors[0])

    def test_import_command_creates_meetings(self):
        path = self._write(
            self._csv_bytes(
                [
                    {
                        "Title": "By command",
                        "Meeting type": "committee",
                        "Start": "2026-11-20 09:30",
                    }
                ]
            )
        )
        out = StringIO()
        call_command("import_meetings", str(path), actor="organiser", stdout=out)
        meeting = Meeting.objects.get(title="By command")
        self.assertEqual(meeting.owner, self.organiser)
        self.assertIn("Created:  1", out.getvalue())

    def test_import_command_dry_run_changes_nothing(self):
        path = self._write(
            self._csv_bytes(
                [
                    {
                        "Title": "Phantom",
                        "Meeting type": "committee",
                        "Start": "2026-11-20 09:30",
                    }
                ]
            )
        )
        out = StringIO()
        call_command(
            "import_meetings", str(path), "--dry-run", actor="organiser", stdout=out
        )
        self.assertFalse(Meeting.objects.filter(title="Phantom").exists())
        self.assertIn("DRY RUN", out.getvalue())

    def test_import_command_defaults_the_actor_to_a_superuser(self):
        User.objects.create_superuser(
            username="root", email="root@example.com", password="pw"
        )
        path = self._write(
            self._csv_bytes(
                [
                    {
                        "Title": "By root",
                        "Meeting type": "committee",
                        "Start": "2026-11-20 09:30",
                    }
                ]
            )
        )
        call_command("import_meetings", str(path), stdout=StringIO())
        self.assertEqual(Meeting.objects.get(title="By root").owner.username, "root")

    def test_import_command_errors_on_a_missing_file(self):
        with self.assertRaises(CommandError):
            call_command("import_meetings", "/no/such/meetings.csv", stdout=StringIO())

    def test_import_command_errors_on_a_bad_file(self):
        path = self._write(b"Title,Start\nFoo,2026-11-20\n")  # no Meeting type
        with self.assertRaises(CommandError):
            call_command(
                "import_meetings", str(path), actor="organiser", stdout=StringIO()
            )

    def test_export_then_import_round_trips(self):
        self._meeting(
            reference_number="RT/1", title="Round trip", meeting_type="workshop"
        )
        self.client.force_login(self.organiser)
        exported = self.client.get(
            reverse("pwms:meeting_export"), {"format": "csv"}
        ).content
        report = self._upload(exported).context["report"]
        # A clean round trip changes nothing: the reference matches, so the row is
        # recognised but no field differs and nothing is written or duplicated.
        self.assertEqual((report.created, report.updated, report.skipped), (0, 0, 1))
        self.assertEqual(Meeting.objects.filter(reference_number="RT/1").count(), 1)
        self.assertEqual(Meeting.objects.count(), 1)
