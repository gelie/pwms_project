"""The workflow detail page: its tabs, the unified timeline, and the diagram.

The detail page used to be one long scroll of tables. It is now a header plus
one tab per concern, with ``TransitionLog`` and the auditlog CRUD trail merged
into a single Timeline table (see :mod:`pwms.services.history`).
"""

from __future__ import annotations

import re
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Bill,
    DelegationReport,
    EventType,
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Role,
    TransitionLog,
    WorkflowNote,
    WorkflowType,
)
from .services.history import _audit_entry, instance_timeline
from .services.progress import workflow_progress
from .utils.diagrams import workflow_type_stem

#: The tabbed panes the shared detail template renders, in tab order.
DETAIL_PANES = (
    "overview",
    "related",
    "notes",
    "events",
    "attachments",
    "diagram",
    "referrals",
)

#: Reference views that open as modals from the Workflow Actions menu. They used
#: to be tabs, so they have no tab button and no ``pane-`` element any more.
DETAIL_MODALS = ("progress", "timeline")


def live_markup(response):
    """
    The rendered page with HTML comments removed.

    Markup commented out with ``<!-- -->`` in the template is still sent to the
    browser, so ``assertNotContains`` cannot tell a retired element from a live
    one. Stripping comments checks what the browser actually renders.
    """
    return re.sub(r"<!--.*?-->", "", response.content.decode(), flags=re.DOTALL)


def header_menus(response):
    """
    The two header dropdowns' items, as ``(workflow_menu, actions_menu)``.

    The first menu is the record's workflow menu (status moves, Progress,
    Timeline, type-specific workflow items); the second is the generic Actions
    menu (Share, Print, documents, Edit, Delete). Found by position rather than
    by button label so a rename does not break the tests.
    """
    menus = re.findall(
        r'<ul class="dropdown-menu dropdown-menu-end workflow-actions-menu">(.*?)</ul>',
        live_markup(response),
        flags=re.DOTALL,
    )
    assert len(menus) == 2, f"expected two header menus, found {len(menus)}"
    return menus[0], menus[1]


