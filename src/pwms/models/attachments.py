"""Attachments: SharePoint documents linked to a workflow instance.

An attachment points at a file that lives in SharePoint (site / drive / item
coordinates are stored, so it can be opened without another Graph round trip)
and is linked to its owning record through a generic foreign key — the same
``content_type`` + ``object_id`` shape the RBAC tables use, so one table serves
every concrete workflow subclass.
"""

import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from .base import BaseModel
from .sharepoint import SharepointDrive, SharepointFolder, SharepointSite
from .users import User


class Attachment(BaseModel):
    """A SharePoint document attached to a record (workflow instance, event, ...)."""

    ATTACHMENT_TYPE_CHOICES = [
        ("agenda", "Agenda"),
        ("agreement", "Agreement"),
        ("annexure", "Annexure"),
        ("bill", "Bill"),
        ("brief", "Brief"),
        ("correspondence", "Correspondence"),
        ("document", "Document"),
        ("draft", "Draft"),
        ("gazette", "Gazette"),
        ("memorandum", "Memorandum"),
        ("minutes", "Minutes"),
        ("motion", "Motion"),
        ("notice", "Notice"),
        ("order_paper", "Order Paper"),
        ("other", "Other"),
        ("petition", "Petition"),
        ("presentation", "Presentation"),
        ("question", "Question"),
        ("report", "Report"),
        ("resolution", "Resolution"),
        ("response", "Response"),
        ("speech", "Speech"),
        ("submission", "Submission"),
        ("transcript", "Transcript"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)

    # Generic foreign key to link to any model (Workflow, Event, etc.)
    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, null=True, blank=True
    )
    object_id = models.CharField(max_length=64, null=True, blank=True)
    content_object = GenericForeignKey("content_type", "object_id")

    name = models.CharField(max_length=500)
    drive_id = models.CharField(max_length=512, help_text="SharePoint drive ID")
    item_id = models.CharField(max_length=512, help_text="SharePoint item ID")
    mimetype = models.CharField(
        max_length=512, blank=True, null=True, help_text="MIME type of the file"
    )
    size = models.BigIntegerField(blank=True, null=True, help_text="File size in bytes")
    download_url = models.URLField(
        max_length=2048,
        blank=True,
        null=True,
        help_text="SharePoint download URL",
    )

    # Enhanced SharePoint references, resolved from the mirrored Sharepoint* rows
    # so the browser never has to call Graph just to render an attachment.
    sharepoint_site = models.ForeignKey(
        SharepointSite, on_delete=models.CASCADE, null=True, blank=True
    )
    sharepoint_drive = models.ForeignKey(
        SharepointDrive, on_delete=models.CASCADE, null=True, blank=True
    )
    sharepoint_folder = models.ForeignKey(
        SharepointFolder, on_delete=models.CASCADE, null=True, blank=True
    )
    sharepoint_web_url = models.URLField(
        max_length=2048,
        blank=True,
        null=True,
        help_text="Direct SharePoint web URL",
    )
    sharepoint_folder_path = models.CharField(
        max_length=500, blank=True, help_text="Folder path in SharePoint"
    )

    type = models.CharField(max_length=50, choices=ATTACHMENT_TYPE_CHOICES)
    uploaded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        owner = self.content_object or "Unattached"
        return f"{self.name} - {owner}"

    def get_sharepoint_url(self):
        """Get the direct SharePoint URL for this attachment."""
        return self.sharepoint_web_url or self.download_url

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["sharepoint_site", "sharepoint_drive"]),
        ]
        constraints = [
            # The same SharePoint item may only be attached once to a given
            # record (it may still be attached to other records).
            models.UniqueConstraint(
                fields=["content_type", "object_id", "item_id"],
                name="workflows_attachment_object_item_uniq",
            ),
        ]


class AttachmentVersion(BaseModel):
    """One version of an :class:`Attachment`'s SharePoint item.

    SharePoint keeps its own version history; these rows are a local mirror of
    it, refreshed on demand by ``pwms.services.attachments.sync_versions``. The
    mirror means the history renders without a Graph round trip and superseded
    versions stay queryable. ``version_id`` is SharePoint's own version label
    (``"2.0"``), and exactly one row is normally flagged ``is_current``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    attachment = models.ForeignKey(
        Attachment, on_delete=models.CASCADE, related_name="versions"
    )
    version_id = models.CharField(
        max_length=128, help_text="SharePoint version ID, e.g. 2.0"
    )
    size = models.BigIntegerField(
        blank=True, null=True, help_text="File size in bytes at this version"
    )
    modified_at = models.DateTimeField(
        blank=True, null=True, help_text="When SharePoint saved this version"
    )
    modified_by = models.CharField(
        max_length=512,
        blank=True,
        help_text="Who saved this version, as reported by SharePoint",
    )
    is_current = models.BooleanField(
        default=False, help_text="The version currently held by SharePoint"
    )

    def __str__(self):
        return f"{self.attachment.name} v{self.version_id}"

    class Meta:
        ordering = ["-modified_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["attachment", "version_id"],
                name="workflows_attachmentversion_version_uniq",
            ),
        ]
        indexes = [models.Index(fields=["attachment", "-modified_at"])]
