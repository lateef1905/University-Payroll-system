"""Operational helpers for payroll batches."""
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import BatchStatus, PayrollBatch

logger = logging.getLogger(__name__)

# Grace period on top of Celery's hard time limit before a batch counts as abandoned.
STALE_GRACE = timedelta(minutes=5)


def stale_cutoff():
    return timezone.now() - timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT) - STALE_GRACE


def fail_stale_batches() -> int:
    """
    Mark batches whose worker died as FAILED so they stop blocking new runs.

    A batch is stale when it has been PROCESSING (or PENDING, i.e. never picked up)
    for longer than the Celery hard time limit plus a grace period. Re-triggering is
    safe: staff already paid for the period are skipped.
    """
    cutoff = stale_cutoff()
    stale = PayrollBatch.objects.filter(
        Q(status=BatchStatus.PROCESSING, started_at__lt=cutoff)
        | Q(status=BatchStatus.PENDING, created_at__lt=cutoff)
    )
    count = 0
    for batch in stale:
        batch.status = BatchStatus.FAILED
        batch.completed_at = timezone.now()
        batch.error_log = list(batch.error_log or []) + [
            {"staff_id": None, "error": "Worker lost: batch timed out. Safe to re-trigger (already-paid staff are skipped)."}
        ]
        batch.save(update_fields=["status", "completed_at", "error_log"])
        count += 1
    if count:
        logger.warning("Marked %s stale payroll batch(es) as FAILED.", count)
    return count
