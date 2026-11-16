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

The rows are written through the **historical** models from ``apps.get_model``,
never the live ``pwms.models`` classes: a data migration must only touch columns
that exist at this point in history, and the live ``Group`` has since gained
``external_id`` / ``prefix`` / ``term``. ``apps.get_model`` renders ``Group`` as
a plain model, though, so it does not maintain the ``lft`` / ``rght`` /
``tree_id`` / ``level`` columns — which are NOT NULL — hence the explicit
``_rebuild_tree`` pass that reproduces ``Group.objects.rebuild()``.

Reverse removes the groups seeded here — and, by cascade, any memberships
attached to them — and removes the roles only while nothing references them.
"""

from collections import defaultdict

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
    "The Presidency",
    "Women, Youth and Persons with Disabilities",
    "Water and Sanitation",
)

#: Executive office roles, with the descriptions used by the role sync.
EXECUTIVE_ROLES = (
    ("Minister", "Member of the Executive responsible for a national portfolio"),
    ("Deputy Minister", "Member of the Executive deputising for a Minister"),
)


def _rebuild_tree(Group):
    """Recompute the MPTT columns the historical model does not maintain.

    ``apps.get_model`` gives a plain model, so ``save()`` does not populate
    ``lft`` / ``rght`` / ``tree_id`` / ``level`` and those columns are NOT NULL.
    Number the forest depth-first, ordered by name to match the model's
    ``MPTTMeta.order_insertion_by``, and write the values with ``update()`` so
    no ``save()`` hook runs.
    """
    children = defaultdict(list)
    for node in Group.objects.all().order_by("name"):
        children[node.parent_id].append(node)

    next_lft = 1

    def number(node, *, level, tree_id):
        nonlocal next_lft
        left = next_lft
        next_lft += 1
        for child in children[node.pk]:
            number(child, level=level + 1, tree_id=tree_id)
        Group.objects.filter(pk=node.pk).update(
            lft=left, rght=next_lft, tree_id=tree_id, level=level
        )
        next_lft += 1

    for tree_id, root in enumerate(children[None], start=1):
        number(root, level=0, tree_id=tree_id)


def seed_executive_branch(apps, schema_editor):
    """Create the executive groups and roles that are missing."""
    Group = apps.get_model("pwms", "Group")
    Role = apps.get_model("pwms", "Role")

    # Temporary MPTT values so the NOT NULL columns are satisfied; the rebuild
    # below replaces them with the real numbering.
    mptt_defaults = {"lft": 0, "rght": 0, "tree_id": 0, "level": 0}

    root, created = Group.objects.get_or_create(
        name=GOVERNMENT_ROOT_NAME,
        parent=None,
        defaults={
            "group_type": GOVERNMENT_ROOT_TYPE,
            "is_active": True,
            **mptt_defaults,
        },
    )
    if not created and root.group_type != GOVERNMENT_ROOT_TYPE:
        root.group_type = GOVERNMENT_ROOT_TYPE
        root.save(update_fields=["group_type"])

    for name in CABINET_MINISTRIES:
        group_type = PRESIDENCY_TYPE if name == PRESIDENCY_NAME else "ministry"
        group, created = Group.objects.get_or_create(
            name=name,
            parent=root,
            defaults={
                "group_type": group_type,
                "is_active": True,
                **mptt_defaults,
            },
        )
        if not created and group.group_type != group_type:
            group.group_type = group_type
            group.save(update_fields=["group_type"])

    for name, description in EXECUTIVE_ROLES:
        Role.objects.get_or_create(name=name, defaults={"description": description})

    _rebuild_tree(Group)


def unseed_executive_branch(apps, schema_editor):
    """Reverse: drop the seeded groups (and their memberships) and spare roles."""
    Group = apps.get_model("pwms", "Group")
    Role = apps.get_model("pwms", "Role")

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

    _rebuild_tree(Group)


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0022_user_identity_source"),
    ]

    operations = [
        migrations.RunPython(seed_executive_branch, unseed_executive_branch),
    ]
