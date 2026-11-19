from datetime import datetime, time

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts.permissions import IsPayrollManager, IsSuperAdmin

from .history import SUPER_ADMIN_ONLY, TRACKED_MODELS, history_model
from .models import AuditAction, AuditEvent
from .serializers import AuditEventSerializer, ChangeRecordSerializer


def _parse_when(value, name, end_of_day=False):
    """
    Accept an ISO date (YYYY-MM-DD) or datetime. A bare date means the whole day:
    start of day for `since`, end of day for `until`.
    """
    if not value:
        return None
    error = ValidationError({name: "Use YYYY-MM-DD or an ISO 8601 datetime."})
    try:
        # Check for a bare date FIRST: Django 5's parse_datetime also accepts
        # "YYYY-MM-DD" (as midnight), which would make `until=<date>` exclude that day.
        day = parse_date(value)
        parsed = None if day else parse_datetime(value)
    except ValueError:  # well-formed but impossible, e.g. month 13
        raise error
    if day:
        return timezone.make_aware(datetime.combine(day, time.max if end_of_day else time.min))
    if parsed is None:
        raise error
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _int_param(params, name):
    value = params.get(name)
    if value in (None, ""):
        return None
    if not value.isdigit():
        raise ValidationError({name: "Must be a whole number."})
    return int(value)


DATE_PARAMS = [
    OpenApiParameter("since", OpenApiTypes.STR, description="From this date/datetime (inclusive)."),
    OpenApiParameter("until", OpenApiTypes.STR, description="Up to this date/datetime (inclusive)."),
]


@extend_schema(parameters=[
    OpenApiParameter("model", OpenApiTypes.STR, required=True, enum=list(TRACKED_MODELS)),
    OpenApiParameter("object_id", OpenApiTypes.INT, description="Only changes to this record."),
    OpenApiParameter("changed_by", OpenApiTypes.INT, description="Only changes made by this user id."),
    *DATE_PARAMS,
])
class ChangeHistoryView(generics.ListAPIView):
    """
    Who changed what, when: create/update/delete history with field-level old/new values.

    Payroll managers may read history for payroll and staff data; history of user
    accounts is restricted to Super Administrators.
    """

    permission_classes = [IsPayrollManager]
    serializer_class = ChangeRecordSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return history_model("salaryscale").objects.none()
        p = self.request.query_params
        name = (p.get("model") or "").lower()
        if name not in TRACKED_MODELS:
            raise ValidationError({"model": f"Required. One of: {', '.join(TRACKED_MODELS)}."})
        if name in SUPER_ADMIN_ONLY and not self.request.user.is_super_admin:
            raise PermissionDenied("Only a Super Administrator may view user account history.")

        qs = history_model(name).objects.select_related("history_user").order_by("-history_date", "-history_id")
        if (object_id := _int_param(p, "object_id")) is not None:
            qs = qs.filter(id=object_id)
        if (changed_by := _int_param(p, "changed_by")) is not None:
            qs = qs.filter(history_user_id=changed_by)
        if since := _parse_when(p.get("since"), "since"):
            qs = qs.filter(history_date__gte=since)
        if until := _parse_when(p.get("until"), "until", end_of_day=True):
            qs = qs.filter(history_date__lte=until)
        return qs


@extend_schema(parameters=[
    OpenApiParameter("action", OpenApiTypes.STR, enum=AuditAction.values),
    OpenApiParameter("actor", OpenApiTypes.INT, description="Only events by this user id."),
    OpenApiParameter("username", OpenApiTypes.STR, description="Actor username (also matches failed-login attempts)."),
    OpenApiParameter("target_type", OpenApiTypes.STR),
    OpenApiParameter("target_id", OpenApiTypes.STR),
    *DATE_PARAMS,
])
class AuditEventListView(generics.ListAPIView):
    """Activity log (logins, password changes, payroll runs, payslip downloads). Super Administrator only."""

    permission_classes = [IsSuperAdmin]
    serializer_class = AuditEventSerializer

    def get_queryset(self):
        qs = AuditEvent.objects.all()
        p = self.request.query_params
        if action := p.get("action"):
            if action not in AuditAction.values:
                raise ValidationError({"action": f"One of: {', '.join(AuditAction.values)}."})
            qs = qs.filter(action=action)
        if (actor := _int_param(p, "actor")) is not None:
            qs = qs.filter(actor_id=actor)
        if username := p.get("username"):
            qs = qs.filter(actor_username__iexact=username)
        if target_type := p.get("target_type"):
            qs = qs.filter(target_type__iexact=target_type)
        if target_id := p.get("target_id"):
            qs = qs.filter(target_id=target_id)
        if since := _parse_when(p.get("since"), "since"):
            qs = qs.filter(timestamp__gte=since)
        if until := _parse_when(p.get("until"), "until", end_of_day=True):
            qs = qs.filter(timestamp__lte=until)
        return qs
