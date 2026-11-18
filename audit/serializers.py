from rest_framework import serializers

from .history import CHANGE_TYPES, describe_changes
from .models import AuditEvent


class ChangeRecordSerializer(serializers.Serializer):
    """One history row from django-simple-history, as a readable diff."""

    history_id = serializers.IntegerField()
    object_id = serializers.SerializerMethodField()
    object_repr = serializers.SerializerMethodField()
    change_type = serializers.SerializerMethodField()
    changed_at = serializers.DateTimeField(source="history_date")
    changed_by = serializers.SerializerMethodField()
    changes = serializers.SerializerMethodField()

    def get_object_id(self, obj) -> int:
        return obj.id

    def get_object_repr(self, obj) -> str:
        try:
            return str(obj.instance)
        except Exception:  # noqa: BLE001 - e.g. a related row that no longer exists
            return f"#{obj.id}"

    def get_change_type(self, obj) -> str:
        return CHANGE_TYPES.get(obj.history_type, obj.history_type)

    def get_changed_by(self, obj) -> dict | None:
        user = obj.history_user
        return {"id": user.pk, "username": user.get_username()} if user else None

    def get_changes(self, obj) -> list:
        return describe_changes(obj)


class AuditEventSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = AuditEvent
        fields = [
            "id", "timestamp", "action", "action_display", "actor", "actor_username",
            "target_type", "target_id", "ip_address", "user_agent", "detail",
        ]
        read_only_fields = fields
