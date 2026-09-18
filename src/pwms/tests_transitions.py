"""The Status menu: performing state transitions from a record's detail page.

Covers the dropdown the shared toolbar renders, the confirmation page it links
to, and the transition that page applies.

Materialisation gives a type's **owning group** view, edit *and* transition, so a
member of it can move its records on out of the box. What gates the menu is the
``transition`` capability rather than group membership, so the tests also withhold
it from an owning-group member (who may still edit) and use a viewer-group member
who may only read.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import (
    Group,
    GroupMembership,
    InternationalResolution,
    Role,
    State,
    Transition,
    TransitionCondition,
    WorkflowType,
)


class StatusTransitionTests(TestCase):
    """Available transitions offered as a Status menu, and then applied."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="status-owner", password="pw")
        # Both are members of the type's owning group, which materialisation gives
        # view, edit and transition to.
        cls.mover = User.objects.create_user(username="status-mover", password="pw")
        cls.editor = User.objects.create_user(username="status-editor", password="pw")
        # A read-only stakeholder: may open the record, may not change it.
        cls.viewer = User.objects.create_user(username="status-viewer", password="pw")
        cls.stranger = User.objects.create_user(
            username="status-stranger", password="pw"
        )

        cls.group = Group.objects.create(name="Status Office", group_type="division")
        cls.viewer_group = Group.objects.create(
            name="Status Committee", group_type="portfolio_committee"
        )
        cls.officer_role = Role.objects.create(name="Status Officer")
        cls.clerk_role = Role.objects.create(name="Status Clerk")
        cls.viewer_role = Role.objects.create(name="Status Observer")
        GroupMembership.objects.create(
            user=cls.mover, group=cls.group, role=cls.officer_role
        )
        GroupMembership.objects.create(
            user=cls.editor, group=cls.group, role=cls.clerk_role
        )
        GroupMembership.objects.create(
            user=cls.viewer, group=cls.viewer_group, role=cls.viewer_role
        )

        cls.workflow_type = WorkflowType.objects.create(
            name="Status Transition Type", group=cls.group
        )
        cls.workflow_type.viewer_groups.add(cls.viewer_group)
        cls.open_state = State.objects.create(
            workflow_type=cls.workflow_type, name="Open", is_initial=True
        )
        cls.assigned = State.objects.create(
            workflow_type=cls.workflow_type, name="Assigned"
        )
        cls.closed = State.objects.create(
            workflow_type=cls.workflow_type, name="Closed", is_terminal=True
        )
        cls.assign = Transition.objects.create(
            workflow_type=cls.workflow_type,
            name="Assign for implementation",
            from_state=cls.open_state,
            to_state=cls.assigned,
            requires_comment=False,
        )
        cls.close = Transition.objects.create(
            workflow_type=cls.workflow_type,
            name="Close",
            from_state=cls.assigned,
            to_state=cls.closed,
            requires_comment=True,
        )

    # -- helpers ------------------------------------------------------------
    def _resolution(self, state=None, **overrides):
        data = {
            "workflow_type": self.workflow_type,
            "current_state": state or self.open_state,
            "resolution_number": "IR-STATUS-1",
            "title": "Status transition test",
            "owner": self.owner,
        }
        data.update(overrides)
        return InternationalResolution.objects.create(**data)

    def _withhold_transitions(self, resolution):
        """Narrow the owning group's grant, as an administrator might.

        The materialised default includes the transition right, so a reader who
        cannot move a record on has either had it withheld (this) or never held it.
        """
        access = resolution.group_accesses().get(group=self.group)
        access.can_transition = False
        access.save(update_fields=["can_transition"])

    def _detail_url(self, resolution):
        return reverse(
            "pwms:international_resolution_detail",
            kwargs={"public_id": resolution.public_id},
        )

    def _transition_url(self, resolution, transition):
        return (
            reverse(
                "pwms:workflow_transition", kwargs={"public_id": resolution.public_id}
            )
            + f"?transition={transition.pk}"
        )

    def _transition_link_markup(self, resolution, transition):
        """How the Referrals tab's row links to a transition, as it renders."""
        url = self._transition_url(resolution, transition)
        return f'<a href="{url}">{transition.name}</a>'

    # -- the menu -----------------------------------------------------------
    def test_detail_page_offers_the_status_menu_with_the_available_transitions(self):
        resolution = self._resolution()
        self.client.force_login(self.mover)

        response = self.client.get(self._detail_url(resolution))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._transition_url(resolution, self.assign))
        # The transition name and the state it moves to label the option.
        self.assertContains(response, "Assign for implementation")
        # A pending state is not offered: it is not reachable from Open.
        self.assertNotContains(response, self._transition_url(resolution, self.close))

    def test_status_menu_is_hidden_when_the_transition_right_is_withheld(self):
        resolution = self._resolution()
        self._withhold_transitions(resolution)
        self.client.force_login(self.editor)

        response = self.client.get(self._detail_url(resolution))

        self.assertEqual(response.status_code, 200)
        # The owning group may still edit its records, so Edit is offered...
        self.assertContains(
            response,
            reverse(
                "pwms:international_resolution_update",
                kwargs={"public_id": resolution.public_id},
            ),
        )
        # ...but moving the record on is a capability of its own.
        self.assertNotContains(response, self._transition_url(resolution, self.assign))

    def test_referrals_tab_lists_the_transitions_as_links(self):
        resolution = self._resolution()
        self.client.force_login(self.mover)

        response = self.client.get(self._detail_url(resolution))

        # The Referrals tab describes the same transitions, and offers each one
        # as a link, so the action is reachable from where they are set out.
        self.assertContains(
            response, self._transition_link_markup(resolution, self.assign)
        )

    def test_referrals_tab_lists_the_transitions_as_facts_without_the_right(self):
        resolution = self._resolution()
        self.client.force_login(self.viewer)

        response = self.client.get(self._detail_url(resolution))

        # The table still describes them — it is reference material — but not as
        # something this reader may act on.
        self.assertContains(response, "Assign for implementation")
        self.assertNotContains(
            response, self._transition_link_markup(resolution, self.assign)
        )

    # -- the confirmation page ---------------------------------------------
    def test_confirm_page_describes_the_transition(self):
        resolution = self._resolution()
        self.client.force_login(self.mover)

        response = self.client.get(self._transition_url(resolution, self.assign))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Change status")
        self.assertContains(response, "Assign for implementation")
        self.assertContains(response, "Assigned")
        self.assertContains(response, self._detail_url(resolution))

    def test_confirm_page_requires_the_transition_right(self):
        resolution = self._resolution()
        self._withhold_transitions(resolution)
        self.client.force_login(self.editor)

        response = self.client.get(self._transition_url(resolution, self.assign))

        self.assertEqual(response.status_code, 403)

    def test_a_stranger_is_refused_too(self):
        resolution = self._resolution()
        self.client.force_login(self.stranger)

        response = self.client.get(self._transition_url(resolution, self.assign))

        self.assertEqual(response.status_code, 403)

    def test_a_transition_not_available_from_this_state_is_a_404(self):
        # `assign` starts from Open, which this record has already left.
        resolution = self._resolution(state=self.assigned)
        self.client.force_login(self.mover)

        response = self.client.get(self._transition_url(resolution, self.assign))

        self.assertEqual(response.status_code, 404)

    def test_a_transition_from_another_workflow_type_is_a_404(self):
        other_type = WorkflowType.objects.create(name="Other Status Type")
        other_start = State.objects.create(
            workflow_type=other_type, name="Start", is_initial=True
        )
        other_end = State.objects.create(workflow_type=other_type, name="End")
        foreign = Transition.objects.create(
            workflow_type=other_type,
            name="Elsewhere",
            from_state=other_start,
            to_state=other_end,
        )
        resolution = self._resolution()
        self.client.force_login(self.mover)

        response = self.client.get(self._transition_url(resolution, foreign))

        self.assertEqual(response.status_code, 404)

    # -- applying one -------------------------------------------------------
    def test_post_applies_the_transition_and_lands_on_the_timeline(self):
        resolution = self._resolution()
        self.client.force_login(self.mover)

        response = self.client.post(
            self._transition_url(resolution, self.assign),
            {"transition": self.assign.pk, "comment": "Handed to the unit."},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], f"{self._detail_url(resolution)}#pane-timeline"
        )
        resolution.refresh_from_db()
        self.assertEqual(resolution.current_state, self.assigned)

        log = resolution.audit_logs().get()
        self.assertEqual(log.action, "STATE_TRANSITION")
        self.assertEqual(log.from_state, self.open_state)
        self.assertEqual(log.to_state, self.assigned)
        self.assertEqual(log.actor, self.mover)
        self.assertEqual(log.notes, "Handed to the unit.")

    def test_a_comment_required_transition_is_refused_without_one(self):
        resolution = self._resolution(state=self.assigned)
        self.client.force_login(self.mover)

        response = self.client.post(
            self._transition_url(resolution, self.close), {"transition": self.close.pk}
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "A comment is required for this transition.",
            status_code=400,
        )
        resolution.refresh_from_db()
        self.assertEqual(resolution.current_state, self.assigned)
        self.assertFalse(resolution.audit_logs().exists())

    def test_a_comment_required_transition_is_applied_with_one(self):
        resolution = self._resolution(state=self.assigned)
        self.client.force_login(self.mover)

        response = self.client.post(
            self._transition_url(resolution, self.close),
            {"transition": self.close.pk, "comment": "Implemented in full."},
        )

        self.assertEqual(response.status_code, 302)
        resolution.refresh_from_db()
        self.assertEqual(resolution.current_state, self.closed)
        self.assertEqual(resolution.audit_logs().get().notes, "Implemented in full.")

    def test_an_unmet_condition_is_explained_and_blocks_the_post(self):
        resolution = self._resolution()
        TransitionCondition.objects.create(
            transition=self.assign,
            condition_type="field_set",
            field_name="resolution_text",
        )
        self.client.force_login(self.mover)

        # The page names the guard before the reader commits to anything.
        page = self.client.get(self._transition_url(resolution, self.assign))
        self.assertContains(page, "This transition cannot be taken yet.")
        self.assertContains(page, "resolution_text")

        # The model refuses it too, and the comment already typed is kept.
        response = self.client.post(
            self._transition_url(resolution, self.assign),
            {"transition": self.assign.pk, "comment": "Trying anyway."},
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "resolution_text", status_code=400)
        self.assertContains(response, "Trying anyway.", status_code=400)
        resolution.refresh_from_db()
        self.assertEqual(resolution.current_state, self.open_state)
        self.assertFalse(resolution.audit_logs().exists())
