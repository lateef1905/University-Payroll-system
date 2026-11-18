"""Custom user model carrying the RBAC role."""
from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    SUPER_ADMIN = "SUPER_ADMIN", "Super Administrator"
    BURSARY = "BURSARY", "Bursary Officer / Payroll Manager"
    STAFF = "STAFF", "Regular Staff Member"


class User(AbstractUser):
    """
    Django user + a single RBAC role.

    A single role column (not groups) is deliberate: the three roles are fixed
    by the business rules, and one indexed column is easier to audit than
    group/permission tables.
    """

    email = models.EmailField("email address", unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STAFF, db_index=True)

    REQUIRED_FIELDS = ["email"]

    def save(self, *args, **kwargs):
        # `createsuperuser` sets is_superuser but leaves role at the default.
        if self.is_superuser:
            self.role = Role.SUPER_ADMIN
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    @property
    def is_super_admin(self) -> bool:
        return bool(self.is_active and (self.is_superuser or self.role == Role.SUPER_ADMIN))

    @property
    def is_payroll_manager(self) -> bool:
        """Super admins and bursary officers may run payroll and edit salary data."""
        return bool(self.is_active and (self.is_super_admin or self.role == Role.BURSARY))

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"
