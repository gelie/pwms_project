from django.contrib.auth import get_user_model
from django.urls import reverse
from django_flatpickr.widgets import DatePickerInput, DateTimePickerInput

from ..models import (
    Bill,
    City,
    Country,
    DelegationParticipant,
    DelegationReport,
    Group,
    InternationalAgreement,
    InternationalResolution,
)
from .views_crud_base import WorkflowCrudViewTestBase


class WorkflowFormViewTests(WorkflowCrudViewTestBase):
    """Workflow create/update forms: child lists, state protection and pickers."""

    def test_report_form_records_delegates(self):
        """Delegates chosen through the user lookup are saved with their role."""
        delegate = get_user_model().objects.create_user(
            username="mokoena", first_name="Naledi", last_name="Mokoena"
        )
        payload = self._report_payload()
        payload["participants-0-user"] = delegate.pk
        payload["participants-0-delegation_role"] = "Leader of the Delegation"

        response = self.client.post(reverse("pwms:delegation_report_create"), payload)
        report = DelegationReport.objects.get(title="CRUD report")
        self.assertRedirects(
            response,
            reverse("pwms:delegation_report_detail", args=[report.public_id]),
        )

        participant = report.participants.get()
        self.assertEqual(participant.user, delegate)
        self.assertEqual(participant.delegation_role, "Leader of the Delegation")
        # The person's names are taken from the account the row picked.
        self.assertEqual(participant.first_name, "Naledi")
        self.assertEqual(participant.last_name, "Mokoena")

        # The edit form lists the delegate again as one of the report's cards.
        edit = self.client.get(
            reverse("pwms:delegation_report_update", args=[report.public_id])
        )
        self.assertContains(edit, delegate.display_name)
        self.assertContains(edit, "Leader of the Delegation")

    def test_report_form_rejects_the_same_delegate_twice(self):
        """One person cannot be listed twice on the same report."""
        delegate = get_user_model().objects.create_user(
            username="twice", first_name="Tanya", last_name="Twice"
        )
        payload = self._report_payload()
        payload.update(
            {
                "participants-TOTAL_FORMS": "2",
                "participants-0-user": delegate.pk,
                "participants-0-delegation_role": "Delegate",
                "participants-1-id": "",
                "participants-1-user": delegate.pk,
                "participants-1-participant_type": DelegationParticipant.MEMBER,
                "participants-1-delegation_role": "Head Delegate",
                "participants-1-DELETE": "",
            }
        )

        response = self.client.post(reverse("pwms:delegation_report_create"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DelegationReport.objects.exists())
        # The clash is reported against the row, not swallowed.
        self.assertContains(response, "is already on this delegation")

    def test_report_form_rejects_the_same_resolution_number_twice(self):
        """Two rows cannot claim the same resolution number."""
        payload = self._report_payload()
        payload.update(
            {
                "resolutions-TOTAL_FORMS": "2",
                "resolutions-0-resolution_number": "IR-TWICE-1",
                "resolutions-0-title": "First outcome",
                "resolutions-1-resolution_number": "IR-TWICE-1",
                "resolutions-1-title": "Second outcome",
                "resolutions-1-adoption_date": "",
                "resolutions-1-DELETE": "",
            }
        )

        response = self.client.post(reverse("pwms:delegation_report_create"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DelegationReport.objects.exists())
        self.assertContains(response, "is listed twice")

    def test_report_form_offers_one_line_adders(self):
        """Delegates and resolutions are added from a one-line row, not blank rows."""
        response = self.client.get(reverse("pwms:delegation_report_create"))

        self.assertEqual(response.status_code, 200)
        # Each adder posts its entry through the formset it belongs to.
        self.assertContains(response, "participants-TOTAL_FORMS")
        self.assertContains(response, "resolutions-TOTAL_FORMS")
        self.assertContains(response, 'id="id_delegate_adder-user_search"')
        self.assertContains(response, 'id="id_resolution_adder-title"')
        # Nothing is pre-rendered: the page builds each row when one is added.
        self.assertNotContains(response, 'name="participants-0-user"')
        self.assertNotContains(response, 'name="resolutions-0-resolution_number"')
        # The hidden <template> holds the empty form the page clones, and each
        # card slot names the adder control whose value fills it in.
        self.assertContains(response, "participants-__prefix__-user")
        self.assertContains(response, "resolutions-__prefix__-title")
        self.assertContains(response, 'data-adder-slot="delegation_role"')
        self.assertContains(response, "data-adder-remove")

    def test_report_form_creates_and_links_child_resolutions(self):
        """A resolution entered on the report form is created and nested under it."""
        payload = self._report_payload()
        payload["resolutions-0-resolution_number"] = "IR-CHILD-1"
        payload["resolutions-0-title"] = "Implement the outcome"

        response = self.client.post(reverse("pwms:delegation_report_create"), payload)
        report = DelegationReport.objects.get(title="CRUD report")
        self.assertRedirects(
            response,
            reverse("pwms:delegation_report_detail", args=[report.public_id]),
        )

        resolution = InternationalResolution.objects.get(resolution_number="IR-CHILD-1")
        self.assertEqual(resolution.title, "Implement the outcome")
        self.assertEqual(resolution.workflow_type, self.resolution_type)
        self.assertEqual(
            resolution.current_state, self.resolution_type.get_initial_state()
        )
        self.assertEqual(resolution.owner, self.user)
        self.assertEqual(report.get_all_descendants(), [resolution])

    def test_resolution_form_links_to_an_existing_report(self):
        """The resolution form nests the resolution under a chosen report."""
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Parent report"),
        )
        report = DelegationReport.objects.get(title="Parent report")

        response = self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(parent_report=report.pk),
        )
        resolution = InternationalResolution.objects.get(title="CRUD resolution")
        self.assertRedirects(
            response,
            reverse(
                "pwms:international_resolution_detail", args=[resolution.public_id]
            ),
        )
        self.assertEqual(resolution.parent_workflow, report)
        self.assertEqual(report.get_all_descendants(), [resolution])

    def test_resolution_update_can_detach_from_its_report(self):
        """Clearing the parent picker on the edit form detaches the resolution."""
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(title="Parent report"),
        )
        report = DelegationReport.objects.get(title="Parent report")
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(parent_report=report.pk),
        )
        resolution = InternationalResolution.objects.get(title="CRUD resolution")
        self.assertEqual(resolution.parent_workflow, report)

        response = self.client.post(
            reverse(
                "pwms:international_resolution_update", args=[resolution.public_id]
            ),
            self._resolution_payload(
                current_state=resolution.current_state.pk, parent_report=""
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(
            InternationalResolution.objects.get(pk=resolution.pk).parent_workflow
        )

    def test_resolution_edit_form_shows_the_state_read_only(self):
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(),
        )
        resolution = InternationalResolution.objects.get(resolution_number="IR-CRUD-1")

        response = self.client.get(
            reverse("pwms:international_resolution_update", args=[resolution.public_id])
        )

        self.assertEqual(response.status_code, 200)
        field = response.context["form"].fields["current_state"]
        self.assertTrue(field.disabled)
        # Only the record's own state is offered, so there is nothing to pick.
        self.assertEqual(list(field.queryset), [resolution.current_state])
        self.assertRegex(
            response.content.decode(),
            r'<select[^>]*name="current_state"[^>]*\bdisabled\b',
        )
        self.assertContains(response, "not by editing it")

    def test_resolution_update_cannot_move_the_state(self):
        """Only a transition changes the state, even on a hand-crafted POST."""
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(),
        )
        resolution = InternationalResolution.objects.get(resolution_number="IR-CRUD-1")
        original = resolution.current_state_id
        other = self.resolution_type.states.exclude(pk=original).first()
        self.assertIsNotNone(other)

        response = self.client.post(
            reverse(
                "pwms:international_resolution_update", args=[resolution.public_id]
            ),
            self._resolution_payload(current_state=other.pk, priority="high"),
        )

        self.assertRedirects(
            response,
            reverse(
                "pwms:international_resolution_detail", args=[resolution.public_id]
            ),
        )
        resolution.refresh_from_db()
        self.assertEqual(resolution.current_state_id, original)
        # The rest of the form still saved.
        self.assertEqual(resolution.priority, "high")

    def _protected_state_cases(self):
        """``(name, create URL, update URL name, model, payload, lookup)`` per form."""
        return (
            (
                "delegation report",
                "pwms:delegation_report_create",
                "pwms:delegation_report_update",
                DelegationReport,
                self._report_payload,
                {"engagement_name": "CRUD engagement"},
            ),
            (
                "international agreement",
                "pwms:international_agreement_create",
                "pwms:international_agreement_update",
                InternationalAgreement,
                self._agreement_payload,
                {"title": "CRUD agreement"},
            ),
        )

    def test_report_and_agreement_edit_forms_show_the_state_read_only(self):
        for (
            name,
            create,
            update,
            model,
            payload,
            lookup,
        ) in self._protected_state_cases():
            with self.subTest(form=name):
                self.client.post(reverse(create), payload())
                instance = model.objects.get(**lookup)

                response = self.client.get(reverse(update, args=[instance.public_id]))

                self.assertEqual(response.status_code, 200)
                field = response.context["form"].fields["current_state"]
                self.assertTrue(field.disabled)
                self.assertEqual(list(field.queryset), [instance.current_state])
                self.assertRegex(
                    response.content.decode(),
                    r'<select[^>]*name="current_state"[^>]*\bdisabled\b',
                )
                self.assertContains(response, "not by editing it")

    def test_report_and_agreement_updates_cannot_move_the_state(self):
        """Only a transition changes the state, even on a hand-crafted POST."""
        for (
            name,
            create,
            update,
            model,
            payload,
            lookup,
        ) in self._protected_state_cases():
            with self.subTest(form=name):
                self.client.post(reverse(create), payload())
                instance = model.objects.get(**lookup)
                original = instance.current_state_id
                other = instance.workflow_type.states.exclude(pk=original).first()
                self.assertIsNotNone(other)

                response = self.client.post(
                    reverse(update, args=[instance.public_id]),
                    payload(current_state=other.pk, priority="high"),
                )

                self.assertEqual(response.status_code, 302)
                instance.refresh_from_db()
                self.assertEqual(instance.current_state_id, original)
                # The rest of the form still saved.
                self.assertEqual(instance.priority, "high")

    def test_resolution_create_form_has_no_state_field(self):
        response = self.client.get(reverse("pwms:international_resolution_create"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("current_state", response.context["form"].fields)

    def test_create_form_hides_the_type_and_owner(self):
        """The type is implicit and the owner is the acting user, so both are hidden."""
        response = self.client.get(reverse("pwms:delegation_report_create"))

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        # Submitted, but not controls the user can change.
        self.assertNotContains(response, '<select name="workflow_type"')
        self.assertContains(response, '<input type="hidden" name="workflow_type"')
        self.assertEqual(form["workflow_type"].value(), self.report_type.pk)
        self.assertNotContains(response, '<select name="owner"')
        self.assertContains(response, '<input type="hidden" name="owner"')
        self.assertEqual(form["owner"].value(), self.user.pk)

    def test_create_forms_use_flatpickr_date_pickers(self):
        """Date/time fields are flatpickr widgets and the page loads their media."""
        cases = (
            ("pwms:delegation_report_create", "deadline", "engagement_start_date"),
            (
                "pwms:international_resolution_create",
                "deadline",
                "adoption_date",
            ),
        )
        for url_name, datetime_field, date_field in cases:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                form = response.context["form"]
                self.assertIsInstance(
                    form.fields[datetime_field].widget, DateTimePickerInput
                )
                self.assertIsInstance(form.fields[date_field].widget, DatePickerInput)
                # Without the package's media on the page the inputs stay plain text.
                self.assertContains(response, "flatpickr.min.css")
                self.assertContains(response, "flatpickr.min.js")
                self.assertContains(response, "js/django-flatpickr.js")
                self.assertContains(response, "data-fpconfig")

    def test_group_search_returns_matching_groups_for_htmx(self):
        """The picker's endpoint returns the results fragment, not a full page."""
        response = self.client.get(
            reverse("pwms:group_search"), {"search": "Test Workflow"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/partials/group_search_results.html")
        self.assertContains(response, self.group.name)
        self.assertContains(response, f"selectGroup('{self.group.pk}'")

    def test_ministry_search_returns_matching_ministries_for_htmx(self):
        """The sponsor-name lookup offers ministries only."""
        ministry = Group.objects.create(
            name="Ministry of Testing", short_name="MoT", group_type="ministry"
        )
        Group.objects.create(name="Ministry of Other", group_type="ministry")
        Group.objects.create(
            name="Portfolio Committee on Testing", group_type="committee"
        )

        response = self.client.get(
            reverse("pwms:ministry_search"), {"search": "Testing"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/partials/ministry_search_results.html")
        self.assertContains(response, ministry.name)
        self.assertContains(
            response, f"selectOption('{ministry.name}', '{ministry.name}')"
        )
        # Neither a non-matching ministry nor a committee is offered.
        self.assertNotContains(response, "Ministry of Other")
        self.assertNotContains(response, "Portfolio Committee on Testing")

    def test_bill_form_uses_a_ministry_lookup_for_sponsor_name(self):
        """The sponsor-name box searches ministries and stores the chosen name."""
        response = self.client.get(reverse("pwms:bill_create"))

        self.assertContains(response, 'id="id_sponsor_name_search"')
        self.assertContains(response, f'hx-get="{reverse("pwms:ministry_search")}"')
        self.assertContains(response, '<input type="hidden" name="sponsor_name"')
        self.assertNotContains(response, '<input type="text" name="sponsor_name"')
        # An empty create form leaves the box blank so its placeholder shows;
        # Django renders an unset bound value as the literal string "None".
        self.assertNotContains(response, 'value="None"')

    def test_bill_sponsor_name_round_trips_through_the_lookup(self):
        """The name the picker writes into the hidden field is what is saved."""
        self.client.post(
            reverse("pwms:bill_create"),
            self._bill_payload(sponsor_name="Ministry of Testing"),
        )
        bill = Bill.objects.get(bill_number="B 99—2026")
        self.assertEqual(bill.sponsor_name, "Ministry of Testing")

        response = self.client.get(reverse("pwms:bill_update", args=[bill.public_id]))
        self.assertContains(response, 'value="Ministry of Testing"')

    def test_bill_committee_options_are_limited_to_house_committees(self):
        """Only NA and NCOP committees may be chosen as responsible committee."""
        na = Group.objects.create(name="National Assembly", group_type="house")
        na_committees = Group.objects.create(
            name="NA Committees", group_type="committees", parent=na
        )
        na_committee = Group.objects.create(
            name="Portfolio Committee on Testing",
            group_type="committee",
            parent=na_committees,
        )
        ncop = Group.objects.create(
            name="National Council of Provinces", group_type="house"
        )
        ncop_committees = Group.objects.create(
            name="NCOP Committees", group_type="committees", parent=ncop
        )
        ncop_committee = Group.objects.create(
            name="Select Committee on Testing",
            group_type="committee",
            parent=ncop_committees,
        )

        parliament = Group.objects.create(name="Parliament", group_type="parliament")
        joint_committees = Group.objects.create(
            name="Joint Committees", group_type="committees", parent=parliament
        )
        Group.objects.create(
            name="Joint Standing Committee on Testing",
            group_type="committee",
            parent=joint_committees,
        )
        Group.objects.create(name="Ministry of Testing", group_type="ministry")

        # More groups than the picker threshold, but only the two eligible
        # committees count: the narrowing must run before the picker decision.
        self._add_extra_groups()

        response = self.client.get(reverse("pwms:bill_create"))
        form = response.context["form"]
        self.assertEqual(
            set(form.fields["responsible_committee"].queryset),
            {na_committee, ncop_committee},
        )
        self.assertNotIn("responsible_committee", form.search_pickers)

        # The lookup behind the picker is scoped the same way.
        results = self.client.get(
            reverse("pwms:bill_committee_search"), {"search": "Testing"}
        )
        self.assertContains(results, na_committee.name)
        self.assertContains(results, ncop_committee.name)
        self.assertNotContains(results, "Joint Standing Committee on Testing")
        self.assertNotContains(results, "Ministry of Testing")

    def test_bill_committee_picker_uses_the_scoped_lookup(self):
        """Once the list is long, the picker searches only house committees."""
        na = Group.objects.create(name="National Assembly", group_type="house")
        container = Group.objects.create(
            name="NA Committees", group_type="committees", parent=na
        )
        for number in range(11):
            Group.objects.create(
                name=f"Portfolio Committee {number}",
                group_type="committee",
                parent=container,
            )

        response = self.client.get(reverse("pwms:bill_create"))

        self.assertIn("responsible_committee", response.context["form"].search_pickers)
        self.assertContains(
            response, f'hx-get="{reverse("pwms:bill_committee_search")}"'
        )

    def test_short_option_lists_stay_selects(self):
        """With few options the fields keep their plain <select>."""
        response = self.client.get(reverse("pwms:international_resolution_create"))

        self.assertEqual(response.context["form"].search_pickers, set())
        # owner is never chosen by hand, so it is hidden rather than a control.
        self.assertContains(response, '<input type="hidden" name="owner"')
        self.assertContains(response, '<select name="assigned_to"')
        self.assertContains(response, '<select name="responsible_group"')
        self.assertNotContains(response, 'id="id_assigned_to_search"')

    def test_long_option_lists_become_search_pickers(self):
        """More options than the threshold switches the fields to search fields."""
        self._add_extra_users()
        self._add_extra_groups()

        for url_name in (
            "pwms:delegation_report_create",
            "pwms:international_resolution_create",
        ):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                form = response.context["form"]

                self.assertEqual(response.status_code, 200)
                # owner is never a picker: it is hidden and fixed server-side.
                self.assertNotIn("owner", form.search_pickers)
                self.assertIn("assigned_to", form.search_pickers)
                # A <select> with every active user is not rendered.
                self.assertNotContains(response, '<select name="assigned_to"')
                self.assertContains(response, 'id="id_assigned_to_search"')
                self.assertContains(response, f'hx-get="{reverse("pwms:user_search")}"')
                # The hidden owner still carries the acting user.
                self.assertContains(response, '<input type="hidden" name="owner"')
                self.assertEqual(form["owner"].value(), self.user.pk)

    def test_group_field_becomes_a_picker_only_when_the_list_is_long(self):
        """The group picker follows the same threshold as the user fields."""
        url = reverse("pwms:international_resolution_create")
        short = self.client.get(url)
        self.assertNotIn("responsible_group", short.context["form"].search_pickers)

        self._add_extra_groups()
        long = self.client.get(url)
        self.assertIn("responsible_group", long.context["form"].search_pickers)
        self.assertNotContains(long, '<select name="responsible_group"')
        self.assertContains(long, 'id="id_responsible_group_search"')
        self.assertContains(long, f'hx-get="{reverse("pwms:group_search")}"')
        self.assertContains(long, 'hx-target="#id_responsible_group_results"')

    def test_picked_group_round_trips_through_the_form(self):
        """What the search stores is what the model keeps, and edit shows it."""
        self._add_extra_groups()
        self.client.post(
            reverse("pwms:international_resolution_create"),
            self._resolution_payload(responsible_group=self.group.pk),
        )
        resolution = InternationalResolution.objects.get(title="CRUD resolution")
        self.assertEqual(resolution.responsible_group, self.group)

        response = self.client.get(
            reverse("pwms:international_resolution_update", args=[resolution.public_id])
        )
        # The hidden input carries the pk; the search box carries the name.
        self.assertContains(
            response, f'value="{self.group.pk}" id="id_responsible_group"'
        )
        self.assertContains(response, f'value="{self.group.name}"')

    def test_user_search_returns_matching_users_for_htmx(self):
        """The user picker's endpoint returns the results fragment, not a page."""
        response = self.client.get(
            reverse("pwms:user_search"), {"search": self.user.username}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/partials/user_search_results.html")
        self.assertContains(response, self.user.display_name)
        self.assertContains(response, f"selectUser('{self.user.pk}'")

    def test_picked_users_round_trip_through_the_form(self):
        """The assignee picked in the search is saved; the owner stays the actor."""
        self._add_extra_users()
        assignee = get_user_model().objects.create_user(
            username="assignee", first_name="Ada", last_name="Assignee"
        )
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(owner=self.user.pk, assigned_to=assignee.pk),
        )
        report = DelegationReport.objects.get(title="CRUD report")
        self.assertEqual(report.owner, self.user)
        self.assertEqual(report.assigned_to, assignee)

        response = self.client.get(
            reverse("pwms:delegation_report_update", args=[report.public_id])
        )
        self.assertContains(response, f'value="{assignee.pk}" id="id_assigned_to"')
        self.assertContains(response, f'value="{assignee.display_name}"')

    def test_location_fields_become_linked_pickers(self):
        """The location pair follows the threshold and is wired country -> city."""
        url = reverse("pwms:delegation_report_create")
        short = self.client.get(url)
        self.assertNotIn("location_country", short.context["form"].search_pickers)
        self.assertContains(short, '<select name="location_country"')

        for number in range(11):
            country = Country.objects.create(
                code=chr(65 + number), name=f"Test Country {number}"
            )
            City.objects.create(
                country=country,
                name=f"Test City {number}",
                latitude="1.5",
                longitude="2.5",
                population=1000,
            )

        response = self.client.get(url)
        form = response.context["form"]
        self.assertIn("location_country", form.search_pickers)
        self.assertIn("location_city", form.search_pickers)
        self.assertNotContains(response, '<select name="location_country"')
        self.assertNotContains(response, '<select name="location_city"')
        self.assertContains(response, 'id="id_location_country_search"')
        self.assertContains(response, 'id="id_location_city_search"')
        self.assertContains(response, f'hx-get="{reverse("pwms:country_search")}"')
        self.assertContains(response, f'hx-get="{reverse("pwms:city_search")}"')
        # The city search sends the selected country, and the country picker
        # resets the city when it changes.
        self.assertContains(
            response,
            'hx-vals=\'js:{"country": document.getElementById("id_location_country").value}\'',
        )
        self.assertContains(response, 'data-clears="#id_location_city_picker"')

    def test_location_round_trips_through_the_form(self):
        """A picked country/city is saved and shown again on the edit form."""
        country = Country.objects.create(code="ZZ", iso3="ZZZ", name="Testland")
        city = City.objects.create(
            country=country,
            name="Testville",
            latitude="1.5",
            longitude="2.5",
            population=42,
        )
        self.client.post(
            reverse("pwms:delegation_report_create"),
            self._report_payload(location_country=country.pk, location_city=city.pk),
        )
        report = DelegationReport.objects.get(title="CRUD report")
        self.assertEqual(report.location_country, country)
        self.assertEqual(report.location_city, city)

        response = self.client.get(
            reverse("pwms:delegation_report_update", args=[report.public_id])
        )
        form = response.context["form"]
        self.assertEqual(str(form["location_country"].value()), str(country.pk))
        self.assertEqual(form.picker_labels["location_city"], "Testville")

    def test_bill_edit_renders_the_state_readonly_and_ignores_a_crafted_post(self):
        self.client.post(reverse("pwms:bill_create"), self._bill_payload())
        bill = Bill.objects.get(bill_number="B 99—2026")
        original = bill.current_state_id
        other_state = self.bill_type.states.exclude(pk=original).first()
        self.assertIsNotNone(other_state)

        edit = self.client.get(reverse("pwms:bill_update", args=[bill.public_id]))
        # The select stays visible but read-only: this instance's state is the
        # only option, Django disables it (POSTed values are ignored), and the
        # hint points at the transition path.
        self.assertRegex(edit.content.decode(), r'name="current_state"[^>]*disabled')
        self.assertNotContains(edit, f">{other_state.name}</option>")
        self.assertContains(edit, "state machine")

        response = self.client.post(
            reverse("pwms:bill_update", args=[bill.public_id]),
            {
                "title": "CRUD bill (edited)",
                "description": "",
                "current_state": other_state.pk,
                "owner": self.user.pk,
                "assigned_to": "",
                "deadline": "",
                "priority": "high",
                "bill_number": "B 99—2026",
                "short_title": "CRUD",
                "bill_type": Bill.SECTION_75,
                "house_of_origin": Bill.NA,
                "sponsor_name": "Minister of Justice",
                "introduced_date": "",
                "responsible_committee": "",
                "atc_reference": "",
                "order_paper_reference": "",
                "bill_document_url": "",
                "notes": "",
            },
        )
        self.assertRedirects(
            response, reverse("pwms:bill_detail", args=[bill.public_id])
        )
        bill.refresh_from_db()
        # The state did not move: only a transition may change it.
        self.assertEqual(bill.current_state_id, original)
        self.assertEqual(bill.priority, "high")
