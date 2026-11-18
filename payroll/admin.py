from django.contrib import admin

from .models import AllowanceRule, PayrollBatch, PayrollRecord, SalaryScale, TaxBand

admin.site.register(SalaryScale)
admin.site.register(AllowanceRule)
admin.site.register(TaxBand)


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
