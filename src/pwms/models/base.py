import uuid

from django.db import models


class SoftDeleteQuerySet(models.QuerySet):
    """Queryset whose ``delete()`` deactivates instead of removing rows.

    Rows are kept so membership and delegation history survives, and so routine
    application code cannot lose an authority grant by accident. A deliberate
    maintenance command can still wipe rows with :meth:`hard_delete`.
    """

    def delete(self):
        """Soft-delete every row; returns the same ``(count, detail)`` shape."""
        deleted = 0
        for obj in self:
            count, _ = obj.delete()
            deleted += count
        return deleted, {self.model._meta.label: deleted}

    def hard_delete(self):
        """Permanently remove rows. Reserved for explicit maintenance commands."""
        return super().delete()


class SoftDeleteModelMixin(models.Model):
    """Make ``delete()`` deactivate the row instead of removing it.

    The concrete model must carry an ``is_active`` flag (and, for the timestamp
    to refresh, ``BaseModel``'s ``updated_at``). This is the application-level
    guarantee; the paired ``SoftDeleteQuerySet`` closes the bulk path, and the
    models' FKs use ``PROTECT`` so a cascade cannot reach the row either.
    """

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        label = self._meta.label
        if not self.is_active:
            return 0, {label: 0}
        self.is_active = False
        self.save(using=using, update_fields=["is_active", "updated_at"])
        return 1, {label: 1}


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
