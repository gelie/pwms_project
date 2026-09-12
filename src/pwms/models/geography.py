"""Countries and cities behind the location pickers.

This is reference data: it is loaded from the bundled GeoNames extract with
``manage.py load_places`` and never created by the app itself, which is why
nothing here has workflow or audit behaviour.
"""

from django.db import models

from .base import BaseModel


class Country(BaseModel):
    """A country, keyed by its ISO 3166-1 alpha-2 code."""

    code = models.CharField(
        max_length=2,
        unique=True,
        help_text="ISO 3166-1 alpha-2 code, e.g. ZA.",
    )
    iso3 = models.CharField(max_length=3, blank=True, help_text="ISO 3166-1 alpha-3.")
    name = models.CharField(max_length=100, db_index=True)
    continent = models.CharField(max_length=2, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "countries"

    def __str__(self):
        return self.name


class City(BaseModel):
    """A populated place in a country."""

    country = models.ForeignKey(
        Country,
        on_delete=models.CASCADE,
        related_name="cities",
    )
    name = models.CharField(max_length=200)
    ascii_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Diacritic-free spelling, searched alongside the name.",
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    population = models.PositiveIntegerField(
        default=0,
        help_text="Used to rank search results; 0 when the source has no figure.",
    )

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "cities"
        indexes = [
            models.Index(fields=["country", "name"]),
            models.Index(fields=["country", "ascii_name"]),
        ]

    def __str__(self):
        return self.name
