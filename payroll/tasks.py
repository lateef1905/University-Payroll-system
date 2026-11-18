"""Celery tasks: bulk payroll calculation and report dispatch."""
import logging
from decimal import Decimal

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage, get_connection, send_mass_mail
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from accounts.models import Role, User
from staff.models import StaffProfile

from .calculator import (
    AllowanceSpec,
    PayrollBreakdown,
    PayrollCalculationError,
    PayrollRates,
    TaxBandSpec,
    calculate_payroll,
    validate_tax_bands,
)
from .models import AllowanceRule, BatchStatus, PayrollBatch, PayrollRecord, SalaryScale, TaxBand

logger = logging.getLogger(__name__)
MAX_LOGGED_ERRORS = 500


def _load_reference_data(tax_year: int):
    """Load scales, allowance rules and tax bands ONCE per batch (not per employee)."""
    scales = {(s.scale, s.grade_level, s.step): s.annual_basic for s in SalaryScale.objects.all()}
    rules = [
        AllowanceSpec(
            rule_id=r.pk, allowance_type=r.allowance_type, calc_method=r.calc_method, value=r.value,
            employment_type=r.employment_type, salary_scale=r.salary_scale, min_grade=r.min_grade, max_grade=r.max_grade,
        )
        for r in AllowanceRule.objects.filter(is_active=True)
    ]
    bands = [
        TaxBandSpec(lower=b.lower_bound, upper=b.upper_bound, rate_percent=b.rate_percent)
        for b in TaxBand.objects.filter(tax_year=tax_year)
    ]
    validate_tax_bands(bands)  # fail the whole batch early if the tax table is broken
    return scales, rules, bands


def _build_record(batch: PayrollBatch, staff: StaffProfile, b: PayrollBreakdown) -> PayrollRecord:
    return PayrollRecord(
        batch=batch, staff=staff, period_year=batch.period_year, period_month=batch.period_month,
        salary_scale=staff.salary_scale, grade_level=staff.grade_level, step=staff.step,
        basic_salary=b.basic_salary, housing_allowance=b.housing_allowance, transport_allowance=b.transport_allowance,
        research_allowance=b.research_allowance, medical_allowance=b.medical_allowance, gross_earnings=b.gross_earnings,
        pension_employee=b.pension_employee, paye_tax=b.paye_tax, cooperative_dues=b.cooperative_dues,
        loan_repayment=b.loan_repayment, total_deductions=b.total_deductions, net_pay=b.net_pay,
        pension_employer=b.pension_employer, annual_taxable_income=b.annual_taxable_income,
    )


