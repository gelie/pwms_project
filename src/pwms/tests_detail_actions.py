"""The detail page's add-* affordances: participants, notes and attachments."""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from .forms import DelegationParticipantFormSet
from .models import (
    Bill,
    DelegationParticipant,
    DelegationReport,
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Role,
    WorkflowGroupAccess,
    WorkflowType,
)
from .services.permissions import EDIT, resolve


class ParticipantAddTests(TestCase):
    """Adding and removing delegates on a delegation report from its own page."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="part-owner", password="pw")
        cls.outsider = User.objects.create_user(username="part-outsider", password="pw")
        cls.person = User.objects.create_user(
            username="delegate", password="pw", first_name="Ada", last_name="Mokoena"
        )
        wt = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=wt,
            current_state=wt.get_initial_state(),
            title="Participant test report",
            owner=cls.owner,
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def _add_url(self):
        return reverse(
            "pwms:delegation_report_participant_add",
            kwargs={"public_id": self.report.public_id},
        )

    def _detail_url(self):
        return reverse(
            "pwms:delegation_report_detail",
            kwargs={"public_id": self.report.public_id},
        )

    def _post_participant(self, **overrides):
        data = {
            "user": self.person.pk,
            "participant_type": DelegationParticipant.MEMBER,
            "delegation_role": "Leader of the Delegation",
        }
        data.update(overrides)
        return self.client.post(self._add_url(), data)

    def test_the_page_offers_the_add_form_to_an_editor(self):
        response = self.client.get(self._detail_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add Participant")
        self.assertContains(response, reverse("pwms:user_search"))

    def test_a_participant_is_named_from_the_chosen_account(self):
        response = self._post_participant()
        self.assertEqual(response.status_code, 200)
        participant = self.report.participants.get()
        self.assertEqual(participant.user, self.person)
        self.assertEqual(participant.first_name, "Ada")
        self.assertEqual(participant.last_name, "Mokoena")
        self.assertEqual(participant.delegation_role, "Leader of the Delegation")
        self.assertContains(response, "Ada Mokoena")

    def test_adding_a_participant_needs_the_edit_right(self):
        self.client.force_login(self.outsider)
        response = self._post_participant()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.report.participants.exists())

    def test_a_person_is_not_added_to_the_same_delegation_twice(self):
        self.assertEqual(self._post_participant().status_code, 200)
        again = self._post_participant()
        self.assertEqual(again.status_code, 200)
        self.assertContains(again, "is already on this delegation.")
        self.assertEqual(self.report.participants.count(), 1)

    def test_a_participant_must_name_a_person(self):
        response = self._post_participant(user="")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose the delegate.")
        self.assertFalse(self.report.participants.exists())

    def test_removing_a_participant_keeps_the_row_as_the_record(self):
        self._post_participant()
        participant = self.report.participants.get()
        response = self.client.post(
            reverse(
                "pwms:delegation_report_participant_delete",
                kwargs={"public_id": participant.public_id},
            )
        )
        self.assertEqual(response.status_code, 200)
        # Soft delete: the row survives, marked with who took them off and when.
        participant.refresh_from_db()
        self.assertTrue(participant.is_removed)
        self.assertEqual(participant.removed_by, self.owner)
        self.assertIsNotNone(participant.removed_at)
        # Off the delegation, but still on its record.
        self.assertFalse(
            self.report.participants.filter(removed_at__isnull=True).exists()
        )
        self.assertContains(response, "Ada Mokoena")
        self.assertContains(response, "Removed")
        self.assertContains(response, "Taken off by")

    def test_a_removed_person_can_be_added_to_the_delegation_again(self):
        self._post_participant()
        self.client.post(
            reverse(
                "pwms:delegation_report_participant_delete",
                kwargs={"public_id": self.report.participants.get().public_id},
            )
        )
        response = self._post_participant()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.report.participants.filter(removed_at__isnull=True).count(), 1
        )
        # The earlier stint is history, still kept on the record.
        self.assertEqual(self.report.participants.count(), 2)

    def test_the_report_form_takes_a_delegate_off_without_deleting_the_row(self):
        """The report's own form soft-deletes too, so both paths keep the record."""
        participant = DelegationParticipant.objects.create(
            delegation_report=self.report,
            participant_type=DelegationParticipant.MEMBER,
            first_name="Ada",
            last_name="Mokoena",
            user=self.person,
        )
        formset = DelegationParticipantFormSet(
            {
                "participants-TOTAL_FORMS": "1",
                "participants-INITIAL_FORMS": "1",
                "participants-MIN_NUM_FORMS": "0",
                "participants-MAX_NUM_FORMS": "1000",
                "participants-0-id": participant.pk,
                "participants-0-user": self.person.pk,
                "participants-0-participant_type": DelegationParticipant.MEMBER,
                "participants-0-delegation_role": "",
                "participants-0-DELETE": "on",
            },
            instance=self.report,
            prefix="participants",
            queryset=self.report.participants.filter(removed_at__isnull=True),
            removed_by=self.owner,
        )
        self.assertTrue(formset.is_valid())

        formset.save()

        participant.refresh_from_db()
        self.assertTrue(participant.is_removed)
        self.assertEqual(participant.removed_by, self.owner)

    def test_removing_a_participant_needs_the_edit_right(self):
        self._post_participant()
        participant = self.report.participants.get()
        self.client.force_login(self.outsider)
        response = self.client.post(
            reverse(
                "pwms:delegation_report_participant_delete",
                kwargs={"public_id": participant.public_id},
            )
        )
        self.assertEqual(response.status_code, 403)
        participant.refresh_from_db()
        self.assertFalse(participant.is_removed)


