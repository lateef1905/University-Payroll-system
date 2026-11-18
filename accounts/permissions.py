"""Reusable DRF permission classes implementing the RBAC rules."""
from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsSuperAdmin(BasePermission):
    message = "Only a Super Administrator may perform this action."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_super_admin)


class IsPayrollManager(BasePermission):
    """Super Administrator or Bursary Officer / Payroll Manager."""

    message = "Only a Super Administrator or Bursary Officer may perform this action."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_payroll_manager)


class IsPayrollManagerOrReadOnly(BasePermission):
    """Any authenticated user may read; only payroll managers may write."""

    message = "Only a Super Administrator or Bursary Officer may modify this resource."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return user.is_payroll_manager
