import base64
import datetime
import hashlib
import hmac
import logging

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .base import BaseModel

logger = logging.getLogger(__name__)


class User(AbstractUser, BaseModel):
    """
    Custom User model extending Django's AbstractUser.
    Users can be MPs, staff, administrators, etc.
    """

    TITLE_CHOICES = [
        ("mr", _("Mr.")),
        ("ms", _("Ms.")),
        ("mrs", _("Mrs.")),
        ("dr", _("Dr.")),
        ("prof", _("Prof.")),
        ("hon", _("Hon.")),
        ("rt_hon", _("Rt. Hon.")),
    ]

    EMPLOYEE_TYPE_CHOICES = [
        ("staff", _("Staff")),
        ("member", _("Member")),
        ("graduate", _("Graduate")),
    ]

    IDENTITY_SOURCE_CHOICES = [
        ("erp", _("ERP (Oracle)")),
        ("local", _("Locally managed")),
    ]

    #: Roles that carry an executive office (see ``ministers()``).
    EXECUTIVE_ROLE_NAMES = ("Minister", "Deputy Minister")

    GENDER_CHOICES = [
        ("male", _("Male")),
        ("female", _("Female")),
        ("other", _("Other")),
    ]

    title = models.CharField(max_length=30, choices=TITLE_CHOICES, blank=True)
    middle_name = models.CharField(max_length=100, blank=True)
    employee_type = models.CharField(
        max_length=10, choices=EMPLOYEE_TYPE_CHOICES, blank=True
    )
    identity_source = models.CharField(
        max_length=10,
        choices=IDENTITY_SOURCE_CHOICES,
        default="erp",
        help_text=_(
            "Which system owns this identity. 'erp' (the default) leaves the "
            "user to sync_users_oracle; set 'local' for identities managed in "
            "PWMS only - e.g. a Minister appointed from outside the ERP, who "
            "must not be deactivated when Oracle does not know them."
        ),
    )
    positiondesc = models.CharField(max_length=100, blank=True)
    supervisor = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True
    )
    department = models.ForeignKey(
        "Group", on_delete=models.SET_NULL, null=True, blank=True
    )
    date_of_birth = models.DateField(null=True, blank=True)
    idno_encrypted = models.TextField(blank=True)
    idno_hmac = models.CharField(
        max_length=64, unique=True, db_index=True, blank=True, null=True
    )
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    bio = models.TextField(blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)
    is_mp = models.BooleanField(
        default=False, help_text=_("Is this user a Member of Parliament?")
    )
    is_staff_member = models.BooleanField(
        default=False, help_text=_("Is this user a staff member?")
    )
    constituency = models.CharField(max_length=200, blank=True)
    party_affiliation = models.CharField(max_length=100, blank=True)
    date_joined_parliament = models.DateField(null=True, blank=True)
    termination_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)  # type: ignore

    class Meta:
        ordering = ["last_name", "first_name"]

    @staticmethod
    def _compute_idno_hmac(plain_id: str) -> str | None:
        """Return hex-encoded HMAC-SHA256 of the ID using settings.IDNO_HMAC_KEY."""
        key = getattr(settings, "IDNO_HMAC_KEY", None)
        if not plain_id or not key:
            return None
        key_bytes = key.encode("utf-8") if isinstance(key, str) else key
        return hmac.new(key_bytes, plain_id.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _encrypt_idno(plain_id: str) -> str | None:
        """Encrypt the ID number using Fernet if configured."""
        key = getattr(settings, "IDNO_ENC_KEY", None)
        if not plain_id or not key:
            return None
        try:
            from cryptography.fernet import Fernet

            try:
                fernet = Fernet(key)
            except Exception as error:
                logger.error(error)
                fernet = Fernet(base64.urlsafe_b64encode(key.encode("utf-8")))
            return fernet.encrypt(plain_id.encode("utf-8")).decode("utf-8")
        except Exception as error:
            logger.error(error)
            return None

    @staticmethod
    def _decrypt_idno(ciphertext: str) -> str | None:
        key = getattr(settings, "IDNO_ENC_KEY", None)
        if not ciphertext or not key:
            return None
        try:
            from cryptography.fernet import Fernet

            try:
                fernet = Fernet(key)
            except Exception:
                fernet = Fernet(base64.urlsafe_b64encode(key.encode("utf-8")))
            return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except Exception:
            return None

    def set_idno(self, plain_id: str | None) -> None:
        """Set the user's ID number without storing plaintext."""
        hmac_value = self._compute_idno_hmac(plain_id or "")
        if hmac_value:
            self.idno_hmac = hmac_value
        encrypted_value = self._encrypt_idno(plain_id or "")
        if encrypted_value:
            self.idno_encrypted = encrypted_value

    def get_idno(self) -> str:
        """Return the decrypted ID number if available."""
        decrypted_value = (
            self._decrypt_idno(self.idno_encrypted)
            if getattr(self, "idno_encrypted", None)
            else None
        )
        return decrypted_value or ""

    def __str__(self):
        full_name = f"{self.title.title()} {self.first_name} {self.last_name}".strip()
        return full_name if full_name else self.username

    def get_full_name_with_title(self):
        parts = [self.title.title(), self.first_name, self.middle_name, self.last_name]
        return " ".join(part for part in parts if part)

    @property
    def display_name(self):
        """Label for the user in lists and search pickers (username as fallback)."""
        return self.get_full_name() or self.get_username()

    def get_absolute_url(self):
        return reverse("bungeni:user_detail", kwargs={"pk": self.pk})

    def clean(self):
        if self.date_of_birth and self.date_of_birth > datetime.date.today():
            raise ValidationError(
                {"date_of_birth": "Date of birth cannot be in the future."}
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    @classmethod
    def ministers(cls):
        """Active users holding an executive office, ordered by name.

        A Minister is a user with an *active* membership of an executive group
        (``Group.EXECUTIVE_GROUP_TYPES``) in the ``Minister`` or
        ``Deputy Minister`` role — the office is held over time, so a reshuffle
        ends the membership rather than editing the user.
        """
        from .groups import Group

        return (
            cls.objects.filter(
                is_active=True,
                memberships__is_active=True,
                memberships__role__name__in=cls.EXECUTIVE_ROLE_NAMES,
                memberships__group__group_type__in=Group.EXECUTIVE_GROUP_TYPES,
            )
            .distinct()
            .order_by("last_name", "first_name")
        )

    def executive_memberships(self):
        """This user's executive-office memberships, current or past.

        Ended appointments are included on purpose: an agreement or bill already
        on file still names the office holder who served at the time, so a
        reshuffle must not invalidate it.
        """
        from .groups import Group

        return self.get_groups_with_roles().filter(
            role__name__in=self.EXECUTIVE_ROLE_NAMES,
            group__group_type__in=Group.EXECUTIVE_GROUP_TYPES,
        )

    @property
    def current_portfolio(self):
        """Group of this user's current executive appointment, if any.

        ``None`` for anyone who is not a serving Minister or Deputy Minister;
        use :meth:`get_groups_with_roles` when the role is needed too.
        """
        membership = (
            self.executive_memberships()
            .filter(is_active=True)
            .order_by("-start_date")
            .first()
        )
        return membership.group if membership else None

    def get_groups_with_roles(self):
        from .permissions import GroupMembership

        return GroupMembership.objects.filter(user=self).select_related("group", "role")

    def has_role_in_group(self, role_name, group):
        from .permissions import GroupMembership

        return GroupMembership.objects.filter(
            user=self, group=group, role__name=role_name, is_active=True
        ).exists()

    def can_manage_permissions(self):
        from .permissions import GroupMembership

        return GroupMembership.objects.filter(
            user=self, role__can_manage_permissions=True, is_active=True
        ).exists()

    def can_transition_workflow(self, workflow_instance, transition):
        user_roles = (
            self.get_groups_with_roles()
            .filter(group=workflow_instance.group)
            .values_list("role", flat=True)
        )
        return transition.allowed_roles.filter(id__in=user_roles).exists()
