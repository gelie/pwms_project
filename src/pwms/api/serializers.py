from auditlog.models import LogEntry
from rest_framework import serializers


class AuditLogEntrySerializer(serializers.ModelSerializer):
    """Serialize an ``auditlog.LogEntry`` for the workflow audit API."""

    actor_email = serializers.SerializerMethodField()
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    changes = serializers.JSONField(
        source="changes_dict"
    )  # before/after field diffs (dict)

    class Meta:
        model = LogEntry
        fields = [
            "id",
            "action",  # 0: Create, 1: Update, 2: Delete
            "action_display",  # human-readable label
            "actor_email",
            "timestamp",
            "changes",
            "remote_addr",  # IP captured by AuditlogMiddleware
        ]
        read_only_fields = fields

    def get_actor_email(self, obj) -> str | None:
        """Actor email, or ``None`` when the change was made anonymously."""
        return getattr(obj.actor, "email", None) if obj.actor_id else None
