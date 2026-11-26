"""Forms for sharing reports and workflow records."""

from django import forms
from django.core.validators import validate_email

from ..models.reports import SCHEDULE_CHOICES
from ..reporting.exports import ATTACHMENT_CHOICES


class ReportShareForm(forms.Form):
    """Details for minting a report share: who to tell, how long, and how often.

    A share always produces a link; emailing it, attaching a rendered copy and
    repeating it are optional extras. Recipients are validated one by one so a
    typo in a list is named rather than swallowed.
    """

    title = forms.CharField(
        required=False,
        max_length=255,
        label="Share name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "e.g. Q3 oversight report",
            }
        ),
    )
    message = forms.CharField(
        required=False,
        label="Message",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Optional note to include with the link",
            }
        ),
    )
    expires_days = forms.ChoiceField(
        required=False,
        label="Link expires",
        choices=(
            ("", "Never"),
            ("7", "In 7 days"),
            ("30", "In 30 days"),
            ("90", "In 90 days"),
            ("365", "In a year"),
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    schedule = forms.ChoiceField(
        required=False,
        label="Repeat",
        choices=SCHEDULE_CHOICES,
        initial="none",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    send_email = forms.BooleanField(
        required=False,
        label="Email the link",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    recipients = forms.CharField(
        required=False,
        label="Recipients",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "name@parliament.gov.za, other@example.org",
            }
        ),
    )
    attach_format = forms.ChoiceField(
        required=False,
        label="Attach a copy",
        choices=(("", "Do not attach"),) + ATTACHMENT_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean_recipients(self):
        raw = self.cleaned_data.get("recipients", "")
        addresses = [
            address.strip()
            for address in raw.replace(";", ",").split(",")
            if address.strip()
        ]
        for address in addresses:
            validate_email(address)
        return ", ".join(addresses)

    def clean(self):
        data = super().clean()
        scheduled = data.get("schedule") not in (None, "", "none")
        # A repeating share has to be emailed *somewhere*, so a schedule implies
        # the address list even when "Email the link" was left clear.
        if (data.get("send_email") or scheduled) and not data.get("recipients"):
            self.add_error(
                "recipients",
                "Add at least one address to email the report to.",
            )
        return data


class WorkflowShareForm(forms.Form):
    """Email a workflow record's link to one or more people.

    ``recipients`` takes any mix of internal and external addresses, separated by
    commas, semicolons, spaces or new lines. The share modal's picker writes
    internal users' addresses into the same field, so the server only ever sees
    one list. Every address is validated, de-duplicated (case-insensitively) and
    named in the error if it is bad, rather than silently dropped.
    """

    #: Upper bound on one send, so the modal cannot be used as a mail cannon.
    MAX_RECIPIENTS = 25

    recipients = forms.CharField(
        label="Recipients",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 2,
                "placeholder": "name@parliament.gov.za, other@example.org",
            }
        ),
    )
    message = forms.CharField(
        required=False,
        label="Message",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Optional note to include with the link",
            }
        ),
    )
    attach_format = forms.ChoiceField(
        required=False,
        label="Attach a copy",
        choices=(
            ("", "Link only"),
            ("pdf", "PDF document (.pdf)"),
            ("html", "Web page (.html)"),
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean_recipients(self):
        raw = self.cleaned_data.get("recipients", "")
        tokens = [
            token.strip()
            for token in raw.replace(";", ",")
            .replace("\n", ",")
            .replace(" ", ",")
            .split(",")
            if token.strip()
        ]
        addresses = []
        seen = set()
        for token in tokens:
            try:
                validate_email(token)
            except forms.ValidationError:
                raise forms.ValidationError(
                    "“%(address)s” is not a valid email address.",
                    params={"address": token},
                ) from None
            if token.lower() not in seen:
                seen.add(token.lower())
                addresses.append(token)
        if not addresses:
            raise forms.ValidationError("Add at least one email address.")
        if len(addresses) > self.MAX_RECIPIENTS:
            raise forms.ValidationError(
                f"Share with at most {self.MAX_RECIPIENTS} addresses at a time."
            )
        return addresses
