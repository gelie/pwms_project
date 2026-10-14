"""Seed the on-premises venue catalogue (buildings and their rooms).

Reference data so a meeting's venue can be *selected* on a fresh database
rather than typed. Matched by natural key and idempotent, so re-running is
non-destructive and an administrator's edits are left alone. The list is a
representative subset of the parliamentary precinct; it is curated in the admin
afterwards.
"""

from django.db import migrations

#: (building name, short name, address)
BUILDINGS = [
    ("National Assembly Building", "NA", "Parliament Street, Cape Town"),
    ("National Council of Provinces Building", "NCOP", "Parliament Street, Cape Town"),
    ("Marks Building", "Marks", "90 Plein Street, Cape Town"),
    ("Good Hope Building", "Good Hope", "Parliament Street, Cape Town"),
]

#: (building name, room name, floor, capacity)
VENUES = [
    ("National Assembly Building", "National Assembly Chamber", "Ground", 400),
    ("National Council of Provinces Building", "NCOP Chamber", "Ground", 90),
    ("Marks Building", "Committee Room M314", "3rd", 20),
    ("Marks Building", "Committee Room M46", "4th", 30),
    ("Good Hope Building", "Committee Room 1", "1st", 40),
    ("Good Hope Building", "Committee Room 2", "1st", 40),
]


def seed_premises(apps, schema_editor):
    Building = apps.get_model("pwms", "Building")
    Venue = apps.get_model("pwms", "Venue")

    buildings = {}
    for name, short_name, address in BUILDINGS:
        building, _ = Building.objects.get_or_create(
            name=name,
            defaults={"short_name": short_name, "address": address, "is_active": True},
        )
        buildings[name] = building

    for building_name, room, floor, capacity in VENUES:
        building = buildings.get(building_name)
        if building is None:
            continue
        Venue.objects.get_or_create(
            building=building,
            name=room,
            defaults={"floor": floor, "capacity": capacity, "is_active": True},
        )


def unseed_premises(apps, schema_editor):
    Building = apps.get_model("pwms", "Building")
    Venue = apps.get_model("pwms", "Venue")

    building_names = [name for name, _short, _address in BUILDINGS]
    room_names = [room for _building, room, _floor, _capacity in VENUES]
    Venue.objects.filter(
        name__in=room_names, building__name__in=building_names
    ).delete()
    Building.objects.filter(name__in=building_names).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("pwms", "0053_meeting_venues"),
    ]

    operations = [
        migrations.RunPython(seed_premises, unseed_premises),
    ]
