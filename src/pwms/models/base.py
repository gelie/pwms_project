import uuid

from django.db import models


class BaseModel(models.Model):
    # Primary Key (id) for fast internal database indexes, joins, and foreign keys
    # Unique public identifier for URLs, API endpoints, and external integration
    public_id = models.UUIDField(
        default=uuid.uuid7,
        editable=False,
        unique=True,
        db_index=True,
        help_text="Public UUIDv7 identifier for external references and API routes.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
