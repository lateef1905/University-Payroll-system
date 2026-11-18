"""Salary structure, tax tables, payroll batches and immutable payroll records."""
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q

from staff.models import (
    MAX_GRADE_LEVEL,
    MAX_STEP,
    EmploymentType,
    Faculty,
    SalaryScaleType,
    StaffProfile,
    TimeStampedModel,
)

MONEY = dict(max_digits=14, decimal_places=2)
ZERO = Decimal("0.00")


class SalaryScale(TimeStampedModel):
    """Annual basic salary for (scale, grade level, step) - CONUASS / CONTISS / CONPCASS."""

    scale = models.CharField(max_length=10, choices=SalaryScaleType.choices)
    grade_level = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(MAX_GRADE_LEVEL)])
    step = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(MAX_STEP)])
    annual_basic = models.DecimalField(validators=[MinValueValidator(Decimal("0.01"))], **MONEY)

    class Meta:
        ordering = ["scale", "grade_level", "step"]
        constraints = [
            # The unique constraint also creates the (scale, grade_level, step) lookup index.
            models.UniqueConstraint(fields=["scale", "grade_level", "step"], name="uniq_scale_grade_step"),
            models.CheckConstraint(condition=Q(annual_basic__gt=0), name="salaryscale_basic_positive"),
            models.CheckConstraint(condition=Q(grade_level__gte=1) & Q(grade_level__lte=MAX_GRADE_LEVEL), name="salaryscale_grade_range"),
            models.CheckConstraint(condition=Q(step__gte=1) & Q(step__lte=MAX_STEP), name="salaryscale_step_range"),
        ]

    def __str__(self):
        return f"{self.scale} GL{self.grade_level}/{self.step}: {self.annual_basic}"


class AllowanceType(models.TextChoices):
    HOUSING = "HOUSING", "Housing"
    TRANSPORT = "TRANSPORT", "Transport"
    RESEARCH = "RESEARCH", "Research"
    MEDICAL = "MEDICAL", "Medical"


class AllowanceMethod(models.TextChoices):
    PERCENT_OF_BASIC = "PERCENT_OF_BASIC", "Percentage of monthly basic"
    FIXED_MONTHLY = "FIXED_MONTHLY", "Fixed monthly amount"


class AllowanceRule(TimeStampedModel):
    """Configurable allowance. At most one active rule may match a staff member per type."""

    allowance_type = models.CharField(max_length=10, choices=AllowanceType.choices)
    calc_method = models.CharField(max_length=20, choices=AllowanceMethod.choices)
    # Percentage (0-100) when PERCENT_OF_BASIC, otherwise naira per month.
    value = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))])
    employment_type = models.CharField(max_length=15, choices=[("ALL", "All staff")] + list(EmploymentType.choices), default="ALL")
    salary_scale = models.CharField(max_length=10, choices=SalaryScaleType.choices, null=True, blank=True)
    min_grade = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(MAX_GRADE_LEVEL)])
    max_grade = models.PositiveSmallIntegerField(default=MAX_GRADE_LEVEL, validators=[MinValueValidator(1), MaxValueValidator(MAX_GRADE_LEVEL)])
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["allowance_type", "employment_type", "min_grade"]
        constraints = [
            models.CheckConstraint(condition=Q(min_grade__lte=F("max_grade")), name="allowance_grade_order"),
            models.CheckConstraint(
                condition=Q(calc_method="FIXED_MONTHLY") | Q(value__lte=100),
                name="allowance_percent_max_100",
            ),
        ]
        indexes = [models.Index(fields=["allowance_type", "is_active"], name="allowance_type_active_idx")]

    def __str__(self):
        unit = "%" if self.calc_method == AllowanceMethod.PERCENT_OF_BASIC else " NGN/month"
        return f"{self.allowance_type} {self.value}{unit} [{self.employment_type}]"


class TaxBand(TimeStampedModel):
    """One slice of the annual PAYE ladder for a tax year. `upper_bound` NULL = open-ended top band."""

    tax_year = models.PositiveSmallIntegerField(db_index=True)
    lower_bound = models.DecimalField(validators=[MinValueValidator(Decimal("0.00"))], **MONEY)
    upper_bound = models.DecimalField(null=True, blank=True, **MONEY)
    rate_percent = models.DecimalField(max_digits=5, decimal_places=2, validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))])

    class Meta:
        ordering = ["tax_year", "lower_bound"]
        constraints = [
            models.UniqueConstraint(fields=["tax_year", "lower_bound"], name="uniq_taxband_year_lower"),
            models.CheckConstraint(condition=Q(upper_bound__isnull=True) | Q(upper_bound__gt=F("lower_bound")), name="taxband_upper_gt_lower"),
            models.CheckConstraint(condition=Q(rate_percent__gte=0) & Q(rate_percent__lte=100), name="taxband_rate_range"),
        ]

    def __str__(self):
        return f"{self.tax_year}: {self.lower_bound}-{self.upper_bound or 'above'} @ {self.rate_percent}%"


class BatchStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    COMPLETED = "COMPLETED", "Completed"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS", "Completed with errors"
    FAILED = "FAILED", "Failed"


class PayrollBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    period_year = models.PositiveSmallIntegerField()
    period_month = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    faculties = models.ManyToManyField(Faculty, related_name="payroll_batches")
    status = models.CharField(max_length=25, choices=BatchStatus.choices, default=BatchStatus.PENDING, db_index=True)
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payroll_batches")
    celery_task_id = models.CharField(max_length=64, blank=True)

    total_staff = models.PositiveIntegerField(default=0)
    processed_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)  # already paid for this period
    failed_count = models.PositiveIntegerField(default=0)
    total_gross = models.DecimalField(max_digits=18, decimal_places=2, default=ZERO)
    total_deductions = models.DecimalField(max_digits=18, decimal_places=2, default=ZERO)
    total_net = models.DecimalField(max_digits=18, decimal_places=2, default=ZERO)
    error_log = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "payroll batches"
        constraints = [models.CheckConstraint(condition=Q(period_month__gte=1) & Q(period_month__lte=12), name="batch_month_range")]
        indexes = [models.Index(fields=["period_year", "period_month"], name="batch_period_idx")]

    def __str__(self):
        return f"Batch {self.period_year}-{self.period_month:02d} [{self.status}]"


class PayrollRecord(models.Model):
    """
    One staff member's pay for one month. Money columns are a frozen snapshot so
    later salary-scale or rule changes never rewrite history. Records are
    append-only: save() on an existing row and delete() are blocked.
    """

    batch = models.ForeignKey(PayrollBatch, on_delete=models.PROTECT, related_name="records")
    staff = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="payroll_records")
    period_year = models.PositiveSmallIntegerField()
    period_month = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])

    # Snapshot of grading at time of payment
    salary_scale = models.CharField(max_length=10, choices=SalaryScaleType.choices)
    grade_level = models.PositiveSmallIntegerField()
    step = models.PositiveSmallIntegerField()

    # Earnings
    basic_salary = models.DecimalField(**MONEY)
    housing_allowance = models.DecimalField(default=ZERO, **MONEY)
    transport_allowance = models.DecimalField(default=ZERO, **MONEY)
    research_allowance = models.DecimalField(default=ZERO, **MONEY)
    medical_allowance = models.DecimalField(default=ZERO, **MONEY)
    gross_earnings = models.DecimalField(**MONEY)

    # Deductions
    pension_employee = models.DecimalField(default=ZERO, **MONEY)
    paye_tax = models.DecimalField(default=ZERO, **MONEY)
    cooperative_dues = models.DecimalField(default=ZERO, **MONEY)
    loan_repayment = models.DecimalField(default=ZERO, **MONEY)
    total_deductions = models.DecimalField(**MONEY)

    net_pay = models.DecimalField(**MONEY)

    # Informational (not deducted from the employee)
    pension_employer = models.DecimalField(default=ZERO, **MONEY)
    annual_taxable_income = models.DecimalField(default=ZERO, **MONEY)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_year", "-period_month", "staff__staff_id"]
        constraints = [
            models.UniqueConstraint(fields=["staff", "period_year", "period_month"], name="uniq_record_staff_period"),
            models.CheckConstraint(condition=Q(period_month__gte=1) & Q(period_month__lte=12), name="record_month_range"),
            models.CheckConstraint(
                condition=Q(
                    gross_earnings=F("basic_salary") + F("housing_allowance") + F("transport_allowance")
                    + F("research_allowance") + F("medical_allowance")
                ),
                name="record_gross_is_sum_of_earnings",
            ),
            models.CheckConstraint(
                condition=Q(
                    total_deductions=F("pension_employee") + F("paye_tax") + F("cooperative_dues") + F("loan_repayment")
                ),
                name="record_deductions_is_sum",
            ),
            models.CheckConstraint(condition=Q(net_pay=F("gross_earnings") - F("total_deductions")), name="record_net_equals_gross_minus_deductions"),
            models.CheckConstraint(condition=Q(net_pay__gte=0), name="record_net_non_negative"),
        ]
        indexes = [
            models.Index(fields=["period_year", "period_month"], name="record_period_idx"),
            models.Index(fields=["staff", "-period_year", "-period_month"], name="record_staff_period_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError("Payroll records are immutable once created.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Payroll records cannot be deleted.")

    def __str__(self):
        return f"{self.staff.staff_id} {self.period_year}-{self.period_month:02d}: {self.net_pay}"
