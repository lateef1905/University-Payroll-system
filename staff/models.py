"""Faculties, departments and staff profiles."""
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from simple_history.models import HistoricalRecords


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class EmploymentType(models.TextChoices):
    ACADEMIC = "ACADEMIC", "Academic"
    NON_ACADEMIC = "NON_ACADEMIC", "Non-Academic"


class SalaryScaleType(models.TextChoices):
    CONUASS = "CONUASS", "CONUASS (University academic staff)"
    CONPCASS = "CONPCASS", "CONPCASS (Polytechnic/College academic staff)"
    CONTISS = "CONTISS", "CONTISS (Non-academic tertiary staff)"


# Which scales are legal for which employment type.
ALLOWED_SCALES = {
    EmploymentType.ACADEMIC: (SalaryScaleType.CONUASS, SalaryScaleType.CONPCASS),
    EmploymentType.NON_ACADEMIC: (SalaryScaleType.CONTISS,),
}

MAX_GRADE_LEVEL = 17
MAX_STEP = 15


class Faculty(TimeStampedModel):
    name = models.CharField(max_length=150, unique=True)
    code = models.CharField(max_length=10, unique=True, validators=[RegexValidator(r"^[A-Z0-9]{2,10}$", "Use 2-10 uppercase letters/digits.")])
    history = HistoricalRecords()

    class Meta:
        verbose_name_plural = "faculties"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Department(TimeStampedModel):
    faculty = models.ForeignKey(Faculty, on_delete=models.PROTECT, related_name="departments")
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=10, validators=[RegexValidator(r"^[A-Z0-9]{2,10}$", "Use 2-10 uppercase letters/digits.")])
    history = HistoricalRecords()

    class Meta:
        ordering = ["faculty__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["faculty", "name"], name="uniq_department_name_per_faculty"),
            models.UniqueConstraint(fields=["faculty", "code"], name="uniq_department_code_per_faculty"),
        ]

    def __str__(self):
        return f"{self.name} ({self.faculty.code})"


class StaffProfile(TimeStampedModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_profile")
    staff_id = models.CharField(
        max_length=20,
        unique=True,
        validators=[RegexValidator(r"^[A-Z0-9/\-]{3,20}$", "Staff ID: 3-20 chars; uppercase letters, digits, '/' or '-'.")],
    )
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="staff")
    designation = models.CharField(max_length=120, blank=True)
    employment_type = models.CharField(max_length=15, choices=EmploymentType.choices)
    salary_scale = models.CharField(max_length=10, choices=SalaryScaleType.choices)
    grade_level = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(MAX_GRADE_LEVEL)])
    step = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(MAX_STEP)])
    date_of_employment = models.DateField()
    is_active = models.BooleanField(default=True, db_index=True)

    # Payment details (payslip shows the account number masked).
    bank_name = models.CharField(max_length=80, blank=True)
    account_number = models.CharField(max_length=10, blank=True, validators=[RegexValidator(r"^\d{10}$", "NUBAN account numbers are 10 digits.")])

    # Inputs to PAYE / deductions.
    annual_rent_paid = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    cooperative_monthly_dues = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    loan_monthly_repayment = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])

    history = HistoricalRecords()

    class Meta:
        ordering = ["staff_id"]
        indexes = [
            models.Index(fields=["department", "is_active"], name="staff_dept_active_idx"),
            models.Index(fields=["salary_scale", "grade_level", "step"], name="staff_scale_grade_step_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(grade_level__gte=1) & Q(grade_level__lte=MAX_GRADE_LEVEL), name="staff_grade_range"),
            models.CheckConstraint(condition=Q(step__gte=1) & Q(step__lte=MAX_STEP), name="staff_step_range"),
            models.CheckConstraint(
                condition=Q(annual_rent_paid__gte=0) & Q(cooperative_monthly_dues__gte=0) & Q(loan_monthly_repayment__gte=0),
                name="staff_amounts_non_negative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(employment_type="ACADEMIC", salary_scale__in=["CONUASS", "CONPCASS"])
                    | Q(employment_type="NON_ACADEMIC", salary_scale="CONTISS")
                ),
                name="staff_scale_matches_employment_type",
            ),
        ]

    def clean(self):
        super().clean()
        allowed = ALLOWED_SCALES.get(self.employment_type, ())
        if self.salary_scale not in allowed:
            raise ValidationError({"salary_scale": f"{self.employment_type} staff must use one of: {', '.join(allowed)}."})
        SalaryScale = apps.get_model("payroll", "SalaryScale")
        if not SalaryScale.objects.filter(scale=self.salary_scale, grade_level=self.grade_level, step=self.step).exists():
            raise ValidationError(f"No salary scale row exists for {self.salary_scale} GL{self.grade_level} step {self.step}.")

    def __str__(self):
        return f"{self.staff_id} - {self.user.get_full_name() or self.user.username}"
