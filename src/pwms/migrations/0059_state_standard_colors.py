"""Give every workflow state the one standard colour for its role.

Each seed migration chose its own colours, so the same role looked different in
every workflow (and the initial state was a yellow too pale for white badge
text). This sets ``State.color`` from the state's flags and name, across all
workflow types, to the scheme in ``pwms.state_colors``:

    initial       slate   #5b6b7f
    in progress   green   #3c7f52
    terminal      pine    #14532d
    unsuccessful  maroon  #8c2f2f   (Withdrawn, Lapsed, Cancelled)

The palette is repeated here on purpose: a migration must keep doing what it did
when it was written, even if ``pwms.state_colors`` is later retuned.

Colours that were set by hand are overwritten; that is the point of making them
uniform. The reverse is a no-op, as the old values are not kept.
"""

import re

from django.db import migrations, models

INITIAL = "#5b6b7f"
IN_PROGRESS = "#3c7f52"
TERMINAL = "#14532d"
UNSUCCESSFUL = "#8c2f2f"

UNSUCCESSFUL_NAME = re.compile(r"\b(withdrawn|lapsed|cancell?ed)\b", re.IGNORECASE)


def standard_color(state):
    if UNSUCCESSFUL_NAME.search(state.name or ""):
        return UNSUCCESSFUL
    if state.is_initial:
        return INITIAL
    if state.is_terminal:
        return TERMINAL
    return IN_PROGRESS


def apply_standard_colors(apps, schema_editor):
    State = apps.get_model("pwms", "State")
    for state in State.objects.all():
        color = standard_color(state)
        if state.color != color:
            State.objects.filter(pk=state.pk).update(color=color)


class Migration(migrations.Migration):

    dependencies = [
        ("pwms", "0058_alter_bill_atc_reference_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="state",
            name="color",
            field=models.CharField(
                default="#3c7f52",
                help_text=(
                    "Hex color code. Use the standard colour for the state's "
                    "role (see the default_color property) so every workflow "
                    "looks alike."
                ),
                max_length=7,
            ),
        ),
        migrations.RunPython(apply_standard_colors, migrations.RunPython.noop),
    ]
