"""
Seed the executive branch: ``Government of RSA`` → Cabinet ministries, plus the
``Minister`` / ``Deputy Minister`` roles.

The ministry names are snapshotted from
``workflow_requirements/Names of Cabinet Ministries (and responsibility areas).csv``
(the file lives outside the ``pwms`` package and is not shipped with it, so the
list is embedded here rather than read at migration time). The CSV is a flat
single-column list, so every row is a ministry — including *Agriculture*, which
``import_groups`` would swallow as a section heading.

Creates, idempotently and matched by natural key:

* the ``executive`` root group ``Government of RSA``;
* one ``ministry`` child per Cabinet ministry, except *The Presidency*, which is
  seeded as ``presidency`` because it is not a ministry;
* the roles ``Minister`` and ``Deputy Minister``, with no workflow capability
  flags: the office identifies its holder, while what they may *do* is granted
  by the RBAC layer (``WorkflowGroupAccess``, ``WorkflowRolePermission``,
  ``WorkflowStatePermission``), exactly as for ``Member of Parliament``.

Appointments themselves are not seeded: a Minister is a ``GroupMembership``
linking their ``User`` to a ministry group with the ``Minister`` role and
``start_date`` / ``end_date`` (see the ``User`` model for the ``identity_source``
flag that keeps locally appointed office holders out of the Oracle sync).

Reverse removes the groups seeded here — and, by cascade, any memberships
attached to them — and removes the roles only while nothing references them.
"""

from django.db import migrations

#: Root of the executive branch.
GOVERNMENT_ROOT_NAME = "Government of RSA"
GOVERNMENT_ROOT_TYPE = "executive"

#: The Presidency is executive but not a ministry.
PRESIDENCY_NAME = "The Presidency"
PRESIDENCY_TYPE = "presidency"

#: Cabinet ministries, in the order given by the source CSV.
CABINET_MINISTRIES = (
    "Agriculture",
    "Basic Education",
    "Communications and Digital Technologies",
    "Cooperative Governance and Traditional Affairs",
    "Correctional Services",
    "Defence and Military Veterans",
    "Employment and Labour",
    "Electricity and Energy",
    "Finance",
    "Forestry, Fisheries and the Environment",
    "Health",
    "Higher Education",
    "Home Affairs",
    "Human Settlements",
    "International Relations and Cooperation",
    "Justice and Constitutional Development",
    "Land Reform and Rural Development",
    "Mineral and Petroleum Resources",
    "Planning, Monitoring and Evaluation",
    "Police",
    "Public Service and Administration",
    "Public Works and Infrastructure",
    "Science, Technology and Innovation",
    "Small Business Development",
    "Social Development",
    "Sport, Arts and Culture",
    "Tourism",
    "Trade, Industry and Competition",
    "Transport",
    PRESIDENCY_NAME,
    "Women, Youth and Persons with Disabilities",
    "Water and Sanitation",
)

#: Executive office roles, with the descriptions used by the role sync.
EXECUTIVE_ROLES = (
    ("Minister", "Member of the Executive responsible for a national portfolio"),
    ("Deputy Minister", "Member of the Executive deputising for a Minister"),
)


def _models():
    """Return the concrete models this migration seeds.

    ``apps.get_model`` renders ``Group`` as a plain model: ``MPTTModel`` is
    abstract, so the rendered class loses the tree behaviour and would insert
    NULL ``lft`` / ``rght``. Seed data therefore uses the concrete models — the
    usual workaround for MPTT trees in data migrations — which only requires
    that the fields set here (name, group_type, parent, is_active) stay
    compatible with the model.
    """
    from pwms.models import Group, Role

    return Group, Role


def seed_executive_branch(apps, schema_editor):
    """Create the executive groups and roles that are missing."""
    Group, Role = _models()

    root, created = Group.objects.get_or_create(
        name=GOVERNMENT_ROOT_NAME,
        parent=None,
        defaults={"group_type": GOVERNMENT_ROOT_TYPE, "is_active": True},
    )
    if not created and root.group_type != GOVERNMENT_ROOT_TYPE:
        root.group_type = GOVERNMENT_ROOT_TYPE
        root.save(update_fields=["group_type"])

    for name in CABINET_MINISTRIES:
        group_type = PRESIDENCY_TYPE if name == PRESIDENCY_NAME else "ministry"
        group, created = Group.objects.get_or_create(
            name=name,
            parent=root,
            defaults={"group_type": group_type, "is_active": True},
        )
        if not created and group.group_type != group_type:
            group.group_type = group_type
            group.save(update_fields=["group_type"])

    for name, description in EXECUTIVE_ROLES:
        Role.objects.get_or_create(name=name, defaults={"description": description})


def unseed_executive_branch(apps, schema_editor):
    """Reverse: drop the seeded groups (and their memberships) and spare roles."""
    Group, Role = _models()

    root = Group.objects.filter(name=GOVERNMENT_ROOT_NAME, parent__isnull=True).first()
    if root is not None:
        for group in Group.objects.filter(parent=root, name__in=CABINET_MINISTRIES):
            group.delete()
        if not Group.objects.filter(parent=root).exists():
            root.delete()

    for name, _description in EXECUTIVE_ROLES:
        role = Role.objects.filter(name=name).first()
        if role is not None and not role.groupmembership_set.exists():
            role.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0022_user_identity_source"),
    ]

    operations = [
        migrations.RunPython(seed_executive_branch, unseed_executive_branch),
    ]
