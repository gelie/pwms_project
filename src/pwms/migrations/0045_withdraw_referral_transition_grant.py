"""A referral confers view and edit, not transition.

Migrations ``0043`` / ``0044`` had a referral lend the referred group ``transition``
as well, so a committee could move on a record it was asked to consider. That is
too much: ``transition`` is all-or-nothing. ``get_available_transitions()`` offers
every transition out of the current state and ``perform_transition()`` checks the
state machine, the guards and a comment — never the actor — so the capability
would let a referred group close or even withdraw a record it was only asked to
advise on (for a Bill referred to committee, the state even offers *"Withdraw bill
(referred to committee)"*). Moving a record on is the owning group's job; a
referred group reads the record and contributes the answer.

So the referral grant narrows to view + edit. This migration withdraws the
transition those two migrations handed out: a row carrying
``referral_raised_transition`` is one the referral raised that capability on (the
only way it was ever granted), so clearing it restores exactly the state the group
held before the referral — an organic transition right was never marked and is
left alone. The column then goes, ``sync_referral_access`` no longer carrying
transition at all.

The reverse re-adds the column but cannot repopulate it: which rows a referral
raised transition on is not recoverable once the flag is cleared, so rolling back
leaves every row without the capability. Re-grant it by hand if a rollback needs
the old behaviour.
"""

from django.db import migrations


def withdraw_referral_transition(apps, schema_editor):
    """Take back the transition right a referral handed the referred group."""
    WorkflowGroupAccess = apps.get_model("pwms", "WorkflowGroupAccess")
    WorkflowGroupAccess.objects.filter(referral_raised_transition=True).update(
        can_transition=False, referral_raised_transition=False
    )


def restore_referral_transition(apps, schema_editor):
    """No-op: the flags this would need are gone by the time it reverses."""


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0044_backfill_referral_access"),
    ]

    operations = [
        migrations.RunPython(withdraw_referral_transition, restore_referral_transition),
        migrations.RemoveField(
            model_name="workflowgroupaccess",
            name="referral_raised_transition",
        ),
    ]