@shared_task(bind=True, name="payroll.process_payroll_batch")
def process_payroll_batch(self, batch_id: str):
    """
    Calculate and persist payroll for every active staff member in the batch's faculties.

    Safety properties:
      * Claimed atomically (PENDING -> PROCESSING) so a duplicate delivery is a no-op.
      * Idempotent: staff who already have a record for the period are skipped, so a
        re-run (or acks_late redelivery) can never double-pay.
      * One bad employee never aborts the batch; the error is logged on the batch.
      * Records are written in chunked transactions to bound memory and lock time.
    """
    claimed = PayrollBatch.objects.filter(pk=batch_id, status=BatchStatus.PENDING).update(
        status=BatchStatus.PROCESSING, started_at=timezone.now(), celery_task_id=self.request.id or ""
    )
    if not claimed:
        logger.warning("Batch %s was not PENDING; skipping duplicate delivery.", batch_id)
        return "skipped"

    batch = PayrollBatch.objects.get(pk=batch_id)
    errors, failed, skipped, total = [], 0, 0, 0

    try:
        scales, rules, bands = _load_reference_data(batch.period_year)
        rates = PayrollRates.from_settings()
        chunk_size = settings.PAYROLL["BATCH_CHUNK_SIZE"]

        already_paid = set(
            PayrollRecord.objects.filter(period_year=batch.period_year, period_month=batch.period_month)
            .values_list("staff_id", flat=True)
        )
        staff_qs = (
            StaffProfile.objects.filter(
                is_active=True, user__is_active=True, department__faculty__in=batch.faculties.all()
            )
            .select_related("user", "department__faculty")
            .order_by("pk")
        )

        buffer = []

        def flush():
            if buffer:
                with transaction.atomic():
                    # ignore_conflicts guards the unique (staff, period) race with another batch.
                    PayrollRecord.objects.bulk_create(buffer, ignore_conflicts=True)
                buffer.clear()

        for staff in staff_qs.iterator(chunk_size=chunk_size):
            total += 1
            if staff.pk in already_paid:
                skipped += 1
                continue
            try:
                annual_basic = scales[(staff.salary_scale, staff.grade_level, staff.step)]
                breakdown = calculate_payroll(
                    annual_basic=annual_basic,
                    employment_type=staff.employment_type,
                    salary_scale=staff.salary_scale,
                    grade_level=staff.grade_level,
                    annual_rent_paid=staff.annual_rent_paid,
                    cooperative_dues=staff.cooperative_monthly_dues,
                    loan_repayment=staff.loan_monthly_repayment,
                    allowance_rules=rules,
                    tax_bands=bands,
                    rates=rates,
                )
            except KeyError:
                failed += 1
                if len(errors) < MAX_LOGGED_ERRORS:
                    errors.append({"staff_id": staff.staff_id, "error": f"No salary scale row for {staff.salary_scale} GL{staff.grade_level} step {staff.step}."})
                continue
            except PayrollCalculationError as exc:
                failed += 1
                if len(errors) < MAX_LOGGED_ERRORS:
                    errors.append({"staff_id": staff.staff_id, "error": str(exc)})
                continue

            buffer.append(_build_record(batch, staff, breakdown))
            if len(buffer) >= chunk_size:
                flush()
        flush()

        # Totals come from the database, so they are exact even after ignored conflicts.
        agg = PayrollRecord.objects.filter(batch=batch).aggregate(
            n=Count("id"), gross=Sum("gross_earnings"), ded=Sum("total_deductions"), net=Sum("net_pay")
        )
        batch.total_staff = total
        batch.processed_count = agg["n"] or 0
        batch.skipped_count = skipped
        batch.failed_count = failed
        batch.total_gross = agg["gross"] or Decimal("0.00")
        batch.total_deductions = agg["ded"] or Decimal("0.00")
        batch.total_net = agg["net"] or Decimal("0.00")
        batch.error_log = errors
        batch.status = BatchStatus.COMPLETED_WITH_ERRORS if failed else BatchStatus.COMPLETED
    except Exception as exc:  # noqa: BLE001 - last-resort guard; recorded on the batch
        logger.exception("Payroll batch %s failed", batch_id)
        batch.status = BatchStatus.FAILED
        batch.error_log = errors + [{"staff_id": None, "error": f"Batch aborted: {exc}"}]
    finally:
        batch.completed_at = timezone.now()
        batch.save()
        try:
            send_batch_report.delay(str(batch.pk))
        except Exception:  # noqa: BLE001 - a broker hiccup must not mask the batch result
            logger.exception("Could not enqueue report email for batch %s", batch_id)
    return batch.status


@shared_task(name="payroll.send_batch_report")
def send_batch_report(batch_id: str):
    """Email a summary to payroll managers and a 'payslip ready' notice to paid staff."""
    batch = PayrollBatch.objects.get(pk=batch_id)
    period = f"{batch.period_year}-{batch.period_month:02d}"

    managers = list(
        User.objects.filter(is_active=True, role__in=[Role.BURSARY, Role.SUPER_ADMIN]).exclude(email="").values_list("email", flat=True)
    )
    if managers:
        lines = [
            f"Payroll batch {batch.pk} for {period}: {batch.get_status_display()}",
            f"Staff in scope: {batch.total_staff} | paid: {batch.processed_count} | already paid (skipped): {batch.skipped_count} | failed: {batch.failed_count}",
            f"Total gross: {batch.total_gross:,.2f} | deductions: {batch.total_deductions:,.2f} | net: {batch.total_net:,.2f}",
        ]
        if batch.error_log:
            lines.append("")
            lines.append("First errors:")
            lines += [f"- {e.get('staff_id') or 'BATCH'}: {e['error']}" for e in batch.error_log[:20]]
        EmailMessage(
            subject=f"[Payroll] {period} - {batch.get_status_display()}",
            body="\n".join(lines), from_email=settings.DEFAULT_FROM_EMAIL, to=managers,
        ).send(fail_silently=False)

    if settings.PAYROLL["NOTIFY_STAFF_BY_EMAIL"] and batch.processed_count:
        # No amounts in email: only a pointer to the authenticated portal.
        emails = (
            PayrollRecord.objects.filter(batch=batch).exclude(staff__user__email="")
            .values_list("staff__user__email", flat=True)
        )
        subject = f"Your {period} payslip is available"
        body = "Your payslip is now available. Please sign in to the staff portal to view or download it."
        messages = [(subject, body, settings.DEFAULT_FROM_EMAIL, [e]) for e in emails]
        connection = get_connection()
        for i in range(0, len(messages), 100):
            try:
                send_mass_mail(messages[i : i + 100], fail_silently=False, connection=connection)
            except Exception:  # noqa: BLE001 - notification failure must not affect payroll
                logger.exception("Failed sending staff notification chunk %s for batch %s", i, batch_id)
    return "sent"
