"""The standard colour scheme for workflow states (``pwms.state_colors``).

One colour per role (initial, in progress, terminal, unsuccessful) across every
workflow type, kept on ``State.color`` and shown by every list and detail page.
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import state_colors
from .models import State
from .tests_detail import DetailPageTestCase


def _contrast_with_white(hex_color):
    """WCAG contrast ratio of ``hex_color`` against white."""
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return 1.05 / (luminance + 0.05)


class StateColorRuleTests(SimpleTestCase):
    def test_each_role_has_its_own_colour(self):
        self.assertEqual(
            state_colors.state_color(is_initial=True, is_terminal=False),
            state_colors.INITIAL,
        )
        self.assertEqual(
            state_colors.state_color(is_initial=False, is_terminal=False),
            state_colors.IN_PROGRESS,
        )
        self.assertEqual(
            state_colors.state_color(is_initial=False, is_terminal=True),
            state_colors.TERMINAL,
        )

    def test_the_four_colours_are_distinct(self):
        colours = {
            state_colors.INITIAL,
            state_colors.IN_PROGRESS,
            state_colors.TERMINAL,
            state_colors.UNSUCCESSFUL,
        }
        self.assertEqual(len(colours), 4)

    def test_withdrawn_lapsed_and_cancelled_are_red(self):
        for name in (
            "Withdrawn",
            "Lapsed",
            "Cancelled",
            "Canceled",
            "withdrawn",
            "Bill Lapsed",
            "Lapsed – session ended",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    state_colors.state_color(
                        is_initial=False, is_terminal=True, name=name
                    ),
                    state_colors.UNSUCCESSFUL,
                )

    def test_a_red_name_wins_even_without_the_terminal_flag(self):
        self.assertEqual(
            state_colors.state_color(is_initial=False, is_terminal=False, name="Lapsed"),
            state_colors.UNSUCCESSFUL,
        )

    def test_a_name_merely_containing_the_letters_is_not_red(self):
        for name in ("Withdrawal pending", "Collapsed view", "Closed"):
            with self.subTest(name=name):
                self.assertNotEqual(
                    state_colors.state_color(
                        is_initial=False, is_terminal=True, name=name
                    ),
                    state_colors.UNSUCCESSFUL,
                )

    def test_every_colour_is_legible_under_white_badge_text(self):
        for colour in (
            state_colors.INITIAL,
            state_colors.IN_PROGRESS,
            state_colors.TERMINAL,
            state_colors.UNSUCCESSFUL,
        ):
            with self.subTest(colour=colour):
                self.assertGreaterEqual(_contrast_with_white(colour), 4.5)

    def test_the_migration_copy_of_the_rule_has_not_drifted(self):
        migration = import_module("pwms.migrations.0059_state_standard_colors")
        for is_initial in (True, False):
            for is_terminal in (True, False):
                for name in ("Open", "Closed", "Withdrawn", "Lapsed", "Cancelled"):
                    with self.subTest(
                        is_initial=is_initial, is_terminal=is_terminal, name=name
                    ):
                        state = SimpleNamespace(
                            name=name, is_initial=is_initial, is_terminal=is_terminal
                        )
                        self.assertEqual(
                            migration.standard_color(state),
                            state_colors.state_color(
                                is_initial=is_initial,
                                is_terminal=is_terminal,
                                name=name,
                            ),
                        )


class SeededStateColorTests(TestCase):
    """Migration 0059 left every seeded state on its standard colour."""

    def test_every_state_carries_its_standard_colour(self):
        states = list(State.objects.select_related("workflow_type"))
        self.assertTrue(states)
        for state in states:
            with self.subTest(state=str(state)):
                self.assertEqual(state.color, state.default_color)

    def _colour(self, workflow_type, name):
        return State.objects.get(workflow_type__name=workflow_type, name=name).color

    def test_the_same_role_looks_the_same_in_every_workflow(self):
        self.assertEqual(self._colour("Bill", "Introduced"), state_colors.INITIAL)
        self.assertEqual(self._colour("Meeting", "Scheduled"), state_colors.INITIAL)
        self.assertEqual(
            self._colour("International Resolution", "Captured"), state_colors.INITIAL
        )
        self.assertEqual(
            self._colour("Bill", "Committee Deliberation"), state_colors.IN_PROGRESS
        )
        self.assertEqual(self._colour("Meeting", "In Progress"), state_colors.IN_PROGRESS)
        self.assertEqual(
            self._colour("International Resolution", "In Progress"),
            state_colors.IN_PROGRESS,
        )

    def test_successful_endings_share_one_colour(self):
        endings = {
            self._colour("Bill", "Signed into Law"),
            self._colour("Meeting", "Completed"),
            self._colour("International Resolution", "Closed"),
            self._colour("International Agreement", "Closed – House approved"),
            self._colour("Delegation Report", "Closed – House approved"),
        }
        self.assertEqual(endings, {state_colors.TERMINAL})

    def test_unsuccessful_endings_are_red(self):
        self.assertEqual(self._colour("Bill", "Withdrawn"), state_colors.UNSUCCESSFUL)
        self.assertEqual(self._colour("Meeting", "Cancelled"), state_colors.UNSUCCESSFUL)

    def test_a_new_state_defaults_to_the_in_progress_colour(self):
        self.assertEqual(
            State._meta.get_field("color").default, state_colors.IN_PROGRESS
        )


class StateColorPageTests(DetailPageTestCase):
    """The pages show the stored colour, not a fixed grey or green."""

    def _badge(self, state):
        return f'style="--state-color: {state.color}">{state.name}</span>'

    def test_the_workflow_register_colours_the_state(self):
        report = self.make_report()

        response = self.client.get(reverse("pwms:workflows"))

        self.assertContains(response, self._badge(report.current_state))
        self.assertNotContains(response, "text-bg-secondary\">Awaiting")

    def test_each_instrument_list_colours_the_state(self):
        report = self.make_report()
        agreement = self.make_agreement()

        for name, instance in (
            ("delegation_reports", report),
            ("international_agreements", agreement),
        ):
            with self.subTest(list=name):
                response = self.client.get(reverse(f"pwms:{name}"))
                self.assertContains(response, self._badge(instance.current_state))

    def test_the_detail_page_uses_the_same_markup_as_the_lists(self):
        report = self.make_report()

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[report.public_id])
        )

        self.assertContains(response, self._badge(report.current_state))

    def test_a_changed_state_colour_shows_up_everywhere(self):
        report = self.make_report()
        state = report.current_state
        state.color = "#123abc"
        state.save(update_fields=["color"])

        for url in (
            reverse("pwms:workflows"),
            reverse("pwms:delegation_reports"),
            reverse("pwms:delegation_report_detail", args=[report.public_id]),
        ):
            with self.subTest(url=url):
                self.assertContains(
                    self.client.get(url),
                    f'style="--state-color: #123abc">{state.name}</span>',
                )