class DetailPageTestCase(TestCase):
    """Shared fixtures: one instance of each instrument the signed-in user sees."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="detail", email="detail@example.com", password="pw"
        )
        cls.group = Group.objects.create(
            name="Detail Test Group", group_type="portfolio_committee"
        )
        cls.role = Role.objects.create(name="Detail Test Creator")
        GroupMembership.objects.create(user=cls.user, group=cls.group, role=cls.role)
        # A type's owning group is what materialises an instance's access rows, so
        # each type has to belong to a group the fixture user is active in.
        cls.types = {}
        for name in (
            "Delegation Report",
            "International Resolution",
            "International Agreement",
            "Bill",
        ):
            workflow_type = WorkflowType.objects.get(name=name)
            workflow_type.group = cls.group
            workflow_type.save(update_fields=["group"])
            workflow_type.create_roles.add(cls.role)
            cls.types[name] = workflow_type

    def setUp(self):
        self.client.force_login(self.user)

    def make_report(self, **overrides):
        fields = {
            "workflow_type": self.types["Delegation Report"],
            "current_state": self.types["Delegation Report"].get_initial_state(),
            "title": "A report",
            "owner": self.user,
        }
        fields.update(overrides)
        return DelegationReport.objects.create(**fields)

    def make_agreement(self, **overrides):
        workflow_type = self.types["International Agreement"]
        fields = {
            "workflow_type": workflow_type,
            "current_state": workflow_type.get_initial_state(),
            "title": "An agreement",
            "owner": self.user,
        }
        fields.update(overrides)
        return InternationalAgreement.objects.create(**fields)

    def add_transition(
        self,
        instance,
        *,
        from_state=None,
        to_state=None,
        actor=None,
        notes="",
        ip="127.0.0.1",
    ):
        """A state-change row for ``instance`` (the views do not perform one yet)."""
        return TransitionLog.objects.create(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
            action="STATE_TRANSITION",
            from_state=from_state or instance.current_state,
            to_state=to_state or instance.current_state,
            actor=actor,
            ip_address=ip,
            notes=notes,
        )


class TimelineServiceTests(DetailPageTestCase):
    """``instance_timeline`` merges the two histories into one ordering."""

    def test_merges_transitions_and_crud_newest_first(self):
        report = self.make_report()
        report.title = "A renamed report"
        report.save()
        self.add_transition(report, actor=self.user)

        timeline = instance_timeline(report)

        # create -> update -> transition, so newest first is the reverse.
        self.assertEqual(
            [entry.kind for entry in timeline],
            ["transition", "update", "create"],
        )

    def test_transition_entry_carries_the_move_and_the_actor(self):
        report = self.make_report()
        self.add_transition(report, actor=self.user, notes="Tabled", ip="10.0.0.9")

        entry = instance_timeline(report)[0]

        self.assertEqual(entry.kind, "transition")
        self.assertEqual(entry.label, "State change")
        self.assertEqual(entry.actor, self.user.display_name)
        self.assertEqual(entry.from_state, report.current_state.name)
        self.assertEqual(entry.to_state, report.current_state.name)
        self.assertEqual(entry.notes, "Tabled")
        self.assertEqual(entry.ip_address, "10.0.0.9")

    def test_update_entry_lists_the_field_diff(self):
        report = self.make_report()
        report.title = "A renamed report"
        report.save()

        update = next(
            entry for entry in instance_timeline(report) if entry.kind == "update"
        )
        changed = [
            change for change in update.changes if change.after == "A renamed report"
        ]

        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].before, "A report")

    def test_actor_is_system_when_nobody_is_recorded(self):
        report = self.make_report()
        self.add_transition(report, actor=None)

        entry = next(
            item for item in instance_timeline(report) if item.kind == "transition"
        )

        self.assertEqual(entry.actor, "System")

    def test_domain_events_are_merged_into_the_timeline(self):
        report = self.make_report()
        event_type = EventType.objects.get(slug="document-attached")
        report.record_event(event_type, actor=self.user, notes="A document")

        entry = next(item for item in instance_timeline(report) if item.kind == "event")

        self.assertEqual(entry.label, event_type.name)
        self.assertEqual(entry.actor, self.user.display_name)
        self.assertEqual(entry.notes, "A document")

    def test_a_system_event_names_its_origin_and_has_no_actor(self):
        report = self.make_report()
        event_type = EventType.objects.get(slug="atc-update-published")
        report.record_event(event_type, origin="integration")

        entry = next(item for item in instance_timeline(report) if item.kind == "event")

        self.assertEqual(entry.actor, "System")
        self.assertEqual(entry.action, "Integration")

    def test_m2m_change_is_summarised(self):
        """
        An m2m diff renders as "Added: …", not as the diff dict's keys.

        M2M tracking is opt-in in auditlog and is not switched on for these
        models, so this drives ``_audit_entry`` with a stub row rather than
        relying on a real m2m write.
        """
        stub = SimpleNamespace(
            action=1,
            timestamp=timezone.now(),
            actor=None,
            remote_addr=None,
            changes={
                "referral_committees": {
                    "type": "m2m",
                    "operation": "add",
                    "objects": ["Committee X", "Committee Y"],
                }
            },
            # What auditlog's own display helper produces for an m2m dict.
            changes_display_dict={"Referral committees": ["type", "operation"]},
        )

        entry = _audit_entry(stub, self.make_agreement())

        (change,) = entry.changes
        # The label is the field's verbose name as Django derives it.
        self.assertEqual(change.field, "referral committees")
        self.assertEqual(change.before, "")
        self.assertEqual(change.after, "Added: Committee X, Committee Y")


class ProgressServiceTests(DetailPageTestCase):
    """``workflow_progress`` says where a record is, and how far is left."""

    def make_resolution(self, state_name):
        workflow_type = self.types["International Resolution"]
        return InternationalResolution.objects.create(
            workflow_type=workflow_type,
            current_state=workflow_type.states.get(name=state_name),
            title="A resolution",
            resolution_number=f"IR-PROGRESS-{state_name}",
            owner=self.user,
        )

    def make_bill(self, state_name):
        workflow_type = self.types["Bill"]
        return Bill.objects.create(
            workflow_type=workflow_type,
            current_state=workflow_type.states.get(name=state_name),
            title="A bill",
            bill_number=f"B PROGRESS {state_name}",
            owner=self.user,
        )

    def test_linear_machine_starts_at_zero(self):
        progress = workflow_progress(self.make_resolution("Captured"))

        self.assertTrue(progress.has_machine)
        self.assertEqual(progress.percent, 0)
        self.assertEqual(progress.steps_left, 4)
        self.assertEqual(progress.target, "Closed")
        self.assertFalse(progress.is_closed)
        self.assertEqual((progress.completed, progress.total), (0, 5))

    def test_progress_is_measured_to_the_terminal_state(self):
        progress = workflow_progress(self.make_resolution("In Progress"))

        self.assertEqual(progress.steps_left, 2)
        self.assertEqual(progress.percent, 50)

    def test_terminal_state_is_complete(self):
        progress = workflow_progress(self.make_resolution("Closed"))

        self.assertTrue(progress.is_closed)
        self.assertEqual(progress.steps_left, 0)
        self.assertEqual(progress.percent, 100)

    def test_branch_is_measured_along_the_route(self):
        """
        A bill at NCOP Consideration is two steps from Signed into Law — 75% of
        the eight transitions from Introduced — although the state list holds
        twelve states and a one-step withdrawal exit.
        """
        progress = workflow_progress(self.make_bill("NCOP Consideration"))

        self.assertEqual(progress.target, "Signed into Law")
        self.assertEqual(progress.steps_left, 2)
        self.assertEqual(progress.percent, 75)

    def test_early_exit_does_not_read_as_complete(self):
        """
        Withdrawn is one step from Introduced, so measuring against the *nearest*
        terminal would call a brand-new bill finished.
        """
        progress = workflow_progress(self.make_bill("Introduced"))

        self.assertEqual(progress.steps_left, 8)
        self.assertEqual(progress.percent, 0)

    def test_withdrawn_bill_is_closed(self):
        progress = workflow_progress(self.make_bill("Withdrawn"))

        self.assertTrue(progress.is_closed)
        self.assertEqual(progress.percent, 100)

    def test_steps_mark_done_current_and_upcoming(self):
        workflow_type = self.types["International Resolution"]
        resolution = self.make_resolution("In Progress")
        self.add_transition(
            resolution,
            from_state=workflow_type.states.get(name="Captured"),
            to_state=workflow_type.states.get(name="Assigned"),
            actor=self.user,
        )

        progress = workflow_progress(resolution)

        self.assertEqual(
            [(step.state.name, step.status) for step in progress.steps],
            [
                ("Captured", "done"),
                ("Assigned", "done"),
                ("In Progress", "current"),
                ("Implemented", "upcoming"),
                ("Closed", "upcoming"),
            ],
        )
        self.assertEqual(progress.completed, 2)

    def test_journey_lists_the_moves_oldest_first(self):
        workflow_type = self.types["International Resolution"]
        resolution = self.make_resolution("Assigned")
        self.add_transition(
            resolution,
            from_state=workflow_type.states.get(name="Captured"),
            to_state=workflow_type.states.get(name="Assigned"),
            actor=self.user,
            notes="Captured at the ATC",
        )

        progress = workflow_progress(resolution)

        self.assertEqual([move.to_state for move in progress.moves], ["Assigned"])
        move = progress.moves[0]
        self.assertEqual(move.from_state, "Captured")
        self.assertEqual(move.actor, self.user.display_name)
        self.assertEqual(move.notes, "Captured at the ATC")
        self.assertEqual(progress.started_at, move.timestamp)


class WorkflowDetailViewTests(DetailPageTestCase):
    """Each instrument's detail page renders as the tabbed shell."""

    def test_renders_every_tab(self):
        report = self.make_report()

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        self.assertEqual(response.status_code, 200)
        page = live_markup(response)
        for pane in DETAIL_PANES:
            self.assertIn(f'id="pane-{pane}"', page)
            self.assertIn(f'id="tab-{pane}"', page)

    def test_progress_and_timeline_are_modals_not_tabs(self):
        report = self.make_report()

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        page = live_markup(response)
        for name in DETAIL_MODALS:
            self.assertIn(f'id="modal-{name}"', page)
            self.assertNotIn(f'id="pane-{name}"', page)
            self.assertNotIn(f'id="tab-{name}"', page)

    def test_workflow_menu_opens_progress_and_timeline(self):
        report = self.make_report()

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        workflow_menu, actions_menu = header_menus(response)
        self.assertIn('data-bs-target="#modal-progress"', workflow_menu)
        self.assertIn('data-bs-target="#modal-timeline"', workflow_menu)
        self.assertNotIn("#modal-progress", actions_menu)
        self.assertNotIn("#modal-timeline", actions_menu)

    def test_every_instrument_renders_its_detail_page(self):
        report = self.make_report()
        resolution_type = self.types["International Resolution"]
        resolution = InternationalResolution.objects.create(
            workflow_type=resolution_type,
            current_state=resolution_type.get_initial_state(),
            title="A resolution",
            resolution_number="IR-DETAIL-1",
            owner=self.user,
        )
        agreement = self.make_agreement()
        bill_type = self.types["Bill"]
        bill = Bill.objects.create(
            workflow_type=bill_type,
            current_state=bill_type.get_initial_state(),
            title="A bill",
            bill_number="B DETAIL-1",
            owner=self.user,
        )

        for name, instance in (
            ("delegation_report_detail", report),
            ("international_resolution_detail", resolution),
            ("international_agreement_detail", agreement),
            ("bill_detail", bill),
        ):
            with self.subTest(instrument=name):
                response = self.client.get(
                    reverse(f"pwms:{name}", args=[instance.public_id])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="modal-progress"')
                self.assertContains(response, 'id="modal-timeline"')

    def test_progress_modal_shows_the_completion(self):
        report = self.make_report()

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        self.assertContains(response, 'id="modal-progress"')
        self.assertContains(response, 'aria-label="0 percent complete"')
        # The whole sequence is rendered, terminal state included.
        self.assertContains(response, "Awaiting PGIR approval")
        self.assertContains(response, "Closed – House approved")

    def test_timeline_modal_shows_transitions_and_crud_together(self):
        report = self.make_report()
        report.description = "Edited once"
        report.save()
        self.add_transition(report, actor=self.user, notes="Moved on")

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        # The state change and the CRUD rows share one table...
        self.assertContains(response, "State change")
        self.assertContains(response, "Created")
        self.assertContains(response, "Moved on")
        # ...so the old separate "Audit trail" section is gone.
        self.assertNotContains(response, "Workflow audit trail")

    def test_timeline_modal_counts_the_rows(self):
        report = self.make_report()
        self.add_transition(report, actor=self.user)

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        page = live_markup(response)
        # Two rows: the create and the transition. The count sits on the menu
        # item that opens the modal and in the modal's own header.
        self.assertIn(
            'Timeline <span class="workflow-tab-count ms-auto">2</span>', page
        )
        self.assertIn('<span class="badge badge-muted">2</span>', page)

    def test_progress_menu_item_shows_the_percentage(self):
        report = self.make_report()

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        self.assertIn(
            'Progress <span class="workflow-tab-count ms-auto">0%</span>',
            live_markup(response),
        )

    def test_tab_counters_are_left_out_while_there_is_nothing_to_count(self):
        report = self.make_report()

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        page = live_markup(response)
        for element_id in (
            "tab-notes-count",
            "tab-events-count",
            "tab-attachments-count",
            "tab-referrals-count",
        ):
            self.assertNotIn(f'id="{element_id}"', page)

    def test_tab_counters_render_in_place_once_there_are_rows(self):
        report = self.make_report()
        WorkflowNote.objects.create(
            content_type=ContentType.objects.get_for_model(report),
            object_id=report.pk,
            author=self.user,
            body="Chased the committee",
        )

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        self.assertIn(
            '<span class="workflow-tab-count" id="tab-notes-count">1</span>',
            live_markup(response),
        )
        # The first paint renders counters in place, never out-of-band.
        self.assertNotContains(response, "hx-swap-oob")


class WorkflowDetailOverviewTests(DetailPageTestCase):
    """What the Overview tab shows: key facts, the record line, admin details."""

    def _get(self, instance, name="delegation_report_detail"):
        return self.client.get(reverse(f"pwms:{name}", args=[instance.public_id]))

    def _make_bill(self):
        bill_type = self.types["Bill"]
        return Bill.objects.create(
            workflow_type=bill_type,
            current_state=bill_type.get_initial_state(),
            title="A bill",
            bill_number="B OVERVIEW-1",
            owner=self.user,
        )

    def test_priority_is_a_badge_in_its_own_colour(self):
        for priority, label in (
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("urgent", "Urgent"),
        ):
            with self.subTest(priority=priority):
                report = self.make_report(priority=priority)

                page = live_markup(self._get(report))

                self.assertIn(
                    f'<span class="badge badge-priority badge-priority--{priority}">'
                    f"{label}</span>",
                    page,
                )

    def test_state_badges_take_the_states_own_colour(self):
        report = self.make_report()
        state = report.current_state
        state.color = "#123abc"
        state.save(update_fields=["color"])

        page = live_markup(self._get(report))

        badge = (
            f'<span class="badge badge-state-color" style="--state-color: #123abc">'
            f"{state.name}</span>"
        )
        # The Progress modal's "Current state" row. (Key information no longer
        # repeats the state: the status buttons at the top of the page show it.)
        self.assertEqual(page.count(badge), 1)

    def test_record_dates_are_one_muted_line(self):
        report = self.make_report()
        created = timezone.localtime(report.created_at).strftime("%d %b %Y")
        updated = timezone.localtime(report.updated_at).strftime("%d %b %Y")

        response = self._get(report)

        self.assertContains(
            response,
            f"Created {created} · Updated {updated}",
        )
        self.assertContains(response, 'class="workflow-record-line"')
        # The two cards the line replaces are gone.
        self.assertNotContains(response, "System information")
        self.assertNotContains(response, "Last updated")

    def test_technical_details_are_hidden_from_ordinary_users(self):
        report = self.make_report()

        response = self._get(report)

        self.assertNotContains(response, "Technical details")
        self.assertNotContains(response, "data-copy-text")
        self.assertNotContains(response, "Hierarchy level")

    def test_technical_details_are_collapsed_for_a_superuser(self):
        report = self.make_report()
        admin = get_user_model().objects.create_superuser(
            username="detail-admin", email="admin@example.com", password="pw"
        )
        self.client.force_login(admin)

        response = self._get(report)

        self.assertContains(response, "Technical details")
        # A native <details> with no `open` attribute starts collapsed.
        self.assertContains(response, '<details class="workflow-technical">')
        self.assertContains(response, "Hierarchy level")
        # The ID has a copy button carrying the value to copy.
        self.assertContains(response, f'data-copy-text="{report.public_id}"')
        self.assertContains(response, "js/copy-button.js")

    def test_record_version_is_a_workflow_action_on_a_bill(self):
        bill = self._make_bill()
        url = reverse("pwms:bill_version_create", args=[bill.public_id])

        workflow_menu, actions_menu = header_menus(self._get(bill, "bill_detail"))

        self.assertIn(url, workflow_menu)
        self.assertIn("Record version", workflow_menu)
        self.assertNotIn(url, actions_menu)
        self.assertNotIn("Record version", actions_menu)

    def test_other_instruments_have_no_record_version_item(self):
        report = self.make_report()

        workflow_menu, actions_menu = header_menus(self._get(report))

        self.assertNotIn("Record version", workflow_menu)
        self.assertNotIn("Record version", actions_menu)

    def test_a_bill_keeps_share_print_and_edit_under_actions(self):
        bill = self._make_bill()

        _, actions_menu = header_menus(self._get(bill, "bill_detail"))

        self.assertIn("Print", actions_menu)
        self.assertIn(reverse("pwms:bill_update", args=[bill.public_id]), actions_menu)


class DeadlineStatusTests(DetailPageTestCase):
    """``deadline_status`` and how the workflow register shows the deadline."""

    def _report_due(self, delta, **overrides):
        deadline = None if delta is None else timezone.now() + delta
        return self.make_report(deadline=deadline, **overrides)

    def _closed_state(self):
        return self.types["Delegation Report"].states.filter(is_terminal=True).first()

    def test_no_deadline_has_no_status(self):
        self.assertEqual(self._report_due(None).deadline_status, "")

    def test_a_past_deadline_is_overdue(self):
        self.assertEqual(
            self._report_due(timedelta(days=-1)).deadline_status, "overdue"
        )

    def test_a_deadline_within_ten_days_is_due_soon(self):
        for days in (0.5, 1, 9, 9.9):
            with self.subTest(days=days):
                report = self._report_due(timedelta(days=days))
                self.assertEqual(report.deadline_status, "due_soon")

    def test_a_deadline_beyond_ten_days_has_no_status(self):
        self.assertEqual(self._report_due(timedelta(days=10.1)).deadline_status, "")
        self.assertEqual(self._report_due(timedelta(days=60)).deadline_status, "")

    def test_a_closed_workflow_has_no_status_even_when_past_due(self):
        report = self._report_due(
            timedelta(days=-30), current_state=self._closed_state()
        )
        self.assertEqual(report.deadline_status, "")

    def test_status_agrees_with_is_overdue(self):
        for delta in (None, timedelta(days=-3), timedelta(days=3), timedelta(days=40)):
            with self.subTest(delta=delta):
                report = self._report_due(delta)
                self.assertEqual(report.deadline_status == "overdue", report.is_overdue)

    def _register(self):
        response = self.client.get(reverse("pwms:workflows"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _chip(self, report, status=""):
        css = f"deadline-chip deadline-chip--{status}" if status else "deadline-chip"
        date = timezone.localtime(report.deadline).strftime("%Y-%m-%d")
        return f'class="{css}"', date

    def test_register_marks_an_overdue_date_red(self):
        report = self._report_due(timedelta(days=-2))

        page = self._register()

        css, date = self._chip(report, "overdue")
        self.assertIn(css, page)
        self.assertIn(f">{date}</span>", page)
        self.assertIn("deadline-chip--overdue", page)
        self.assertNotIn("deadline-chip--due_soon", page)

    def test_register_marks_a_date_within_ten_days_amber(self):
        report = self._report_due(timedelta(days=5))

        page = self._register()

        css, date = self._chip(report, "due_soon")
        self.assertIn(css, page)
        self.assertIn(f">{date}</span>", page)
        self.assertNotIn("deadline-chip--overdue", page)

    def test_register_leaves_a_distant_date_plain(self):
        report = self._report_due(timedelta(days=45))

        page = self._register()

        css, date = self._chip(report)
        self.assertIn(css, page)
        self.assertIn(f">{date}</span>", page)
        self.assertNotIn("deadline-chip--", page)

    def test_register_hides_the_date_of_a_closed_workflow(self):
        report = self._report_due(
            timedelta(days=-30), current_state=self._closed_state()
        )

        page = self._register()

        self.assertIn(report.title, page)
        _, date = self._chip(report)
        self.assertNotIn(date, page)
        self.assertNotIn("deadline-chip", page)

    def test_register_shows_nothing_when_there_is_no_deadline(self):
        self._report_due(None)

        self.assertNotIn('class="deadline-chip', self._register())

    # -- the detail page's Due row ------------------------------------------
    def _detail(self, report):
        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )
        self.assertEqual(response.status_code, 200)
        return live_markup(response)

    def _due_row(self, page):
        row = page.split("<dt>Due</dt>", 1)[1].split("<dd>", 1)[1].split("</dd>", 1)[0]
        return " ".join(row.split())

    def test_detail_marks_an_overdue_date_red(self):
        report = self._report_due(timedelta(days=-2))

        row = self._due_row(self._detail(report))

        self.assertIn('class="deadline-chip deadline-chip--overdue"', row)
        local = timezone.localtime(report.deadline)
        self.assertIn(f">{local.strftime('%d %b %Y %H:%M')}</span>", row)

    def test_detail_marks_a_date_within_ten_days_amber(self):
        report = self._report_due(timedelta(days=4))

        row = self._due_row(self._detail(report))

        self.assertIn('class="deadline-chip deadline-chip--due_soon"', row)

    def test_detail_leaves_a_distant_date_plain(self):
        report = self._report_due(timedelta(days=45))

        row = self._due_row(self._detail(report))

        self.assertIn('class="deadline-chip"', row)
        self.assertNotIn("deadline-chip--", row)

    def test_detail_keeps_a_closed_records_date_but_uncoloured(self):
        report = self._report_due(
            timedelta(days=-30), current_state=self._closed_state()
        )

        row = self._due_row(self._detail(report))

        self.assertIn('class="deadline-chip"', row)
        self.assertNotIn("deadline-chip--", row)

    def test_detail_offers_set_deadline_when_there_is_none(self):
        report = self._report_due(None)

        page = self._detail(report)

        row = self._due_row(page)
        self.assertIn("Set deadline", row)
        self.assertIn(f'data-deadline-object-id="{report.pk}"', row)
        self.assertIn('data-deadline-content-type="pwms.delegationreport"', row)
        # The modal it opens is on the page, posting back to this same page.
        self.assertIn('id="modal-set-deadline"', page)
        detail_path = reverse("pwms:delegation_report_detail", args=[report.public_id])
        self.assertIn(f'name="next" value="{detail_path}"', page)

    def test_detail_has_no_button_or_modal_when_there_is_a_deadline(self):
        report = self._report_due(timedelta(days=30))

        page = self._detail(report)

        self.assertNotIn("data-deadline-object-id", page)
        self.assertNotIn('id="modal-set-deadline"', page)

    def test_detail_shows_a_dash_for_a_closed_record_without_a_deadline(self):
        report = self._report_due(None, current_state=self._closed_state())

        page = self._detail(report)

        self.assertEqual(self._due_row(page), "—")
        self.assertNotIn("data-deadline-object-id", page)
        self.assertNotIn('id="modal-set-deadline"', page)


class SetDeadlineTests(DetailPageTestCase):
    """The register's "Set deadline" button and the endpoint behind it."""

    URL = "pwms:workflow_set_deadline"

    def _closed_state(self):
        return self.types["Delegation Report"].states.filter(is_terminal=True).first()

    def _post(self, report, **overrides):
        data = {
            "content_type": "pwms.delegationreport",
            "object_id": report.pk,
            "deadline": "2027-03-15",
            "next": "/pwms/workflows/?q=report",
        }
        data.update(overrides)
        return self.client.post(reverse(self.URL), data)

    def _register(self):
        response = self.client.get(reverse("pwms:workflows"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    # -- the button ---------------------------------------------------------
    def test_button_is_offered_on_an_open_row_without_a_deadline(self):
        report = self.make_report()

        page = self._register()

        self.assertIn("Set deadline", page)
        self.assertIn(f'data-deadline-object-id="{report.pk}"', page)
        self.assertIn('data-deadline-content-type="pwms.delegationreport"', page)
        self.assertIn('id="modal-set-deadline"', page)

    def test_no_button_when_the_row_already_has_a_deadline(self):
        self.make_report(deadline=timezone.now() + timedelta(days=30))

        self.assertNotIn("data-deadline-object-id", self._register())

    def test_no_button_on_a_closed_row(self):
        self.make_report(current_state=self._closed_state())

        self.assertNotIn("data-deadline-object-id", self._register())

    def test_a_meeting_is_never_offered_a_deadline(self):
        from .models import Meeting
        from .views import _can_offer_deadline

        state = self.types["Delegation Report"].get_initial_state()
        meeting = Meeting(current_state=state, deadline=None)

        self.assertFalse(_can_offer_deadline(self.user, meeting))

    # -- the endpoint -------------------------------------------------------
    def test_it_sets_the_deadline_to_the_end_of_the_chosen_day(self):
        report = self.make_report()

        response = self._post(report)

        self.assertRedirects(
            response, "/pwms/workflows/?q=report", fetch_redirect_response=False
        )
        report.refresh_from_db()
        local = timezone.localtime(report.deadline)
        self.assertEqual(local.strftime("%Y-%m-%d %H:%M"), "2027-03-15 23:59")

    def test_the_message_confirms_it(self):
        report = self.make_report()

        response = self._post(report, next=reverse("pwms:workflows"))
        page = self.client.get(response["Location"]).content.decode()

        self.assertIn("Deadline set.", page)

    def test_the_button_disappears_once_a_deadline_is_set(self):
        report = self.make_report()
        self._post(report)

        page = self._register()

        self.assertNotIn("data-deadline-object-id", page)
        self.assertIn("2027-03-15", page)

    def test_a_closed_record_takes_no_deadline(self):
        report = self.make_report(current_state=self._closed_state())

        self._post(report)

        report.refresh_from_db()
        self.assertIsNone(report.deadline)

    def test_an_existing_deadline_is_not_overwritten(self):
        existing = timezone.now() + timedelta(days=20)
        report = self.make_report(deadline=existing)

        self._post(report)

        report.refresh_from_db()
        self.assertEqual(report.deadline, existing)

    def test_an_invalid_date_changes_nothing(self):
        report = self.make_report()

        for bad in ("", "not-a-date", "15/03/2027"):
            with self.subTest(value=bad):
                response = self._post(report, deadline=bad)
                self.assertEqual(response.status_code, 302)
                report.refresh_from_db()
                self.assertIsNone(report.deadline)

    def test_an_unsafe_next_falls_back_to_the_register(self):
        report = self.make_report()

        response = self._post(report, next="https://evil.example/steal")

        self.assertEqual(response["Location"], reverse("pwms:workflows"))

    def test_it_needs_the_edit_right(self):
        report = self.make_report()
        outsider = get_user_model().objects.create_user(
            username="deadline-outsider", password="pw"
        )
        self.client.force_login(outsider)

        response = self._post(report)

        self.assertEqual(response.status_code, 403)
        report.refresh_from_db()
        self.assertIsNone(report.deadline)

    def test_it_only_accepts_post(self):
        self.assertEqual(self.client.get(reverse(self.URL)).status_code, 405)

    def test_it_refuses_an_unknown_target(self):
        report = self.make_report()

        response = self._post(report, content_type="auth.user")

        self.assertEqual(response.status_code, 404)


class AssignTests(DetailPageTestCase):
    """The detail page's "Assign" button and the endpoint behind it."""

    URL = "pwms:workflow_assign"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.person = get_user_model().objects.create_user(
            username="assignee", first_name="Ada", last_name="Assignee", password="pw"
        )

    def _closed_state(self):
        return self.types["Delegation Report"].states.filter(is_terminal=True).first()

    def _detail_url(self, report):
        return reverse("pwms:delegation_report_detail", args=[report.public_id])

    def _detail(self, report):
        response = self.client.get(self._detail_url(report))
        self.assertEqual(response.status_code, 200)
        return live_markup(response)

    def _assigned_row(self, page):
        row = (
            page.split("<dt>Assigned to</dt>", 1)[1]
            .split("<dd>", 1)[1]
            .split("</dd>", 1)[0]
        )
        return " ".join(row.split())

    def _post(self, report, **overrides):
        data = {
            "content_type": "pwms.delegationreport",
            "object_id": report.pk,
            "assigned_to": self.person.pk,
            "next": self._detail_url(report),
        }
        data.update(overrides)
        return self.client.post(reverse(self.URL), data)

    # -- the button ---------------------------------------------------------
    def test_button_is_offered_when_nobody_is_assigned(self):
        report = self.make_report()

        page = self._detail(report)

        self.assertIn("Assign", self._assigned_row(page))
        self.assertIn('data-bs-target="#modal-assign"', self._assigned_row(page))
        self.assertIn('id="modal-assign"', page)

    def test_the_modal_holds_the_user_search_picker(self):
        report = self.make_report()

        page = self._detail(report)

        modal = page.split('id="modal-assign"', 1)[1]
        self.assertIn('name="assigned_to"', modal)
        self.assertIn(f'hx-get="{reverse("pwms:user_search")}"', modal)
        self.assertIn(f'name="object_id" value="{report.pk}"', modal)
        self.assertIn('name="content_type" value="pwms.delegationreport"', modal)
        self.assertIn(f'name="next" value="{self._detail_url(report)}"', modal)

    def test_no_button_once_someone_is_assigned(self):
        report = self.make_report(assigned_to=self.person)

        page = self._detail(report)

        self.assertIn("Ada Assignee", self._assigned_row(page))
        self.assertNotIn("modal-assign", page)

    def test_no_button_on_a_closed_record(self):
        report = self.make_report(current_state=self._closed_state())

        page = self._detail(report)

        self.assertEqual(self._assigned_row(page), "—")
        self.assertNotIn("modal-assign", page)

    # -- the endpoint -------------------------------------------------------
    def test_it_assigns_the_chosen_person_and_returns_to_the_page(self):
        report = self.make_report()

        response = self._post(report)

        self.assertRedirects(
            response, self._detail_url(report), fetch_redirect_response=False
        )
        report.refresh_from_db()
        self.assertEqual(report.assigned_to, self.person)

    def test_the_page_then_names_them_and_confirms_it(self):
        report = self.make_report()
        self._post(report)

        page = self.client.get(self._detail_url(report)).content.decode()

        self.assertIn("Assigned to Ada Assignee.", page)
        self.assertNotIn(
            "modal-assign", live_markup(self.client.get(self._detail_url(report)))
        )

    def test_a_closed_record_takes_no_assignee(self):
        report = self.make_report(current_state=self._closed_state())

        self._post(report)

        report.refresh_from_db()
        self.assertIsNone(report.assigned_to)

    def test_an_existing_assignee_is_not_replaced(self):
        other = get_user_model().objects.create_user(username="first", password="pw")
        report = self.make_report(assigned_to=other)

        self._post(report)

        report.refresh_from_db()
        self.assertEqual(report.assigned_to, other)

    def test_nothing_chosen_or_a_bad_choice_changes_nothing(self):
        report = self.make_report()
        inactive = get_user_model().objects.create_user(
            username="gone", password="pw", is_active=False
        )

        for bad in ("", "999999", "abc", inactive.pk):
            with self.subTest(value=bad):
                response = self._post(report, assigned_to=bad)
                self.assertEqual(response.status_code, 302)
                report.refresh_from_db()
                self.assertIsNone(report.assigned_to)

    def test_an_unsafe_next_falls_back_to_the_register(self):
        report = self.make_report()

        response = self._post(report, next="https://evil.example/steal")

        self.assertEqual(response["Location"], reverse("pwms:workflows"))

    def test_it_needs_the_edit_right(self):
        report = self.make_report()
        outsider = get_user_model().objects.create_user(
            username="assign-outsider", password="pw"
        )
        self.client.force_login(outsider)

        response = self._post(report)

        self.assertEqual(response.status_code, 403)
        report.refresh_from_db()
        self.assertIsNone(report.assigned_to)

    def test_it_only_accepts_post(self):
        self.assertEqual(self.client.get(reverse(self.URL)).status_code, 405)

    def test_it_refuses_an_unknown_target(self):
        report = self.make_report()

        self.assertEqual(self._post(report, content_type="auth.user").status_code, 404)


#: A minimal stand-in for a rendered diagram.
SVG_STUB = "<svg xmlns='http://www.w3.org/2000/svg'><title>states</title></svg>"


@contextmanager
def diagram_dirs(*workflow_types):
    """
    A temporary ``WORKFLOW_DIAGRAM_DIRS`` holding a stub SVG per type.

    The diagram view serves the files ``manage.py generate_diagrams`` writes, so
    the tests point the setting at a directory they control rather than relying
    on the checkout having rendered anything. Pass no types for "nothing
    generated".
    """
    with tempfile.TemporaryDirectory() as tmp:
        for workflow_type in workflow_types:
            path = Path(tmp) / f"{workflow_type_stem(workflow_type)}.svg"
            path.write_text(SVG_STUB)
        with override_settings(WORKFLOW_DIAGRAM_DIRS=[tmp]):
            yield


class WorkflowDiagramViewTests(DetailPageTestCase):
    """The Diagram tab serves the generated SVG, or says there is none."""

    def test_detail_page_reports_a_missing_diagram(self):
        report = self.make_report()

        with diagram_dirs():
            response = self.client.get(
                reverse("pwms:delegation_report_detail", args=[report.public_id])
            )

        self.assertContains(response, "No diagram has been generated")

    def test_serves_the_generated_svg(self):
        report = self.make_report()

        with diagram_dirs(report.workflow_type):
            response = self.client.get(
                reverse("pwms:workflow_diagram", args=[report.public_id])
            )
            # FileResponse streams lazily, so read it while the file exists.
            content = b"".join(response.streaming_content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertEqual(content.decode(), SVG_STUB)

    def test_detail_page_embeds_the_generated_diagram(self):
        report = self.make_report()

        with diagram_dirs(report.workflow_type):
            response = self.client.get(
                reverse("pwms:delegation_report_detail", args=[report.public_id])
            )

        diagram_url = reverse("pwms:workflow_diagram", args=[report.public_id])
        self.assertContains(response, f'src="{diagram_url}"')

    def test_missing_diagram_is_404(self):
        report = self.make_report()

        with diagram_dirs():
            response = self.client.get(
                reverse("pwms:workflow_diagram", args=[report.public_id])
            )

        self.assertEqual(response.status_code, 404)

    def test_requires_view_permission_on_the_instance(self):
        report = self.make_report()
        stranger = get_user_model().objects.create_user(
            username="stranger", password="pw"
        )
        self.client.force_login(stranger)

        with diagram_dirs(report.workflow_type):
            diagram = self.client.get(
                reverse("pwms:workflow_diagram", args=[report.public_id])
            )
            page = self.client.get(
                reverse("pwms:delegation_report_detail", args=[report.public_id])
            )

        self.assertEqual(diagram.status_code, 403)
        self.assertEqual(page.status_code, 403)
