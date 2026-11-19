from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import AllowanceRule, PayrollBatch, PayrollRecord, SalaryScale, TaxBand

@admin.register(SalaryScale)
class SalaryScaleAdmin(SimpleHistoryAdmin):
    list_display = ("scale", "grade_level", "step", "annual_basic")
    list_filter = ("scale", "grade_level")


@admin.register(AllowanceRule)
class AllowanceRuleAdmin(SimpleHistoryAdmin):
    """Overlap checks run in AllowanceRule.clean() - same rules as the API."""

    list_display = ("allowance_type", "calc_method", "value", "employment_type", "salary_scale", "min_grade", "max_grade", "is_active")
    list_filter = ("allowance_type", "employment_type", "is_active")


@admin.register(TaxBand)
class TaxBandAdmin(SimpleHistoryAdmin):
    """Overlap checks run in TaxBand.clean() - same rules as the API."""

    list_display = ("tax_year", "lower_bound", "upper_bound", "rate_percent")
    list_filter = ("tax_year",)


@admin.register(PayrollBatch)
class PayrollBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "period_year", "period_month", "status", "processed_count", "failed_count", "total_net")
    readonly_fields = [f.name for f in PayrollBatch._meta.fields]

    def has_add_permission(self, request):
        return False  # batches are created via the API so they are enqueued correctly


@admin.register(PayrollRecord)
class PayrollRecordAdmin(admin.ModelAdmin):
    """Read-only: payroll history must never be edited through the admin."""

    list_display = ("staff", "period_year", "period_month", "gross_earnings", "total_deductions", "net_pay")
    list_filter = ("period_year", "period_month")
    search_fields = ("staff__staff_id",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
