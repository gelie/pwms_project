from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django_extensions.db.fields import AutoSlugField

from .base import BaseModel, SoftDeleteModelMixin, SoftDeleteQuerySet
from .groups import Group
from .users import User


class Role(BaseModel):
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


class GroupMembership(SoftDeleteModelMixin, BaseModel):
    """Links users to groups with specific roles.

    Rows are never hard-deleted by application code: ``delete()`` and
    ``QuerySet.delete()`` deactivate them (see ``SoftDeleteModelMixin``), and the
    user/group FKs use ``PROTECT`` so cascades cannot remove them either. Ending
    a membership in the UI and deleting it are therefore the same soft operation.
    """

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="memberships")
    group = models.ForeignKey(Group, on_delete=models.PROTECT, related_name="members")
    role = models.ForeignKey(Role, on_delete=models.PROTECT)
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

    objects = SoftDeleteQuerySet.as_manager()

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

    @property
    def current_delegation(self):
        """The delegation currently lending this membership's duties, if any.

        Delegation does not change ``is_active``: a member on leave is still a
        member, and the history must keep saying so. The group page reflects the
        delegation alongside the membership instead. Prefer the group page's
        prefetched map when rendering a list; this costs one query per row.
        """
        return (
            DutyDelegation.objects.current()
            .filter(group_id=self.group_id, delegator_id=self.user_id)
            .select_related("delegate")
            .first()
        )

    def delete(self, using=None, keep_parents=False):
        """Soft-delete the membership and stop delegations that relied on it."""
        result = super().delete(using=using, keep_parents=keep_parents)
        # Only the delegator's membership backs a delegation: the delegate may
        # be outside the group, so removing *their* membership changes nothing.
        for delegation in DutyDelegation.objects.filter(
            group=self.group, delegator=self.user, is_active=True
        ):
            delegation.is_active = False
            delegation.save(using=using, update_fields=["is_active", "updated_at"])
        return result


class DutyDelegationQuerySet(SoftDeleteQuerySet):
    """Queries for :class:`DutyDelegation`."""

    def current(self, on_date=None):
        """Delegations covering ``on_date`` (today by default)."""
        on_date = on_date or timezone.localdate()
        return self.filter(
            is_active=True, start_date__lte=on_date, end_date__gte=on_date
        )


class DutyDelegation(SoftDeleteModelMixin, BaseModel):
    """A member's duties in one group, delegated to another active user.

    The delegate stands in for the delegator while the delegation is current:
    the delegator's active memberships in ``group`` count for the delegate in
    role checks (see :meth:`User.delegated_memberships`), so work that would
    reach the delegator through a committee role reaches the delegate too.

    The delegate does **not** have to belong to the group — the committee
    secretary records the delegation on the member's behalf, and the stand-in may
    be anyone active in PWMS. Delegation is scoped to one group and one member,
    and only the delegator's own duties travel — it is not a general grant of the
    committee's authority.
    """

    group = models.ForeignKey(
        Group, on_delete=models.PROTECT, related_name="duty_delegations"
    )
    delegator = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="duties_delegated"
    )
    delegate = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="duties_received"
    )
    start_date = models.DateField(default=timezone.localdate)
    #: The day the delegation lapses. Required: a delegation is always bounded.
    end_date = models.DateField()
    is_active = models.BooleanField(default=True)  # type: ignore
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duty_delegations_created",
        help_text="User who recorded this delegation.",
    )

    objects = DutyDelegationQuerySet.as_manager()

    class Meta:
        ordering = ["-start_date", "-pk"]
        indexes = [models.Index(fields=["delegate", "is_active"])]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "delegator", "delegate", "start_date"],
                name="workflows_dutydelegation_group_delegator_delegate_start_uniq",
            ),
            models.CheckConstraint(
                condition=~models.Q(delegator=models.F("delegate")),
                name="workflows_dutydelegation_not_self",
            ),
        ]

    def __str__(self):
        return (
            f"{self.delegator} -> {self.delegate} in {self.group} until {self.end_date}"
        )

    @property
    def is_current(self):
        """True while the delegation is active and today falls inside its range."""
        today = timezone.localdate()
        return self.is_active and self.start_date <= today <= self.end_date

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError(
                {"end_date": "The delegation cannot end before it starts."}
            )
        if (
            self.delegator_id
            and self.delegate_id
            and self.delegator_id == self.delegate_id
        ):
            raise ValidationError(
                {"delegate": "A member cannot delegate their duties to themselves."}
            )
        # Ending (or archiving) a delegation must stay possible after either side
        # has left the group, so the membership rule applies to live rows only.
        if not self.is_active:
            return
        if not self.group_id:
            return
        if (
            self.delegator_id
            and not GroupMembership.objects.filter(
                group_id=self.group_id, user_id=self.delegator_id, is_active=True
            ).exists()
        ):
            raise ValidationError(
                {"delegator": "The delegator must be an active member of the group."}
            )
        if not self.delegate_id:
            return
        if GroupMembership.objects.filter(
            group_id=self.group_id, user_id=self.delegate_id, is_active=True
        ).exists():
            raise ValidationError(
                {
                    "delegate": (
                        "The delegate must not already be a member of this group."
                    )
                }
            )
        if (
            DutyDelegation.objects.current()
            .filter(group_id=self.group_id, delegate_id=self.delegate_id)
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError(
                {"delegate": "That person is already an active delegate in this group."}
            )
        if (
            self.delegator_id
            and not User.delegation_peers(self.delegator)
            .filter(pk=self.delegate_id)
            .exists()
        ):
            raise ValidationError(
                {
                    "delegate": (
                        "Members of Parliament can only delegate to other Members "
                        "of Parliament, and staff to staff."
                    )
                }
            )
