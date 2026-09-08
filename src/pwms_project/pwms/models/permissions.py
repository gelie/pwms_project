from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django_extensions.db.fields import AutoSlugField

from .base import BaseModel
from .groups import Group
from .users import User


class UserRole(BaseModel):
    """Roles and non-state-dependent workflow permissions for groups."""

    name = models.CharField(max_length=100, unique=True)
    slug = AutoSlugField(populate_from="name", unique=True)
    description = models.TextField(blank=True)
    can_transition_workflows = models.BooleanField(default=False)
    can_create_workflows = models.BooleanField(default=False)
    can_assign_workflows = models.BooleanField(default=False)
    can_manage_permissions = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return str(self.name)

    def get_absolute_url(self):
        return reverse("bungeni:role_detail", kwargs={"slug": self.slug})

    def get_workflow_permissions(self):
        return {
            "can_transition": self.can_transition_workflows,
            "can_create": self.can_create_workflows,
            "can_assign": self.can_assign_workflows,
            "can_manage_permissions": self.can_manage_permissions,
        }


class GroupMembership(BaseModel):
    """Links users to groups with specific roles."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="members")
    role = models.ForeignKey(UserRole, on_delete=models.PROTECT)
    start_date = models.DateField(default=timezone.now)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)  # type: ignore
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="membership_changes",
        help_text="User who last modified this membership record.",
    )

    class Meta:
        ordering = ["-start_date"]
        indexes = [models.Index(fields=["user", "is_active"])]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "group", "role", "start_date"],
                name="workflows_groupmembership_user_group_role_start_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.user} - {self.role} in {self.group}"

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError("End date cannot be before start date.")
