"""Give the House's Members and Parties containers their own group types.

``NA Members``/``NCOP Members`` and ``NA Parties``/``NCOP Parties`` were first
added with the types ``MembershipSyncService`` used to create them on demand —
``administration`` and ``party`` — which made a container indistinguishable from
the groups it holds. ``members`` and ``parties`` now say what they are.

Existing rows are remapped only when they still carry the interim type, so a
database that already uses the new types is left alone, and actual party groups
(``party``) under a Parties container are never touched.
"""

from django.db import migrations, models

MEMBERS_CONTAINERS = ["NA Members", "NCOP Members"]
PARTIES_CONTAINERS = ["NA Parties", "NCOP Parties"]

GROUP_TYPE_CHOICES = [
    ("parliament", "Parliament (term)"),
    ("house", "House"),
    ("delegation", "Provincial delegation"),
    ("committees", "Committees (container)"),
    ("committee", "Committee"),
    ("members", "Members (container)"),
    ("parties", "Parties (container)"),
    ("administration", "Administration"),
    ("branch", "Branch"),
    ("division", "Division"),
    ("office", "Office"),
    ("section", "Section"),
    ("unit", "Unit"),
    ("party", "Party"),
    ("executive", "Executive"),
    ("presidency", "Presidency"),
    ("ministry", "Ministry"),
    ("municipality", "Municipality"),
    ("department", "Department"),
    ("province", "Province"),
]


def remap_container_types(apps, schema_editor):
    Group = apps.get_model("pwms", "Group")
    house = {"parent__group_type": "house"}
    Group.objects.filter(
        name__in=MEMBERS_CONTAINERS, group_type="administration", **house
    ).update(group_type="members")
    Group.objects.filter(
        name__in=PARTIES_CONTAINERS, group_type="party", **house
    ).update(group_type="parties")


def unmap_container_types(apps, schema_editor):
    Group = apps.get_model("pwms", "Group")
    house = {"parent__group_type": "house"}
    Group.objects.filter(
        name__in=MEMBERS_CONTAINERS, group_type="members", **house
    ).update(group_type="administration")
    Group.objects.filter(
        name__in=PARTIES_CONTAINERS, group_type="parties", **house
    ).update(group_type="party")


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0061_restore_group_base_model_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="group",
            name="group_type",
            field=models.CharField(choices=GROUP_TYPE_CHOICES, max_length=20),
        ),
        migrations.RunPython(remap_container_types, unmap_container_types),
    ]