class NotesAddTests(TestCase):
    """The note log on a record's page: adding, listing and deleting notes."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="note-owner", password="pw")
        cls.outsider = User.objects.create_user(username="note-outsider", password="pw")
        wt = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=wt,
            current_state=wt.get_initial_state(),
            title="Notes test report",
            owner=cls.owner,
            notes="Recorded on the report itself.",
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def _detail_url(self):
        return reverse(
            "pwms:delegation_report_detail",
            kwargs={"public_id": self.report.public_id},
        )

    def _add_note(self, body, target=None):
        target = target if target is not None else self.report
        return self.client.post(
            reverse("pwms:workflow_notes_add"),
            {
                "content_type": f"{target._meta.app_label}.{target._meta.model_name}",
                "object_id": target.pk,
                "body": body,
            },
        )

    def _editor_who_is_not_the_author(self):
        """A user with the edit right on the report who wrote none of its notes."""
        editor = get_user_model().objects.create_user(
            username="note-editor", password="pw"
        )
        group = Group.objects.create(name="Notes Editors", group_type="committee")
        GroupMembership.objects.create(
            user=editor,
            group=group,
            role=Role.objects.create(name="Notes Editor Role"),
        )
        WorkflowGroupAccess.objects.create(
            content_type=ContentType.objects.get_for_model(DelegationReport),
            object_id=self.report.pk,
            group=group,
            can_view=True,
            can_edit=True,
        )
        return editor

    def test_the_notes_panel_offers_the_add_form_to_an_editor(self):
        response = self.client.get(self._detail_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add Note")

    def test_a_note_is_recorded_as_a_row_with_its_author(self):
        response = self._add_note("Chased the committee for an answer.")
        self.assertEqual(response.status_code, 200)
        note = self.report.note_log().get()
        self.assertEqual(note.body, "Chased the committee for an answer.")
        self.assertEqual(note.author, self.owner)
        self.assertContains(response, "Chased the committee for an answer.")
        self.assertContains(response, self.owner.display_name)
        # A note is its own row, so the record's notes column is left alone.
        self.report.refresh_from_db()
        self.assertEqual(self.report.notes, "Recorded on the report itself.")

    def test_recording_a_note_needs_the_edit_right(self):
        self.client.force_login(self.outsider)
        response = self._add_note("Note from an outsider")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.report.note_log().exists())

    def test_a_blank_note_is_not_recorded(self):
        response = self._add_note("   ")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.report.note_log().exists())

    def test_the_panel_shows_the_record_notes_above_the_log(self):
        self._add_note("Followed up with the committee.")
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Recorded on the report itself.")
        self.assertContains(response, "Followed up with the committee.")

    def test_a_note_can_be_deleted(self):
        self._add_note("Recorded in error.")
        note = self.report.note_log().get()
        response = self.client.post(
            reverse("pwms:workflow_note_delete", kwargs={"public_id": note.public_id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.report.note_log().exists())
        self.assertContains(response, "No notes recorded.")

    def test_deleting_a_note_needs_the_edit_right(self):
        self._add_note("Kept.")
        note = self.report.note_log().get()
        self.client.force_login(self.outsider)
        response = self.client.post(
            reverse("pwms:workflow_note_delete", kwargs={"public_id": note.public_id})
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.report.note_log().exists())

    def test_the_author_can_edit_their_note(self):
        self._add_note("First wording.")
        note = self.report.note_log().get()
        # The page offers the inline form to the note's author.
        self.assertContains(self.client.get(self._detail_url()), f"note-edit-{note.pk}")

        response = self.client.post(
            reverse("pwms:workflow_note_edit", kwargs={"public_id": note.public_id}),
            {"body": "Second wording."},
        )

        self.assertEqual(response.status_code, 200)
        note.refresh_from_db()
        self.assertEqual(note.body, "Second wording.")
        self.assertContains(response, "Second wording.")

    def test_a_blank_edit_is_refused(self):
        self._add_note("Left as written.")
        note = self.report.note_log().get()
        response = self.client.post(
            reverse("pwms:workflow_note_edit", kwargs={"public_id": note.public_id}),
            {"body": "   "},
        )
        self.assertEqual(response.status_code, 200)
        note.refresh_from_db()
        self.assertEqual(note.body, "Left as written.")
        self.assertContains(response, "cannot be empty")

    def test_an_editor_who_is_not_the_author_cannot_change_a_note(self):
        self._add_note("Mine to change.")
        note = self.report.note_log().get()
        editor = self._editor_who_is_not_the_author()
        # The editor may edit the report itself, so the refusal is about the note.
        self.assertTrue(resolve(editor, self.report, EDIT))
        self.client.force_login(editor)

        edit = self.client.post(
            reverse("pwms:workflow_note_edit", kwargs={"public_id": note.public_id}),
            {"body": "Rewritten by someone else."},
        )
        delete = self.client.post(
            reverse("pwms:workflow_note_delete", kwargs={"public_id": note.public_id})
        )

        self.assertEqual(edit.status_code, 403)
        self.assertEqual(delete.status_code, 403)
        note.refresh_from_db()
        self.assertEqual(note.body, "Mine to change.")
        # ...and the page does not offer them the controls either.
        self.assertNotContains(
            self.client.get(self._detail_url()), f"note-edit-{note.pk}"
        )

    def test_an_editor_may_clear_up_a_note_whose_author_has_gone(self):
        """A note with no author (the account was deleted) stays manageable."""
        self._add_note("Left behind by a departed colleague.")
        note = self.report.note_log().get()
        note.author = None
        note.save(update_fields=["author"])
        self.client.force_login(self._editor_who_is_not_the_author())

        response = self.client.post(
            reverse("pwms:workflow_note_delete", kwargs={"public_id": note.public_id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.report.note_log().exists())


class DetailAddAffordancePermissionTests(TestCase):
    """Who is offered each add-* button on a workflow detail page.

    Every one of them writes to the record, so all of them are gated on the edit
    right — including "Add attachment", which has always hidden itself from a
    reader who may only view the record.
    """

    #: The add-* buttons the detail page can show.
    BUTTONS = (
        "Add attachment",
        "Add Referral",
        "Add Note",
        "Add Participant",
        "Add update",
    )

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="affordance-owner", password="pw")
        cls.viewer = User.objects.create_user(
            username="affordance-viewer", password="pw"
        )
        wt = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=wt,
            current_state=wt.get_initial_state(),
            title="Affordance test report",
            owner=cls.owner,
        )
        # A group with view-but-not-edit access on the report, and the viewer in it.
        group = Group.objects.create(name="Affordance Viewers", group_type="committee")
        GroupMembership.objects.create(
            user=cls.viewer,
            group=group,
            role=Role.objects.create(name="Affordance Viewer Role"),
        )
        WorkflowGroupAccess.objects.create(
            content_type=ContentType.objects.get_for_model(DelegationReport),
            object_id=cls.report.pk,
            group=group,
            can_view=True,
            can_edit=False,
        )

    def _detail_url(self):
        return reverse(
            "pwms:delegation_report_detail",
            kwargs={"public_id": self.report.public_id},
        )

    def test_an_editor_is_offered_every_add_button(self):
        self.client.force_login(self.owner)
        response = self.client.get(self._detail_url())
        self.assertEqual(response.status_code, 200)
        for label in self.BUTTONS:
            self.assertContains(response, label)

    def test_a_viewer_is_offered_none_of_them(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self._detail_url())
        self.assertEqual(response.status_code, 200)
        for label in self.BUTTONS:
            self.assertNotContains(response, label)


class ReportUpdateAddTests(TestCase):
    """Recording BR03 updates — the ATC publication among them — on a report.

    The seeded *Close – House approved* transition is guarded by an
    ``atc-update-published`` event, so recording an update that carries any ATC
    detail is what unblocks closing a report (see
    ``pwms.models.DelegationReportUpdate``). The card sits on the report's
    **Overview** tab, so the affordance is visible without opening Related.
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="update-owner", password="pw")
        cls.outsider = User.objects.create_user(
            username="update-outsider", password="pw"
        )
        type_ = WorkflowType.objects.get(name="Delegation Report")
        # "Tabled and referred to Committee": the state the close move leaves.
        cls.report = DelegationReport.objects.create(
            workflow_type=type_,
            current_state=type_.states.get(name="Tabled and referred to Committee"),
            title="ATC update test report",
            owner=cls.owner,
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def _add_url(self):
        return reverse(
            "pwms:delegation_report_update_add",
            kwargs={"public_id": self.report.public_id},
        )

    def _detail_url(self):
        return reverse(
            "pwms:delegation_report_detail",
            kwargs={"public_id": self.report.public_id},
        )

    def _close_transition(self):
        return self.report.get_available_transitions().get(
            name="Close – House approved"
        )

    def _post_update(self, **overrides):
        data = {"update_date": "2026-03-04"}
        data.update(overrides)
        return self.client.post(self._add_url(), data)

    def _published(self):
        return (
            self.report.events()
            .filter(event_type__slug="atc-update-published")
            .exists()
        )

    def test_the_close_move_starts_blocked(self):
        self.assertEqual(
            self.report.unmet_transition_conditions(self._close_transition()),
            ["Requires a 'ATC update published' event on this workflow."],
        )

    def test_recording_the_atc_publication_emits_the_event(self):
        response = self._post_update(
            atc_reference="ATC 2026 No 12",
            atc_publication_date="2026-03-04",
            atc_page_number="4120",
        )

        self.assertEqual(response.status_code, 200)
        update = self.report.updates.get()
        self.assertEqual(update.recorded_by, self.owner)
        self.assertEqual(update.atc_page_number, "4120")
        self.assertTrue(self._published())

    def test_recording_the_atc_publication_unblocks_closing(self):
        self._post_update(atc_reference="ATC 2026 No 12")

        self.report.refresh_from_db()
        self.assertEqual(
            self.report.unmet_transition_conditions(self._close_transition()), []
        )

    def test_the_answer_says_the_report_may_now_be_closed(self):
        response = self._post_update(atc_reference="ATC 2026 No 12")

        self.assertContains(response, "may now be closed")

    def test_the_answer_swaps_the_card_and_its_status_alert(self):
        """The response is the card again, plus the alert swapped out of band."""
        response = self._post_update(atc_reference="ATC 2026 No 12")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="report-updates-section"')
        self.assertContains(response, 'hx-swap-oob="true"')

    def test_an_update_without_atc_details_publishes_nothing(self):
        response = self._post_update(notes="Chased the department.")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.report.updates.count(), 1)
        self.assertFalse(self._published())
        self.assertContains(response, "Add the ATC reference")
        self.assertTrue(
            self.report.unmet_transition_conditions(self._close_transition())
        )

    def test_the_status_may_not_come_from_another_type(self):
        other = WorkflowType.objects.get(name="International Resolution")
        response = self._post_update(resulting_state=other.states.first().pk)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.report.updates.exists())
        self.assertContains(response, "Select a valid choice")

    def test_recording_an_update_needs_the_edit_right(self):
        self.client.force_login(self.outsider)
        response = self._post_update(atc_reference="ATC 2026 No 12")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.report.updates.exists())

    def test_the_history_lists_the_newest_update_first(self):
        self._post_update(update_date="2026-03-04", atc_reference="ATC-Alpha")
        self._post_update(update_date="2026-04-04", atc_reference="ATC-Zulu")

        page = self.client.get(self._detail_url()).content.decode()
        self.assertLess(page.index("ATC-Zulu"), page.index("ATC-Alpha"))

    def test_the_affordance_is_on_the_overview_tab(self):
        """It is reachable without opening Related — the close depends on it."""
        overview, related = self._panes(
            self.client.get(self._detail_url()).content.decode()
        )

        self.assertIn('id="report-updates-section"', overview)
        self.assertNotIn('id="report-updates-section"', related)

    @staticmethod
    def _panes(page):
        """The Overview and Related panes, split out by their pane ids."""
        overview = page.split('id="pane-overview"', 1)[1].split(
            'id="pane-progress"', 1
        )[0]
        related = page.split('id="pane-related"', 1)[1].split('id="pane-notes"', 1)[0]
        return overview, related


