# =======================================================================
# Sharepoint Models for Sites and Drives
# =======================================================================


import uuid

from django.db import models
from django.utils import timezone

from .base import BaseModel
from .users import User


class SharepointSite(BaseModel):
    name = models.CharField(max_length=200)
    url = models.URLField()
    site_id = models.CharField(max_length=200)
    is_personal_site = models.BooleanField(default=False)
    # Local curation flag: only enabled sites are offered by the document
    # picker. Deliberately never written by ``populate_sites``, so re-running
    # the tenant sync cannot undo an administrator's choice here.
    enabled = models.BooleanField(
        default=True,
        help_text=(
            "Uncheck to hide this site (and its drives) from the attachment picker."
        ),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True)
    # When this record was last refreshed from Sharepoint.
    last_synced_at = models.DateTimeField(
        default=timezone.now,
        help_text="When this record was last refreshed from Sharepoint.",
    )
    # Timestamp reported by Sharepoint (e.g., lastModifiedDateTime).
    remote_modified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="The last modified timestamp reported by Sharepoint for this site.",
    )

    def __str__(self):
        return self.name


class SharepointSiteMember(BaseModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    site = models.ForeignKey(SharepointSite, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    date_added = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user.username} - {self.site.name}"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["site", "user"],
                name="workflows_sitemember_site_user_uniq",
            ),
        ]


class SharepointDrive(BaseModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    site = models.ForeignKey(SharepointSite, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    drive_id = models.CharField(max_length=200)

    def __str__(self):
        return self.name


class SharepointToken(BaseModel):
    """
    Stores Sharepoint Graph API access tokens for application-level authentication.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    access_token = models.TextField(help_text="Sharepoint access token")
    refresh_token = models.TextField(blank=True, help_text="Sharepoint refresh token")
    expires_at = models.DateTimeField(help_text="Token expiration time")
    is_active = models.BooleanField(
        default=True, help_text="Whether this token is active"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Token {'Active' if self.is_active else 'Inactive'} - expires {self.expires_at}"

    def is_expired(self):
        """Check if the token is expired"""
        from django.utils import timezone

        return timezone.now() >= self.expires_at

    class Meta:
        ordering = ["-created_at"]


class SharepointFolder(BaseModel):
    """
    Represents a folder hierarchy in Sharepoint for navigation.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    site = models.ForeignKey(SharepointSite, on_delete=models.CASCADE)
    drive = models.ForeignKey(SharepointDrive, on_delete=models.CASCADE)
    folder_id = models.CharField(max_length=200, help_text="Sharepoint folder ID")
    name = models.CharField(max_length=200)
    parent_folder = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="subfolders",
    )
    web_url = models.URLField(
        blank=True, null=True, help_text="Direct URL to folder in Sharepoint"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} - {self.site.name}"

    def get_full_path(self):
        """Get the full path from root to this folder"""
        if self.parent_folder:
            return f"{self.parent_folder.get_full_path()}/{self.name}"
        return f"/{self.name}"

    class Meta:
        ordering = ["site", "drive", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["site", "drive", "folder_id"],
                name="workflows_Sharepointfolder_site_drive_folder_uniq",
            ),
        ]
