from django.contrib import admin

from .models import Department, Faculty, StaffProfile

admin.site.register(Faculty)
admin.site.register(Department)


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("staff_id", "user", "department", "employment_type", "salary_scale", "grade_level", "step", "is_active")
    list_filter = ("employment_type", "salary_scale", "is_active", "department__faculty")
    search_fields = ("staff_id", "user__username", "user__first_name", "user__last_name")
    raw_id_fields = ("user",)
