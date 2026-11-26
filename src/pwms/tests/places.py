import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from ..models import (
    City,
    Country,
)


class PlaceDataTests(TestCase):
    """The bundled country/city reference data and the fragments it feeds."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="places", password="pw"
        )
        cls.south_africa = Country.objects.create(
            code="ZA", iso3="ZAF", name="South Africa", continent="AF"
        )
        cls.switzerland = Country.objects.create(
            code="CH", iso3="CHE", name="Switzerland", continent="EU"
        )
        # Contains "south" without starting with it, so the ranking rule has
        # something to place below South Africa.
        cls.french_southern = Country.objects.create(
            code="TF",
            iso3="ATF",
            name="French Southern Territories",
            continent="AN",
        )
        cls.cape_town = cls._city(cls.south_africa, "Cape Town", 4772846)
        cls.geneva = cls._city(cls.switzerland, "Geneva", 201818)
        cls.zurich = cls._city(cls.switzerland, "Zurich", 341730)

    @staticmethod
    def _city(country, name, population):
        return City.objects.create(
            country=country,
            name=name,
            ascii_name=name,
            latitude="1.5",
            longitude="2.5",
            population=population,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_country_search_fragment_matches_name_and_code(self):
        response = self.client.get(reverse("pwms:country_search"), {"search": "swit"})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pwms/partials/country_search_results.html")
        self.assertContains(response, "Switzerland")
        self.assertContains(response, f"selectOption('{self.switzerland.pk}'")

    def test_country_search_ranks_prefix_matches_first(self):
        response = self.client.get(reverse("pwms:country_search"), {"search": "south"})

        names = [country.name for country in response.context["countries"]]
        self.assertEqual(names[0], "South Africa")
        self.assertIn("French Southern Territories", names)

    def test_city_search_is_scoped_to_the_country_it_is_sent(self):
        response = self.client.get(
            reverse("pwms:city_search"),
            {"country": self.switzerland.pk, "search": "z"},
        )

        self.assertContains(response, "Zurich")
        self.assertNotContains(response, "Cape Town")

    def test_city_search_ranks_biggest_places_first(self):
        response = self.client.get(
            reverse("pwms:city_search"), {"country": self.switzerland.pk}
        )

        names = [city.name for city in response.context["cities"]]
        self.assertEqual(names, ["Zurich", "Geneva"])

    def test_city_search_ignores_an_unusable_country_value(self):
        response = self.client.get(
            reverse("pwms:city_search"), {"country": "not-an-id", "search": "cap"}
        )

        self.assertContains(response, "Cape Town")

    def test_place_loader_reads_the_bundled_layout_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "countries.csv").write_text(
                "code,iso3,name,continent\nZZ,ZZZ,Zedland,AF\n", encoding="utf-8"
            )
            (data_dir / "cities.csv").write_text(
                "name,ascii_name,country_code,latitude,longitude,population\n"
                "Testville,Testville,ZZ,1.5,2.5,42\n",
                encoding="utf-8",
            )
            first = StringIO()
            call_command("load_places", data_dir=data_dir, stdout=first)
            second = StringIO()
            call_command("load_places", data_dir=data_dir, stdout=second)

        self.assertIn("cities: 1 created, 0 already present", first.getvalue())
        self.assertIn("cities: 0 created, 1 already present", second.getvalue())
        self.assertEqual(Country.objects.filter(code="ZZ").count(), 1)
        self.assertEqual(City.objects.get(name="Testville").country.code, "ZZ")
