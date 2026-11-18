from decimal import Decimal

from django.apps import apps
from rest_framework import serializers

from .models import ALLOWED_SCALES, Department, Faculty, StaffProfile


class FacultySerializer(serializers.ModelSerializer):
    class Meta:
        model = Faculty
        fields = ["id", "name", "code"]

    def validate_code(self, value):
        return value.strip().upper()


class DepartmentSerializer(serializers.ModelSerializer):
    faculty_name = serializers.CharField(source="faculty.name", read_only=True)

    class Meta:
        model = Department
        fields = ["id", "faculty", "faculty_name", "name", "code"]

    def validate_code(self, value):
        return value.strip().upper()


class StaffProfileSerializer(serializers.ModelSerializer):
    """Full read/write serializer for payroll managers."""

    full_name = serializers.CharField(source="user.get_full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    faculty_id = serializers.IntegerField(source="department.faculty_id", read_only=True)

    class Meta:
        model = StaffProfile
        fields = [
            "id", "user", "full_name", "email", "staff_id", "department", "department_name", "faculty_id",
            "designation", "employment_type", "salary_scale", "grade_level", "step", "date_of_employment",
            "is_active", "bank_name", "account_number", "annual_rent_paid",
            "cooperative_monthly_dues", "loan_monthly_repayment",
        ]
        read_only_fields = ["id"]

    def validate_staff_id(self, value):
        return value.strip().upper()

    def validate_user(self, user):
        # `user` is OneToOne; give a friendly error instead of an IntegrityError.
        qs = StaffProfile.objects.filter(user=user)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This user already has a staff profile.")
        return user

    def validate(self, attrs):
        def current(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None)

        employment_type = current("employment_type")
        scale = current("salary_scale")
        allowed = ALLOWED_SCALES.get(employment_type, ())
        if scale not in allowed:
            raise serializers.ValidationError(
                {"salary_scale": f"{employment_type} staff must use one of: {', '.join(allowed)}."}
            )

        SalaryScale = apps.get_model("payroll", "SalaryScale")
        grade, step = current("grade_level"), current("step")
        if not SalaryScale.objects.filter(scale=scale, grade_level=grade, step=step).exists():
            raise serializers.ValidationError(
                {"grade_level": f"No salary scale row exists for {scale} GL{grade} step {step}. Load the scale first."}
            )

        for field in ("annual_rent_paid", "cooperative_monthly_dues", "loan_monthly_repayment"):
            value = current(field)
            if value is not None and Decimal(value) < 0:
                raise serializers.ValidationError({field: "Must not be negative."})
        return attrs


class StaffProfileSelfSerializer(serializers.ModelSerializer):
    """Read-only view for a staff member's own profile (account number masked)."""

    full_name = serializers.CharField(source="user.get_full_name", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    faculty_name = serializers.CharField(source="department.faculty.name", read_only=True)
    account_number = serializers.SerializerMethodField()

    class Meta:
        model = StaffProfile
        fields = [
            "staff_id", "full_name", "department_name", "faculty_name", "designation", "employment_type",
            "salary_scale", "grade_level", "step", "date_of_employment", "bank_name", "account_number",
        ]
        read_only_fields = fields

    def get_account_number(self, obj) -> str:
        return f"******{obj.account_number[-4:]}" if obj.account_number else ""
