"""Admin registrations for reference geography and venues."""

from django.contrib import admin

from ..models import Building, City, Country, Venue


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    """Admin for the country list behind the location pickers."""

    list_display = ("name", "code", "iso3", "continent")
    list_filter = ("continent",)
    search_fields = ("name", "code", "iso3")
    ordering = ("name",)


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    """Admin for the city list; reference data, curated by ``load_places``."""

    list_display = ("name", "country", "population")
    list_filter = ("country",)
    search_fields = ("name", "ascii_name")
    autocomplete_fields = ("country",)
    ordering = ("country", "name")


@admin.register(Building)
class BuildingAdmin(admin.ModelAdmin):
    """Admin for parliamentary buildings (the on-premises venue catalogue)."""

    list_display = ("name", "short_name", "address", "is_active", "venue_count")
    list_filter = ("is_active",)
    search_fields = ("name", "short_name", "address")
    ordering = ("name",)

    @admin.display(description="Venues")
    def venue_count(self, obj):
        return obj.venues.count()


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    """Admin for rooms within buildings (what a meeting's venue is selected from)."""

    list_display = ("name", "building", "floor", "capacity", "is_active")
    list_filter = ("is_active", "building")
    search_fields = ("name", "building__name", "floor")
    autocomplete_fields = ("building",)
    ordering = ("building__name", "name")
