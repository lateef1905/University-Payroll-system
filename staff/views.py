from django.db.models import ProtectedError
from drf_spectacular.utils import extend_schema
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from accounts.permissions import IsPayrollManager, IsPayrollManagerOrReadOnly

from .models import Department, Faculty, StaffProfile
from .serializers import (
    DepartmentSerializer,
    FacultySerializer,
    StaffProfileSelfSerializer,
    StaffProfileSerializer,
)


class ProtectedDeleteMixin:
    """Return 409 (not a 500) when deleting a row still referenced elsewhere."""

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "This record is referenced by other records and cannot be deleted."},
                status=status.HTTP_409_CONFLICT,
            )


class FacultyViewSet(ProtectedDeleteMixin, viewsets.ModelViewSet):
    queryset = Faculty.objects.all()
    serializer_class = FacultySerializer
    permission_classes = [IsPayrollManagerOrReadOnly]
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "code"]


class DepartmentViewSet(ProtectedDeleteMixin, viewsets.ModelViewSet):
    queryset = Department.objects.select_related("faculty")
    serializer_class = DepartmentSerializer
    permission_classes = [IsPayrollManagerOrReadOnly]
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "code"]

    def get_queryset(self):
        qs = super().get_queryset()
        faculty = self.request.query_params.get("faculty")
        return qs.filter(faculty_id=faculty) if faculty and faculty.isdigit() else qs


class StaffProfileViewSet(ProtectedDeleteMixin, viewsets.ModelViewSet):
    """Profile administration. Staff members can only call `me`."""

    queryset = StaffProfile.objects.select_related("user", "department__faculty")
    serializer_class = StaffProfileSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["staff_id", "user__first_name", "user__last_name", "user__email"]
    ordering_fields = ["staff_id", "grade_level", "date_of_employment"]

    def get_permissions(self):
        if self.action == "me":
            return super().get_permissions()  # any authenticated user
        return [IsPayrollManager()]

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        if p.get("faculty", "").isdigit():
            qs = qs.filter(department__faculty_id=p["faculty"])
        if p.get("department", "").isdigit():
            qs = qs.filter(department_id=p["department"])
        if p.get("employment_type") in ("ACADEMIC", "NON_ACADEMIC"):
            qs = qs.filter(employment_type=p["employment_type"])
        if p.get("is_active") in ("true", "false"):
            qs = qs.filter(is_active=p["is_active"] == "true")
        return qs

    @extend_schema(responses=StaffProfileSelfSerializer)
    @action(detail=False, methods=["get"], url_path="me")
    def me(self, request):
        """The caller's own (read-only, account-masked) profile."""
        profile = (
            StaffProfile.objects.select_related("department__faculty", "user").filter(user=request.user).first()
        )
        if profile is None:
            raise NotFound("No staff profile is linked to your account.")
        return Response(StaffProfileSelfSerializer(profile).data)
