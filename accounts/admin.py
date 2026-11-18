from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "role", "is_active", "is_staff")
    list_filter = ("role", "is_active", "is_staff")
    fieldsets = DjangoUserAdmin.fieldsets + (("Payroll role", {"fields": ("role",)}),)
    add_fieldsets = DjangoUserAdmin.add_fieldsets + ((None, {"fields": ("email", "role")}),)
