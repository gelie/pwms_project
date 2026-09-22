"""Give the IRPD workflow types the roles that may create their instances.

Migration 0005 (and 0016 for the agreement) seeded the three workflow types the
International Relations unit works with — *Delegation Report*, *International
Resolution* and *International Agreement* — but deliberately left ``create_roles``
empty, which closes creation to superusers alone::

    create_roles: "Roles from this type's group that may create workflow instances.
    Leave empty to let only superusers create instances."

That default is right for a fresh database (the roles and the group only arrive
with the Oracle organisational sync) but wrong for a live one, where the section
cannot capture the instruments it owns until an administrator fills the field in
by hand. This migration fills it in, so the three types are usable out of the box.

The roles seeded are:

* every role held by an **active member of group 98** (*IRP: MR: Man And Gen*) —
  which satisfies by construction the rule the ``WorkflowType`` admin enforces,
  that a type's ``create_roles`` come from the type's own group — and
* the roles named in :data:`DEFAULT_CREATE_ROLE_NAMES`, so a database whose
  memberships have not been synced yet still gets a usable policy rather than an
  empty one. Naming a role nobody holds is inert: it grants nothing until someone
  in the group holds it.

Two deliberate limits keep the write safe:

* a type that already has ``create_roles`` is left alone, so an administrator's
  configuration is never overwritten;
* only types owned by group 98 (or not yet owned by anyone, as on a database
  where migration 0012 ran before the group existed) are touched, so a type an
  administrator has since re-owned to another group is never given this group's
  roles.

Like migrations 0012 and 0016, the whole operation is skipped when group 98 does
not exist — a fresh or test database — and reverse removes exactly the roles it
added.
"""

from django.db import migrations
from django.db.models import Q

#: Group that owns the IRPD workflow types, by name and by its recorded pk.
RBAC_GROUP_NAME = "IRP: MR: Man And Gen"
RBAC_GROUP_ID = 98

#: The three types the section creates, works and reports on (migrations 0005, 0016).
IRPD_TYPE_NAMES = (
    "Delegation Report",
    "International Resolution",
    "International Agreement",
)

#: Roles that may create an IRPD instrument when the group's membership has not
#: been synced yet. Held roles are always seeded as well; this list only widens
#: the policy on a database where nobody holds a role in the group so far. Edit
#: freely — the roles that do not exist are ignored.
DEFAULT_CREATE_ROLE_NAMES = (
    "Staff Member",
    "Administrative Officer",
    "Control Officer",
    "Director",
    "Executive Assistant",
)


def _owning_group(Group):
    """Group 98, resolved by name first and by its recorded pk second."""
    group = Group.objects.filter(name__iexact=RBAC_GROUP_NAME).first()
    if group is None:
        group = Group.objects.filter(pk=RBAC_GROUP_ID).first()
    return group


def _creatable_types(WorkflowType, group):
    """The IRPD types this migration may write to (see the module docstring)."""
    return WorkflowType.objects.filter(
        Q(name__in=IRPD_TYPE_NAMES),
        Q(group=group) | Q(group__isnull=True),
    )


def _creating_roles(apps, group):
    """Roles the IRPD types should allow to create instances.

    The roles active members of ``group`` hold — the invariant the type's admin
    enforces — unioned with the documented defaults. Filtered in one query so a
    role held by several members is returned once.
    """
    Role = apps.get_model("pwms", "Role")
    return Role.objects.filter(
        Q(groupmembership__group=group, groupmembership__is_active=True)
        | Q(name__in=DEFAULT_CREATE_ROLE_NAMES)
    ).distinct()


def seed_create_roles(apps, schema_editor):
    """Seed each IRPD type's creating roles, unless it already has some."""
    Group = apps.get_model("pwms", "Group")
    WorkflowType = apps.get_model("pwms", "WorkflowType")

    group = _owning_group(Group)
    if group is None:
        return

    roles = list(_creating_roles(apps, group))
    if not roles:
        return

    for workflow_type in _creatable_types(WorkflowType, group):
        if workflow_type.create_roles.exists():
            continue
        workflow_type.create_roles.set(roles)


def unseed_create_roles(apps, schema_editor):
    """Reverse: take back the roles seeded here, and nothing else."""
    Group = apps.get_model("pwms", "Group")
    WorkflowType = apps.get_model("pwms", "WorkflowType")

    group = _owning_group(Group)
    if group is None:
        return

    for workflow_type in _creatable_types(WorkflowType, group):
        workflow_type.create_roles.remove(*_creating_roles(apps, group))


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0045_withdraw_referral_transition_grant"),
    ]

    operations = [
        migrations.RunPython(seed_create_roles, unseed_create_roles),
    ]
