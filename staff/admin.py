from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Department, Faculty, StaffProfile


@admin.register(Faculty)
class FacultyAdmin(SimpleHistoryAdmin):
    list_display = ("name", "code")
    search_fields = ("name", "code")


@admin.register(Department)
class DepartmentAdmin(SimpleHistoryAdmin):
    list_display = ("name", "code", "faculty")
    list_filter = ("faculty",)
    search_fields = ("name", "code")


@admin.register(StaffProfile)
class StaffProfileAdmin(SimpleHistoryAdmin):
    list_display = ("staff_id", "user", "department", "employment_type", "salary_scale", "grade_level", "step", "is_active")
    list_filter = ("employment_type", "salary_scale", "is_active", "department__faculty")
    search_fields = ("staff_id", "user__username", "user__first_name", "user__last_name")
    raw_id_fields = ("user",)
