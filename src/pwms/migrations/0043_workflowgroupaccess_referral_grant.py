"""Let a referred group's grant record that a referral produced it.

A referral now carries the referred group's access to the record: while the
referral is open the group may view, edit and move the record on, and once it
closes the group keeps view so it can still see what it was asked about. That
grant lives in ``WorkflowGroupAccess`` beside the organisational ones, so these
columns record where it came from and which capabilities a referral actually
raised.

``via_referral`` marks a row the referral system created rather than the type's
materialisation or an administrator. ``referral_raised_edit`` /
``referral_raised_transition`` record a capability a referral had to *raise* on a
row that already existed (a viewer group's, say), which is therefore the
referral's to hand back when it closes — a capability the group already held is
never marked, so closing a referral cannot narrow an organic grant.

Schema only: nothing is granted by this migration, so existing rows are left
exactly as they are. Migration ``0044`` is the one that backfills access for
referrals that predate the feature.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0042_widen_workflow_document_urls"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowgroupaccess",
            name="referral_raised_edit",
            field=models.BooleanField(
                default=False,
                help_text="A referral raised can_edit; withdraw it once the referrals close.",
            ),
        ),
        migrations.AddField(
            model_name="workflowgroupaccess",
            name="referral_raised_transition",
            field=models.BooleanField(
                default=False,
                help_text="A referral raised can_transition; withdraw it once the referrals close.",
            ),
        ),
        migrations.AddField(
            model_name="workflowgroupaccess",
            name="via_referral",
            field=models.BooleanField(
                default=False,
                help_text="Created by a referral, rather than by materialisation or an administrator.",
            ),
        ),
    ]
