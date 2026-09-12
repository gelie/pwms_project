"""
Load the bundled GeoNames countries and cities into the reference tables.

The files live in ``src/pwms/data``; see the README there for provenance and the
CC BY 4.0 attribution that has to travel with the data. The command is safe to
re-run: countries are upserted on their ISO code and cities are matched on
``(country, name, latitude, longitude)``, so only rows that are missing are
inserted and existing rows keep their primary keys (reports point at them).

Usage:
    manage.py load_places
    manage.py load_places --data-dir=/tmp/geonames-csv
"""

import csv
import gzip
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pwms.models import City, Country

#: Where the bundled CSVs live unless ``--data-dir`` points somewhere else.
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

#: Rows inserted per ``bulk_create`` call while loading cities.
BATCH_SIZE = 1000


def open_csv(path):
    """Open a plain or gzipped CSV file for text reading."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return path.open(newline="", encoding="utf-8")


class Command(BaseCommand):
    help = "Load the bundled country/city reference data used by the location pickers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--data-dir",
            type=Path,
            default=DEFAULT_DATA_DIR,
            help=(
                "Directory holding countries.csv and cities.csv.gz "
                f"(default: {DEFAULT_DATA_DIR})."
            ),
        )

    def handle(self, *args, **options):
        data_dir = options["data_dir"]
        countries_file = data_dir / "countries.csv"
        cities_file = next(
            (
                path
                for path in (data_dir / "cities.csv.gz", data_dir / "cities.csv")
                if path.exists()
            ),
            None,
        )
        if not countries_file.exists() or cities_file is None:
            raise CommandError(
                f"Expected countries.csv and cities.csv[.gz] in {data_dir}"
            )

        with transaction.atomic():
            countries = self._load_countries(countries_file)
            created, skipped = self._load_cities(cities_file, countries)

        self.stdout.write(
            self.style.SUCCESS(
                f"{len(countries)} countries loaded; "
                f"cities: {created} created, {skipped} already present."
            )
        )

    def _load_countries(self, path):
        """Upsert the countries and return them keyed by ISO alpha-2 code."""
        with open_csv(path) as handle:
            for row in csv.DictReader(handle):
                Country.objects.update_or_create(
                    code=row["code"],
                    defaults={
                        "iso3": row["iso3"],
                        "name": row["name"],
                        "continent": row["continent"],
                    },
                )
        return {country.code: country for country in Country.objects.all()}

    def _load_cities(self, path, countries):
        """Insert the cities that are not in the table yet."""
        known = set(
            City.objects.values_list("country_id", "name", "latitude", "longitude")
        )
        batch = []
        created = skipped = 0

        with open_csv(path) as handle:
            for row in csv.DictReader(handle):
                country = countries.get(row["country_code"])
                latitude = Decimal(row["latitude"])
                longitude = Decimal(row["longitude"])
                key = (
                    country.pk if country else None,
                    row["name"],
                    latitude,
                    longitude,
                )
                if country is None or key in known:
                    skipped += 1
                    continue
                known.add(key)
                batch.append(
                    City(
                        country=country,
                        name=row["name"],
                        ascii_name=row["ascii_name"],
                        latitude=latitude,
                        longitude=longitude,
                        population=int(row["population"] or 0),
                    )
                )
                if len(batch) >= BATCH_SIZE:
                    created += len(City.objects.bulk_create(batch, BATCH_SIZE))
                    batch = []

        created += len(City.objects.bulk_create(batch, BATCH_SIZE))
        return created, skipped
