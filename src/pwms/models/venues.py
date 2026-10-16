"""Buildings and venues: where a meeting is held on the parliamentary premises.

A meeting can be held on the premises (a room in a building) or off the premises.
Those are two different things and are modelled separately:

* an on-premises meeting points at a :class:`Venue` — one room in one
  :class:`Building`. Buildings and venues are curated reference data (through the
  admin), so the room is *selected* rather than typed, and the same room reads the
  same way on every meeting;
* an off-premises meeting instead carries a free-text name and address on the
  meeting itself (``Meeting.off_site_venue`` / ``off_site_address``), because an
  external venue is a one-off and should not have to be catalogued first.

:class:`Venue` is deliberately a child of :class:`Building` rather than a flat
list with a building name: "Committee Room M314" only means something as "M314 in
the Marks Building", and a building's address is shared by all its rooms.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from .base import BaseModel


class Building(BaseModel):
    """A building on the parliamentary precinct (or one of its addresses)."""

    name = models.CharField(max_length=255, unique=True)
    short_name = models.CharField(
        max_length=50, blank=True, help_text="Short label, e.g. NA or NCOP."
    )
    address = models.CharField(
        max_length=255, blank=True, help_text="Street address of the building."
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(
        default=True, help_text="Inactive buildings are hidden from the venue picker."
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Venue(BaseModel):
    """A room / chamber within a :class:`Building` where a meeting can be held."""

    building = models.ForeignKey(
        Building,
        on_delete=models.CASCADE,
        related_name="venues",
        help_text="Building the room is in.",
    )
    name = models.CharField(max_length=255, help_text="Room or chamber name.")
    floor = models.CharField(max_length=50, blank=True)
    capacity = models.PositiveIntegerField(
        null=True, blank=True, help_text="Seating capacity, if known."
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(
        default=True, help_text="Inactive venues are hidden from the meeting form."
    )

    class Meta:
        ordering = ["building__name", "name"]
        verbose_name = _("venue")
        verbose_name_plural = _("venues")
        constraints = [
            models.UniqueConstraint(
                fields=["building", "name"],
                name="venues_venue_building_name_uniq",
            ),
        ]
        indexes = [models.Index(fields=["building", "name"])]

    def __str__(self):
        return f"{self.building.name} — {self.name}"

    @property
    def full_name(self):
        """The fully-qualified room label (building then room)."""
        return f"{self.building.name} — {self.name}"
