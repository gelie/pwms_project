"""Forms for the group directory, memberships and duty delegations."""

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from django_flatpickr.widgets import DatePickerInput

from ..models import DutyDelegation, Group, GroupMembership, Role
from .base import SearchPickerMixin

User = get_user_model()


class GroupForm(forms.ModelForm):
    """Edit a group's own descriptive fields.

    Only the fields a committee secretary maintains on their committee are
    exposed. Structure (parent, group type, term, external id) stays in the
    admin: administering a committee must not reshape the parliamentary tree.
    """

    class Meta:
        model = Group
        fields = [
            "name",
            "prefix",
            "short_name",
            "description",
            "contact_email",
            "contact_phone",
            "location",
            "start_date",
            "end_date",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "prefix": forms.TextInput(attrs={"class": "form-control"}),
            "short_name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "contact_email": forms.EmailInput(attrs={"class": "form-control"}),
            "contact_phone": forms.TextInput(attrs={"class": "form-control"}),
            "location": forms.TextInput(attrs={"class": "form-control"}),
            "start_date": DatePickerInput(attrs={"class": "form-control"}),
            "end_date": DatePickerInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class GroupMembershipForm(SearchPickerMixin, forms.ModelForm):
    """Add or edit one person's membership of a group.

    The group and the ``updated_by`` trail are supplied by the view, not chosen
    here: the page the form is rendered on already is that group. The person and
    the role are picked through type-ahead lookups rather than long selects.
    """

    picker_fields = ("user", "role")

    class Meta:
        model = GroupMembership
        fields = ["user", "role", "start_date", "end_date", "is_active", "notes"]
        widgets = {
            "user": forms.Select(attrs={"class": "form-select"}),
            "role": forms.Select(attrs={"class": "form-select"}),
            "start_date": DatePickerInput(attrs={"class": "form-control"}),
            "end_date": DatePickerInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, group=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.group = group
        users = User.objects.filter(is_active=True)
        # Keep the row's current person selectable when editing a membership
        # that outlived their account (ending it must not require reassigning it).
        if self.instance.pk and self.instance.user_id:
            users = User.objects.filter(Q(is_active=True) | Q(pk=self.instance.user_id))
        self.fields["user"].queryset = users.order_by("last_name", "first_name")
        # A committee secretary may hand out the ordinary roles a committee
        # uses, but not mint a global manager: a role carrying
        # ``can_manage_permissions`` is left to the administrators (except the
        # role the row already holds, which must stay selectable when editing).
        roles = Role.objects.filter(can_manage_permissions=False)
        if self.instance.pk and self.instance.role_id:
            roles = Role.objects.filter(
                Q(pk__in=roles.values("pk")) | Q(pk=self.instance.role_id)
            ).distinct()
        self.fields["role"].queryset = roles.order_by("name")
        if self.instance.pk and self.instance.is_committee_secretary:
            # Ending the secretary is blocked (model and view enforce it); make
            # the form show why rather than offering a control that will fail.
            self.fields["is_active"].disabled = True
            self.fields[
                "is_active"
            ].help_text = Group.COMMITTEE_SECRETARY_PROTECTED_MESSAGE
        self.enable_pickers()


class DutyDelegationForm(SearchPickerMixin, forms.ModelForm):
    """Record one member's delegation of their duties in this group.

    The committee secretary records the delegation on the member's behalf, so
    the delegator is chosen here from the group's active members. The delegate
    may be any active PWMS user outside the group and is matched to the
    delegator's kind: Members of Parliament to Members of Parliament, staff to
    staff. Existing group members and current delegates are not offered. Both
    people are picked through a type-ahead rather than a <select> of every
    account.
    """

    picker_fields = ("delegator", "delegate")

    class Meta:
        model = DutyDelegation
        fields = ["delegator", "delegate", "end_date", "notes"]
        widgets = {
            "end_date": DatePickerInput(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, group=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.group = group
        if group is not None:
            # Setting the group on the instance lets ``DutyDelegation.clean``
            # check that the chosen delegator is one of its active members.
            self.instance.group = group
            member_ids = GroupMembership.objects.filter(
                group=group, is_active=True
            ).values_list("user_id", flat=True)
            self.fields["delegator"].queryset = User.objects.filter(
                id__in=member_ids, is_active=True
            ).order_by("last_name", "first_name")
            # The delegate must sit outside the group and must not already be
            # acting as one. Mirror the type-ahead's exclusions here so the
            # field's own choices agree with the picker.
            current_delegates = (
                DutyDelegation.objects.current()
                .filter(group=group)
                .values("delegate_id")
            )
            self.fields["delegate"].queryset = (
                User.objects.filter(is_active=True)
                .exclude(id__in=member_ids)
                .exclude(id__in=current_delegates)
                .order_by("last_name", "first_name")
            )
        else:
            self.fields["delegate"].queryset = User.objects.filter(
                is_active=True
            ).order_by("last_name", "first_name")
        self.fields["delegator"].help_text = "Only active members of this group."
        self.fields["delegate"].help_text = (
            "Only peers of the selected member (MP to MP, staff to staff), "
            "excluding this group's members and current delegates."
        )
        self.enable_pickers()

    def clean_end_date(self):
        end_date = self.cleaned_data["end_date"]
        if end_date < timezone.localdate():
            raise forms.ValidationError("Choose a date that has not already passed.")
        return end_date
