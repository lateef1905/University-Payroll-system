from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """Read-only: the activity log must never be edited."""

    list_display = ("timestamp", "action", "actor_username", "target_type", "target_id", "ip_address")
    list_filter = ("action", "timestamp")
    search_fields = ("actor_username", "target_id", "ip_address")
    readonly_fields = [f.name for f in AuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