class DetailPanelWiringTests(TestCase):
    """The attachments, notes and referrals panels are on every detail template.

    All three live in the shared shell (``pwms/workflow-detail.html``), so every
    workflow type renders them and a type added later inherits them too.
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="notes-wiring", password="pw")
        report_type = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Wiring report",
            owner=cls.owner,
        )
        resolution_type = WorkflowType.objects.get(name="International Resolution")
        cls.resolution = InternationalResolution.objects.create(
            workflow_type=resolution_type,
            current_state=resolution_type.get_initial_state(),
            resolution_number="IR-NOTES-1",
            title="Wiring resolution",
            owner=cls.owner,
        )
        agreement_type = WorkflowType.objects.get(name="International Agreement")
        cls.agreement = InternationalAgreement.objects.create(
            workflow_type=agreement_type,
            current_state=agreement_type.get_initial_state(),
            title="Wiring agreement",
            owner=cls.owner,
            agreement_type=InternationalAgreement.SECTION_231_2,
            submitting_department="Department of International Relations",
            responsible_minister_name="Minister of International Relations",
        )
        bill_type = WorkflowType.objects.get(name="Bill")
        cls.bill = Bill.objects.create(
            workflow_type=bill_type,
            current_state=bill_type.get_initial_state(),
            bill_number="B 99—2026",
            title="Wiring bill",
            bill_type=Bill.SECTION_76,
            house_of_origin=Bill.NA,
            owner=cls.owner,
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def _pages(self):
        """Each workflow's detail page, as (URL name, instance)."""
        return (
            ("delegation_report_detail", self.report),
            ("international_resolution_detail", self.resolution),
            ("international_agreement_detail", self.agreement),
            ("bill_detail", self.bill),
        )

    def _get(self, url_name, obj):
        return self.client.get(
            reverse(f"pwms:{url_name}", kwargs={"public_id": obj.public_id})
        )

    def test_every_type_offers_the_notes_panel(self):
        for url_name, obj in self._pages():
            with self.subTest(url_name=url_name):
                response = self._get(url_name, obj)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Add Note")

    def test_every_type_offers_the_referral_panel(self):
        for url_name, obj in self._pages():
            with self.subTest(url_name=url_name):
                response = self._get(url_name, obj)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Add Referral")

    def test_the_resolution_keeps_its_own_cards_beside_the_panel(self):
        """The panel is inherited; this tab overrides the block to add cards."""
        response = self.client.get(
            reverse(
                "pwms:international_resolution_detail",
                kwargs={"public_id": self.resolution.public_id},
            )
        )
        self.assertContains(response, "Resolution text")
        self.assertContains(response, "Implementation progress")
        self.assertContains(response, "Add Note")

    def test_a_note_lands_on_a_resolution_too(self):
        """Notes are rows, so a type with no notes column can still take one."""
        response = self.client.post(
            reverse("pwms:workflow_notes_add"),
            {
                "content_type": f"pwms.{InternationalResolution._meta.model_name}",
                "object_id": self.resolution.pk,
                "body": "Cleared with the department.",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [note.body for note in self.resolution.note_log()],
            ["Cleared with the department."],
        )


class AddAffordanceOwnershipTests(TestCase):
    """Reading is not editing: the add-* affordances follow the edit right.

    A record's ``assigned_to`` user is granted **view only** (assignment is a work
    queue, not authority), so on a record you do not own the Notes / Attachments /
    Referrals add buttons are absent — as are the toolbar's Edit and Delete, which
    use the same check. Recognised editors do see them: this record's ``owner``,
    and a member of the type's owning group when the type names one (its
    materialised primary grant carries ``can_edit`` — see
    ``ViewerGroupAccessTests``).
    """

    #: The add-* buttons a record page can show.
    BUTTONS = ("Add attachment", "Add Referral", "Add Note")

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="owned-by", password="pw")
        cls.assignee = User.objects.create_user(username="assigned-to", password="pw")
        wt = WorkflowType.objects.get(name="International Resolution")
        cls.resolution = InternationalResolution.objects.create(
            workflow_type=wt,
            current_state=wt.get_initial_state(),
            resolution_number="IR-OWN-1",
            title="Ownership test resolution",
            owner=cls.owner,
            assigned_to=cls.assignee,
        )

    def _detail_url(self):
        return reverse(
            "pwms:international_resolution_detail",
            kwargs={"public_id": self.resolution.public_id},
        )

    def _update_url(self):
        return reverse(
            "pwms:international_resolution_update",
            kwargs={"public_id": self.resolution.public_id},
        )

    def test_the_owner_is_offered_the_add_buttons_and_the_toolbar_actions(self):
        self.client.force_login(self.owner)
        response = self.client.get(self._detail_url())
        self.assertEqual(response.status_code, 200)
        for label in self.BUTTONS:
            self.assertContains(response, label)
        self.assertContains(response, self._update_url())

    def test_an_assignee_can_read_the_record_but_is_offered_no_actions(self):
        self.client.force_login(self.assignee)
        response = self.client.get(self._detail_url())
        self.assertEqual(response.status_code, 200)
        # The record is readable, panels and empty states included...
        self.assertContains(response, "No notes recorded.")
        self.assertContains(response, "This record has not been referred.")
        # ...but nothing on it may be changed.
        for label in self.BUTTONS:
            self.assertNotContains(response, label)
        # The toolbar agrees, because it uses the same check.
        self.assertNotContains(response, self._update_url())
