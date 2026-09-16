"""The workflow detail page: its tabs, the unified timeline, and the diagram.

The detail page used to be one long scroll of tables. It is now a header plus
one tab per concern, with ``TransitionLog`` and the auditlog CRUD trail merged
into a single Timeline table (see :mod:`pwms.services.history`).
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
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
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Role,
    TransitionLog,
    WorkflowType,
)
from .services.history import _audit_entry, instance_timeline
from .utils.diagrams import workflow_type_stem

#: The pane ids the shared detail template renders, in tab order.
DETAIL_PANES = (
    "overview",
    "related",
    "notes",
    "timeline",
    "attachments",
    "diagram",
    "referrals",
)


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

    def add_transition(self, instance, *, actor=None, notes="", ip="127.0.0.1"):
        """A state-change row for ``instance`` (the views do not perform one yet)."""
        return TransitionLog.objects.create(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
            action="STATE_TRANSITION",
            from_state=instance.current_state,
            to_state=instance.current_state,
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


class WorkflowDetailViewTests(DetailPageTestCase):
    """Each instrument's detail page renders as the tabbed shell."""

    def test_renders_every_tab(self):
        report = self.make_report()

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        self.assertEqual(response.status_code, 200)
        for pane in DETAIL_PANES:
            self.assertContains(response, f'id="pane-{pane}"')
            self.assertContains(response, f'id="tab-{pane}"')

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
                self.assertContains(response, 'id="pane-timeline"')

    def test_timeline_tab_shows_transitions_and_crud_together(self):
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

    def test_timeline_tab_counts_the_rows(self):
        report = self.make_report()
        self.add_transition(report, actor=self.user)

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        # Two rows: the create and the transition.
        self.assertContains(response, '<span class="workflow-tab-count">2</span>')


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
