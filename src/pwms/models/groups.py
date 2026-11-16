from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.fields import AutoSlugField
from mptt.models import MPTTModel, TreeForeignKey

from .base import BaseModel


class ParliamentTerm(BaseModel):
    number = models.PositiveSmallIntegerField(unique=True)  # 7 = 7th Parliament
    name = models.CharField(max_length=100)  # "7th Parliament"
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)  # null = still running

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return self.name


class Group(MPTTModel, BaseModel):
    class Type(models.TextChoices):
        PARLIAMENT = "parliament", _("Parliament (term)")
        HOUSE = "house", _("House")
        DELEGATION = "delegation", _("Provincial delegation")
        COMMITTEES = "committees", _("Committees (container)")
        COMMITTEE = "committee", _("Committee")
        MEMBERS = "members", _("Members (container)")
        PARTIES = "parties", _("Parties (container)")
        ADMINISTRATION = "administration", _("Administration")
        BRANCH = "branch", _("Branch")
        DIVISION = "division", _("Division")
        OFFICE = "office", _("Office")
        SECTION = "section", _("Section")
        UNIT = "unit", _("Unit")
        PARTY = "party", _("Party")
        EXECUTIVE = "executive", _("Executive")
        PRESIDENCY = "presidency", _("Presidency")
        MINISTRY = "ministry", _("Ministry")
        MUNICIPALITY = "municipality", _("Municipality")
        DEPARTMENT = "department", _("Department")
        PROVINCE = "province", _("Province")

    #: Group types that carry political office rather than employment: an
    #: appointment in one of these groups outlives an ERP payroll record.
    #: ``sync_users_oracle`` never ends such memberships, and ``User.ministers()``
    #: looks for appointments here.
    EXECUTIVE_GROUP_TYPES = ("executive", "presidency", "ministry")

    #: Group types that belong to a Parliament term. The legislative tree is
    #: created once per term; the administration tree is permanent (term=None).
    #: ``PARTY`` is here because the parties under a House's Parties container
    #: belong to the term too.
    TERM_GROUP_TYPES = (
        Type.PARLIAMENT,
        Type.HOUSE,
        Type.COMMITTEES,
        Type.COMMITTEE,
        Type.MEMBERS,
        Type.PARTIES,
        Type.DELEGATION,
        Type.PARTY,
    )

    name = models.CharField(max_length=200)
    prefix = models.CharField(max_length=30, blank=True)
    description = models.TextField(blank=True)
    slug = AutoSlugField(populate_from="name", unique=True, editable=False)
    short_name = models.CharField(max_length=50, blank=True)
    group_type = models.CharField(max_length=20, choices=Type.choices)
    parent = TreeForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    # parliament.gov.za committee-details/<ID>; lets you re-sync later.
    external_id = models.PositiveIntegerField(null=True, blank=True)
    # Set only on the legislative tree. Administration is permanent -> null.
    term = models.ForeignKey(
        ParliamentTerm,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="groups",
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    location = models.CharField(max_length=255, blank=True)

    class MPTTMeta:
        order_insertion_by = ["name"]

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "parent"],
                name="workflows_group_name_parent_uniq",
            ),
            models.UniqueConstraint(
                fields=["term", "external_id"], name="uniq_term_external_id"
            ),
        ]

    def __str__(self):
        return str(self.name)

    def clean(self):
        if self.term and self.group_type not in self.TERM_GROUP_TYPES:
            raise ValidationError(
                {"term": "Terms are only applicable to the legislative tree."}
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def get_full_path(self):
        ancestors = self.get_ancestors(include_self=True)
        return " > ".join([group.name for group in ancestors])

    @property
    def full_path(self):
        """The group's name preceded by its ancestors, joined with `` > ``."""
        return self.get_full_path()

    def get_active_members(self):
        from .users import User

        return User.objects.filter(
            memberships__group=self, memberships__is_active=True
        ).distinct()
