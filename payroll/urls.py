from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AllowanceRuleViewSet,
    PayrollBatchViewSet,
    PayrollRecordViewSet,
    PayrollStatsView,
    SalaryScaleViewSet,
    TaxBandViewSet,
)

router = DefaultRouter()
router.register("salary-scales", SalaryScaleViewSet, basename="salary-scale")
router.register("allowance-rules", AllowanceRuleViewSet, basename="allowance-rule")
router.register("tax-bands", TaxBandViewSet, basename="tax-band")
router.register("batches", PayrollBatchViewSet, basename="payroll-batch")
router.register("records", PayrollRecordViewSet, basename="payroll-record")

urlpatterns = [path("stats/", PayrollStatsView.as_view(), name="payroll-stats"), *router.urls]
