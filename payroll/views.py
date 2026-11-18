import re
import uuid

from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from accounts.permissions import IsPayrollManager
from audit.events import log_event
from audit.models import AuditAction
from staff.views import ProtectedDeleteMixin

from .models import AllowanceRule, BatchStatus, PayrollBatch, PayrollRecord, SalaryScale, TaxBand
from .payslip import render_payslip_pdf
from .serializers import (
    AllowanceRuleSerializer,
    PayrollBatchCreateSerializer,
    PayrollBatchSerializer,
    PayrollRecordSerializer,
    SalaryScaleSerializer,
    TaxBandSerializer,
)


class PayslipDownloadThrottle(UserRateThrottle):
    scope = "payslip"


class SalaryScaleViewSet(ProtectedDeleteMixin, viewsets.ModelViewSet):
    """Salary scale CRUD - Super Administrator / Bursary Officer only."""

    queryset = SalaryScale.objects.all()
    serializer_class = SalaryScaleSerializer
    permission_classes = [IsPayrollManager]

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        if p.get("scale"):
            qs = qs.filter(scale=p["scale"])
        if p.get("grade_level", "").isdigit():
            qs = qs.filter(grade_level=p["grade_level"])
        return qs


class AllowanceRuleViewSet(viewsets.ModelViewSet):
    queryset = AllowanceRule.objects.all()
    serializer_class = AllowanceRuleSerializer
    permission_classes = [IsPayrollManager]


class TaxBandViewSet(viewsets.ModelViewSet):
    queryset = TaxBand.objects.all()
    serializer_class = TaxBandSerializer
    permission_classes = [IsPayrollManager]

    def get_queryset(self):
        qs = super().get_queryset()
        year = self.request.query_params.get("tax_year", "")
        return qs.filter(tax_year=year) if year.isdigit() else qs


class PayrollBatchViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Trigger and monitor monthly payroll runs.

    POST returns **202 Accepted** immediately; poll GET /batches/{id}/ for progress.
    """

    queryset = PayrollBatch.objects.select_related("initiated_by").prefetch_related("faculties")
    permission_classes = [IsPayrollManager]

    def get_serializer_class(self):
        return PayrollBatchCreateSerializer if self.action == "create" else PayrollBatchSerializer

    @extend_schema(request=PayrollBatchCreateSerializer, responses={202: PayrollBatchSerializer})
    def create(self, request, *args, **kwargs):
        serializer = PayrollBatchCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        batch = serializer.save()
        log_event(
            request, AuditAction.BATCH_TRIGGERED, target=batch,
            period=f"{batch.period_year}-{batch.period_month:02d}",
            faculty_ids=sorted(batch.faculties.values_list("pk", flat=True)),
        )
        batch.refresh_from_db()  # inline (eager) runs may already have finished
        return Response(PayrollBatchSerializer(batch).data, status=status.HTTP_202_ACCEPTED)

    @extend_schema(request=None, responses=PayrollBatchSerializer)
    @action(detail=True, methods=["post"], url_path="mark-failed")
    def mark_failed(self, request, pk=None):
        """
        Manually fail a stuck PENDING/PROCESSING batch (e.g. its worker died) so the
        period can be re-triggered. Re-running is safe: already-paid staff are skipped.
        """
        batch = self.get_object()
        updated = PayrollBatch.objects.filter(
            pk=batch.pk, status__in=[BatchStatus.PENDING, BatchStatus.PROCESSING]
        ).update(
            status=BatchStatus.FAILED,
            completed_at=timezone.now(),
            error_log=list(batch.error_log or []) + [
                {"staff_id": None, "error": f"Manually marked as failed by {request.user.username}."}
            ],
        )
        if not updated:
            return Response(
                {"detail": f"Only PENDING or PROCESSING batches can be marked as failed (this one is {batch.status})."},
                status=status.HTTP_409_CONFLICT,
            )
        log_event(request, AuditAction.BATCH_MARKED_FAILED, target=batch, previous_status=batch.status)
        batch.refresh_from_db()
        return Response(PayrollBatchSerializer(batch).data)


_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _pdf_response(request, record: PayrollRecord) -> HttpResponse:
    log_event(
        request, AuditAction.PAYSLIP_DOWNLOADED, target=record,
        staff_id=record.staff.staff_id, period=f"{record.period_year}-{record.period_month:02d}",
        own_payslip=record.staff.user_id == request.user.pk,
    )
    filename = _FILENAME_UNSAFE.sub("_", f"Payslip-{record.staff.staff_id}-{record.period_year}-{record.period_month:02d}.pdf")
    response = HttpResponse(render_payslip_pdf(record), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"  # payslips are confidential
    response["X-Content-Type-Options"] = "nosniff"
    return response


class PayrollRecordViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Payroll records.

    * Payroll managers see every record.
    * Regular staff see ONLY their own - the queryset itself is filtered, so another
      person's record returns 404 (its existence is not revealed).
    """

    serializer_class = PayrollRecordSerializer
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ["period_year", "period_month", "net_pay"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation: no real user
            return PayrollRecord.objects.none()
        qs = PayrollRecord.objects.select_related("staff__user", "staff__department__faculty")
        user = self.request.user
        if not user.is_payroll_manager:
            qs = qs.filter(staff__user=user)
        p = self.request.query_params
        if p.get("year", "").isdigit():
            qs = qs.filter(period_year=p["year"])
        if p.get("month", "").isdigit():
            qs = qs.filter(period_month=p["month"])
        if user.is_payroll_manager:
            if p.get("batch"):
                try:
                    qs = qs.filter(batch_id=uuid.UUID(p["batch"]))
                except ValueError:
                    raise ValidationError({"batch": "Must be a valid batch UUID."})
            if p.get("faculty", "").isdigit():
                qs = qs.filter(staff__department__faculty_id=p["faculty"])
            if p.get("staff_id"):
                qs = qs.filter(staff__staff_id=p["staff_id"].upper())
        return qs

    @extend_schema(responses={(200, "application/pdf"): OpenApiTypes.BINARY})
    @action(detail=True, methods=["get"], url_path="payslip", throttle_classes=[PayslipDownloadThrottle])
    def payslip(self, request, pk=None):
        """Generate and download the PDF payslip for one record."""
        return _pdf_response(request, self.get_object())

    @extend_schema(
        parameters=[
            OpenApiParameter("year", int, required=True),
            OpenApiParameter("month", int, required=True),
        ],
        responses={(200, "application/pdf"): OpenApiTypes.BINARY},
    )
    @action(detail=False, methods=["get"], url_path="my-payslip", throttle_classes=[PayslipDownloadThrottle])
    def my_payslip(self, request):
        """Convenience for staff: download MY payslip for ?year=&month=."""
        year, month = request.query_params.get("year", ""), request.query_params.get("month", "")
        if not (year.isdigit() and month.isdigit() and 1 <= int(month) <= 12):
            raise ValidationError("Provide numeric 'year' and 'month' (1-12).")
        record = (
            PayrollRecord.objects.select_related("staff__user", "staff__department__faculty")
            .filter(staff__user=request.user, period_year=int(year), period_month=int(month))
            .first()
        )
        if record is None:
            raise NotFound("No payslip exists for that period.")
        return _pdf_response(request, record)
