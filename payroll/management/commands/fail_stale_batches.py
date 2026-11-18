"""Mark payroll batches whose worker died as FAILED (run from cron or Celery beat)."""
from django.core.management.base import BaseCommand

from payroll.maintenance import fail_stale_batches


class Command(BaseCommand):
    help = "Mark stuck PENDING/PROCESSING payroll batches older than the Celery time limit as FAILED."

    def handle(self, *args, **opts):
        count = fail_stale_batches()
        self.stdout.write(self.style.SUCCESS(f"{count} stale batch(es) marked as FAILED."))
