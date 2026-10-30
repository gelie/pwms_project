"""Seed the three initial partner registrations.

Secrets are intentionally *not* stored here: a client secret is generated when
an administrator rotates it (admin action or ``manage.py rotate_api_secret
<client_id>``) and is shown once. The rows below are therefore ready for
onboarding but cannot authenticate until a secret has been issued.
"""

from django.db import migrations

SEED_CLIENTS = [
    {
        "name": "Parking System",
        "client_id": "parking-system",
        "contact_email": "zfo@parliament.gov.za",
        "description": (
            "Parliamentary parking system. Validates permit holders against "
            "the PWMS user register and their group memberships."
        ),
        "scopes": ["users:read", "memberships:read"],
        "rate_limit_per_minute": 60,
    },
    {
        "name": "Main Website",
        "client_id": "main-website",
        "contact_email": "jarendz@parliament.gov.za",
        "description": (
            "Parliament's public website. Publishes the bill register and each "
            "bill's public status."
        ),
        "scopes": ["bills:read"],
        "rate_limit_per_minute": 60,
    },
    {
        "name": "eNominations",
        "client_id": "enominations",
        "contact_email": "sblowu@parliament.gov.za",
        "description": (
            "eNominations system. Reads group memberships to determine who may "
            "be nominated for a House or committee position."
        ),
        "scopes": ["memberships:read"],
        "rate_limit_per_minute": 60,
    },
]


def seed_clients(apps, schema_editor):
    ApiClient = apps.get_model("api", "ApiClient")
    for record in SEED_CLIENTS:
        client_id = record["client_id"]
        defaults = {key: value for key, value in record.items() if key != "client_id"}
        ApiClient.objects.update_or_create(client_id=client_id, defaults=defaults)


def remove_clients(apps, schema_editor):
    ApiClient = apps.get_model("api", "ApiClient")
    ApiClient.objects.filter(
        client_id__in=[record["client_id"] for record in SEED_CLIENTS]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_clients, reverse_code=remove_clients),
    ]
