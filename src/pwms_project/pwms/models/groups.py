from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.fields import AutoSlugField
from mptt.models import MPTTModel, TreeForeignKey

from .base import BaseModel


class Group(MPTTModel, BaseModel):
    """Hierarchical group structure for parliamentary and administrative units."""

    GROUP_TYPE_CHOICES = [
        ("legislature", _("Legislature")),
        ("house", _("House")),
        ("portfolio_committee", _("Portfolio Committee")),
        ("select_committee", _("Select Committee")),
        ("special_committee", _("Special Committee")),
        ("public_accounts_committee", _("Public Accounts Committee")),
        ("internal_committee", _("Internal Committee")),
        ("ad_hoc_committee", _("Ad Hoc Committee")),
        ("joint_committee", _("Joint Committee")),
        ("administration", _("Administration")),
        ("office", _("Office")),
        ("division", _("Division")),
        ("section", _("Section")),
        ("business_unit", _("Business Unit")),
        ("party", _("Party")),
        ("executive", _("Executive")),
        ("presidency", _("Presidency")),
        ("ministry", _("Ministry")),
        ("department", _("Department")),
        ("province", _("Province")),
        ("premier", _("Premier")),
        ("delegation", _("Delegation")),
    ]

    name = models.CharField(max_length=255)
    slug = AutoSlugField(populate_from="name", unique=True, editable=False)
    short_name = models.CharField(max_length=50, blank=True)
    group_type = models.CharField(max_length=50, choices=GROUP_TYPE_CHOICES)
    parent = TreeForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="children"
    )
    description = models.TextField(blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)  # type: ignore
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    location = models.CharField(max_length=255, blank=True)

    class MPTTMeta:
        order_insertion_by = ["name"]

    class Meta:  # type: ignore
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "parent"],
                name="workflows_group_name_parent_uniq",
            ),
        ]

    def __str__(self):
        return str(self.name)

    def get_full_path(self):
        ancestors = self.get_ancestors(include_self=True)
        return " > ".join([group.name for group in ancestors])

    def get_active_members(self):
        from .users import User

        return User.objects.filter(
            memberships__group=self, memberships__is_active=True
        ).distinct()
