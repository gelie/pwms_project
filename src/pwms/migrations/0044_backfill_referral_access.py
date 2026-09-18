"""Give groups referred under the old rules the access those referrals imply.

A referral now grants the referred group view + edit + transition on the record
while the referral is open, and leaves it readable afterwards (migration ``0043``
added the columns that track this). Referrals raised before the feature are rows
without a grant, so a group that was asked to consider a record could not even
open the page it is supposed to answer from — ``_can_answer_referral`` lets it
answer, but the detail view still refuses it.

This backfill replays that rule over the referrals already in the database:
grouped by (instance, referred group), each group is owed an access row, created
here with view and — where a referral is still open — edit and transition. It only
ever *adds* access; an existing row is widened by at most the referral's own two
capabilities and never narrowed, so a policy an administrator has set stands.

The reverse withdraws what this migration raised and removes the rows it created.
A grant the running application makes later has the same shape, and a data
migration cannot tell the two apart, so a rollback may also take those back —
narrow or recreate them by hand if that matters.
"""

from django.db import migrations

#: Fields that make a grant more than "may read this record".
BEYOND_VIEW_FIELDS = (
    "can_edit",
    "can_delete",
    "can_share",
    "can_comment",
    "can_manage",
    "can_transition",
)


def _referral_targets(WorkflowReferral):
    """
    Map each (instance, referred group) to whether a referral to it is still open.

    Keyed by the tuple an access row is keyed by, so the backfill and its reverse
    walk exactly the same set.
    """
    targets = {}
    rows = WorkflowReferral.objects.values_list(
        "content_type_id", "object_id", "referred_to_id", "status"
    )
    for content_type_id, object_id, group_id, status in rows:
        key = (content_type_id, object_id, group_id)
        targets[key] = targets.get(key, False) or status == "open"
    return targets


def grant_referral_access(apps, schema_editor):
    """Create or widen each referred group's access row from its referrals."""
    WorkflowReferral = apps.get_model("pwms", "WorkflowReferral")
    WorkflowGroupAccess = apps.get_model("pwms", "WorkflowGroupAccess")

    for (content_type_id, object_id, group_id), is_open in _referral_targets(
        WorkflowReferral
    ).items():
        access = WorkflowGroupAccess.objects.filter(
            content_type_id=content_type_id, object_id=object_id, group_id=group_id
        ).first()
        if access is None:
            WorkflowGroupAccess.objects.create(
                content_type_id=content_type_id,
                object_id=object_id,
                group_id=group_id,
                can_view=True,
                can_edit=is_open,
                can_transition=is_open,
                referral_raised_edit=is_open,
                referral_raised_transition=is_open,
                via_referral=True,
            )
            continue

        # An existing row keeps its own policy; the referral only raises the two
        # capabilities it needs, and only where they are off.
        updates = []
        if not access.can_view:
            access.can_view = True
            updates.append("can_view")
        if is_open and not access.can_edit:
            access.can_edit = True
            access.referral_raised_edit = True
            updates += ["can_edit", "referral_raised_edit"]
        if is_open and not access.can_transition:
            access.can_transition = True
            access.referral_raised_transition = True
            updates += ["can_transition", "referral_raised_transition"]
        if updates:
            access.save(update_fields=updates)


def withdraw_referral_access(apps, schema_editor):
    """Withdraw what the backfill raised and drop the rows it created."""
    WorkflowReferral = apps.get_model("pwms", "WorkflowReferral")
    WorkflowGroupAccess = apps.get_model("pwms", "WorkflowGroupAccess")

    for content_type_id, object_id, group_id in _referral_targets(WorkflowReferral):
        access = WorkflowGroupAccess.objects.filter(
            content_type_id=content_type_id, object_id=object_id, group_id=group_id
        ).first()
        if access is None:
            continue

        updates = []
        if access.referral_raised_edit:
            access.can_edit = False
            access.referral_raised_edit = False
            updates += ["can_edit", "referral_raised_edit"]
        if access.referral_raised_transition:
            access.can_transition = False
            access.referral_raised_transition = False
            updates += ["can_transition", "referral_raised_transition"]
        if updates:
            access.save(update_fields=updates)

        # What the backfill created is now a view-only referral grant with nothing
        # else on it, so it is removed rather than left behind granting view.
        if (
            access.via_referral
            and not access.is_primary
            and not any(getattr(access, field) for field in BEYOND_VIEW_FIELDS)
        ):
            access.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0043_workflowgroupaccess_referral_grant"),
    ]

    operations = [
        migrations.RunPython(grant_referral_access, withdraw_referral_access),
    ]
