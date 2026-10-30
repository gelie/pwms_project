"""Issue a new secret for a registered external API client and print it once."""

from django.core.management.base import BaseCommand, CommandError

from pwms.api.models import ApiClient


class Command(BaseCommand):
    help = (
        "Rotate an external API client's secret. The new value is printed once "
        "and is not recoverable from the database."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "client_id",
            help="Public client id shown in the admin (e.g. 'parking-system').",
        )

    def handle(self, *args, **options):
        client_id = options["client_id"]
        try:
            client = ApiClient.objects.get(client_id=client_id)
        except ApiClient.DoesNotExist as exc:
            raise CommandError(f"No API client with client_id={client_id!r}.") from exc

        raw_secret = client.rotate_secret()
        self.stdout.write(
            self.style.SUCCESS(f"Client:       {client.name} ({client.client_id})")
        )
        self.stdout.write(self.style.WARNING(f"Client secret: {raw_secret}"))
        self.stdout.write(
            "Copy it now: only a hash is stored, so it cannot be shown again."
        )
