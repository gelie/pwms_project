# utils/audit_helpers.py
from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.shortcuts import get_object_or_404


def get_audit_trail_for_instance(instance):
    """
    Retrieves the full audit log history for a concrete model instance.
    Uses internal integer `id` for high-performance ContentType filtering.
    """
    content_type = ContentType.objects.get_for_model(instance)
    return (
        LogEntry.objects.filter(content_type=content_type, object_id=instance.id)
        .select_related("actor")
        .order_by("-timestamp")
    )


def get_audit_trail_by_public_id(model_class, public_id):
    """
    Helper for API views to retrieve logs given a public UUIDv7.
    """
    instance = get_object_or_404(model_class, public_id=public_id)
    return get_audit_trail_for_instance(instance)
