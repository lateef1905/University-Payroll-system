"""
Activity log: security- and payroll-relevant actions that are not plain data edits
(logins, password changes, payroll runs, payslip access).

Data edits (who changed a salary scale, tax band, staff profile, user...) are recorded
separately by django-simple-history - see `history = HistoricalRecords()` on each model
and audit/history.py.
"""
from django.conf import settings
from django.db import models


class AuditAction(models.TextChoices):
    LOGIN_SUCCESS = "LOGIN_SUCCESS", "Login succeeded"
    LOGIN_FAILED = "LOGIN_FAILED", "Login failed"
    PASSWORD_CHANGED = "PASSWORD_CHANGED", "Password changed"
    BATCH_TRIGGERED = "BATCH_TRIGGERED", "Payroll batch triggered"
    BATCH_MARKED_FAILED = "BATCH_MARKED_FAILED", "Payroll batch manually marked as failed"
    PAYSLIP_DOWNLOADED = "PAYSLIP_DOWNLOADED", "Payslip downloaded"


class AuditEvent(models.Model):
    """One logged action. Append-only: rows cannot be edited or deleted through the ORM."""

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    action = models.CharField(max_length=30, choices=AuditAction.choices, db_index=True)
    # Users are never hard-deleted (they are deactivated), so PROTECT keeps the trail intact.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="audit_events"
    )
    # Snapshot, so the log stays readable even if a username later changes. For failed
    # logins this is the username that was attempted.
    actor_username = models.CharField(max_length=150, blank=True)
    target_type = models.CharField(max_length=50, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp", "-id"]
        indexes = [
            models.Index(fields=["actor", "-timestamp"], name="audit_actor_time_idx"),
            models.Index(fields=["target_type", "target_id"], name="audit_target_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError("Audit events are immutable once created.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Audit events cannot be deleted.")

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.action} by {self.actor_username or '-'}"
