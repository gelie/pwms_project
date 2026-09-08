import uuid

from django.db import models


class BaseModel(models.Model):
    public_id = models.UUIDField(default=uuid.uuid7, editable=False, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
