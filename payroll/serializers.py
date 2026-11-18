
import copy

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from staff.models import Faculty

from .calculator import PayrollCalculationError, TaxBandSpec, validate_tax_bands
from .maintenance import fail_stale_batches
from .validators import check_allowance_rule, check_tax_band
from .models import (
    AllowanceRule,
    BatchStatus,
    PayrollBatch,
    PayrollRecord,
    SalaryScale,
    TaxBand,
)


def _run_model_check(serializer, attrs, model, check):
    """Apply a shared payroll.validators check to the instance as it WOULD be saved."""
    if serializer.instance is not None:
        candidate = copy.copy(serializer.instance)
    else:
        candidate = model()
    for field, value in attrs.items():
        setattr(candidate, field, value)
    try:
        check(candidate)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(
            exc.message_dict if hasattr(exc, "error_dict") else exc.messages
        )
    return attrs


class SalaryScaleSerializer(serializers.ModelSerializer):
    monthly_basic = serializers.SerializerMethodField()

    class Meta:
        model = SalaryScale
        fields = ["id", "scale", "grade_level", "step", "annual_basic", "monthly_basic"]
        read_only_fields = ["id", "monthly_basic"]

    def get_monthly_basic(self, obj) -> str:
        return f"{(obj.annual_basic / 12):.2f}"

    def validate_annual_basic(self, value):
        if value <= 0:
            raise serializers.ValidationError("Annual basic must be greater than zero.")
        return value

    def validate(self, attrs):
        # Friendly duplicate message instead of a DB IntegrityError.
        get = lambda f: attrs.get(f, getattr(self.instance, f, None))  # noqa: E731
        qs = SalaryScale.objects.filter(scale=get("scale"), grade_level=get("grade_level"), step=get("step"))
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A salary row for this scale, grade level and step already exists.")
        return attrs


class AllowanceRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AllowanceRule
        fields = [
            "id", "allowance_type", "calc_method", "value", "employment_type", "salary_scale",
            "min_grade", "max_grade", "is_active",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        return _run_model_check(self, attrs, AllowanceRule, check_allowance_rule)


class TaxBandSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaxBand
        fields = ["id", "tax_year", "lower_bound", "upper_bound", "rate_percent"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        return _run_model_check(self, attrs, TaxBand, check_tax_band)


class PayrollBatchSerializer(serializers.ModelSerializer):
    faculties = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    initiated_by = serializers.StringRelatedField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = PayrollBatch
        fields = [
            "id", "period_year", "period_month", "faculties", "status", "status_display", "initiated_by",
            "total_staff", "processed_count", "skipped_count", "failed_count",
            "total_gross", "total_deductions", "total_net", "error_log",
            "created_at", "started_at", "completed_at",
        ]
        read_only_fields = fields


CLASH_MESSAGE = "A batch for this period and faculty is already pending or processing."


def _clashing_batch_exists(year, month, faculties) -> bool:
    return PayrollBatch.objects.filter(
        period_year=year, period_month=month,
        status__in=[BatchStatus.PENDING, BatchStatus.PROCESSING],
        faculties__in=faculties,
    ).exists()


class PayrollBatchCreateSerializer(serializers.Serializer):
    """Validates a payroll trigger request and creates + enqueues the batch."""

    period_year = serializers.IntegerField(min_value=2020)
    period_month = serializers.IntegerField(min_value=1, max_value=12)
    faculty_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Faculty.objects.all(), required=False, allow_empty=True,
        help_text="Faculties to pay. Omit/empty and set all_faculties=true to pay everyone.",
    )
    all_faculties = serializers.BooleanField(default=False)

    def validate(self, attrs):
        year, month = attrs["period_year"], attrs["period_month"]
        today = timezone.localdate()
        if (year, month) > (today.year, today.month):
            raise serializers.ValidationError("Payroll cannot be processed for a future month.")

        faculties = list(attrs.get("faculty_ids") or [])
        if attrs.get("all_faculties"):
            if faculties:
                raise serializers.ValidationError("Provide either faculty_ids or all_faculties=true, not both.")
            faculties = list(Faculty.objects.all())
        if not faculties:
            raise serializers.ValidationError("Select at least one faculty (or set all_faculties=true).")
        attrs["faculties"] = faculties

        # Fail fast (400) instead of creating a batch that would immediately FAIL.
        bands = [
            TaxBandSpec(lower=b.lower_bound, upper=b.upper_bound, rate_percent=b.rate_percent)
            for b in TaxBand.objects.filter(tax_year=year)
        ]
        try:
            validate_tax_bands(bands)
        except PayrollCalculationError as exc:
            raise serializers.ValidationError(f"Tax bands for {year} are missing or invalid: {exc}")

        # Batches whose worker died must not block new runs forever.
        fail_stale_batches()
        if _clashing_batch_exists(year, month, faculties):
            raise serializers.ValidationError(CLASH_MESSAGE)
        return attrs

    def create(self, validated_data):
        from .tasks import process_payroll_batch  # local import avoids a circular import at load time

        with transaction.atomic():
            # Serialise concurrent triggers for the same faculties, then re-check the clash
            # under the lock (the check in validate() alone is a check-then-act race).
            # On SQLite select_for_update is a no-op, but SQLite already allows one writer.
            faculty_ids = sorted(f.pk for f in validated_data["faculties"])
            list(Faculty.objects.select_for_update().filter(pk__in=faculty_ids).order_by("pk"))
            if _clashing_batch_exists(validated_data["period_year"], validated_data["period_month"], faculty_ids):
                raise serializers.ValidationError(CLASH_MESSAGE)

            batch = PayrollBatch.objects.create(
                period_year=validated_data["period_year"],
                period_month=validated_data["period_month"],
                initiated_by=self.context["request"].user,
            )
            batch.faculties.set(validated_data["faculties"])

            def _enqueue():
                try:
                    process_payroll_batch.delay(str(batch.pk))
                except Exception as exc:  # noqa: BLE001 - broker down: don't leave a zombie PENDING batch
                    PayrollBatch.objects.filter(pk=batch.pk).update(
                        status=BatchStatus.FAILED,
                        error_log=[{"staff_id": None, "error": f"Could not enqueue task: {exc}"}],
                    )

            # Only enqueue after the batch row is committed, otherwise the worker could miss it.
            transaction.on_commit(_enqueue)
        return batch


class PayrollRecordSerializer(serializers.ModelSerializer):
    staff_id = serializers.CharField(source="staff.staff_id", read_only=True)
    staff_name = serializers.CharField(source="staff.user.get_full_name", read_only=True)
    period = serializers.SerializerMethodField()

    class Meta:
        model = PayrollRecord
        fields = [
            "id", "batch", "staff", "staff_id", "staff_name", "period", "period_year", "period_month",
            "salary_scale", "grade_level", "step",
            "basic_salary", "housing_allowance", "transport_allowance", "research_allowance", "medical_allowance",
            "gross_earnings", "pension_employee", "paye_tax", "cooperative_dues", "loan_repayment",
            "total_deductions", "net_pay", "pension_employer", "annual_taxable_income", "created_at",
        ]
        read_only_fields = fields

    def get_period(self, obj) -> str:
        return f"{obj.period_year}-{obj.period_month:02d}"
