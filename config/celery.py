"""Celery application for the payroll system."""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("uipayroll")
# All Celery settings live in Django settings under the CELERY_ prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
