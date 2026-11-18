"""
Regression tests for issues found in a code review. Each test name carries a finding ID
(C = critical, H = high, M = medium) and its docstring describes the bug it guards against.

Run with:  DB_ENGINE=django.db.backends.sqlite3 DB_NAME=:memory: python manage.py test
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal as D
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.forms import modelform_factory
from django.test import override_settings
from django.utils import timezone
from rest_framework import serializers
from rest_framework.test import APITestCase

from accounts.models import Role
from payroll import tasks
from payroll.models import AllowanceRule, BatchStatus, PayrollBatch, PayrollRecord, SalaryScale, TaxBand
from payroll.serializers import PayrollBatchCreateSerializer
from payroll.tasks import process_payroll_batch
from staff.models import Department, Faculty, StaffProfile

User = get_user_model()
PW = "Correct-Horse-Battery-9"
NTA_2025_BANDS = [(0, 800000, 0), (800000, 3000000, 15), (3000000, 12000000, 18),
                  (12000000, 25000000, 21), (25000000, 50000000, 23), (50000000, None, 25)]


def add_bands(year):
    for lo, hi, r in NTA_2025_BANDS:
        TaxBand.objects.create(tax_year=year, lower_bound=D(lo), upper_bound=None if hi is None else D(hi), rate_percent=D(r))


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],  # fast test users
)
class ReviewFixTests(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from config.celery import app

        app.conf.task_always_eager = True
        app.conf.task_eager_propagates = True

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.faculty = Faculty.objects.create(name="Science", code="SCI")
        cls.dept = Department.objects.create(faculty=cls.faculty, name="CS", code="CSC")
        # Uneven kobo amounts on purpose: they are what broke float storage on SQLite.
        for grade in range(1, 8):
            for step in range(1, 10):
                SalaryScale.objects.create(
                    scale="CONUASS", grade_level=grade, step=step,
                    annual_basic=D(1_234_567) + D(grade * 211_111) + D(step) * D("13579.37"),
                )
        AllowanceRule.objects.create(allowance_type="HOUSING", calc_method="PERCENT_OF_BASIC", value=D("25"))
        AllowanceRule.objects.create(allowance_type="TRANSPORT", calc_method="PERCENT_OF_BASIC", value=D("15"))
        add_bands(cls.today.year)
        cls.bursar = User.objects.create_user(username="bursar", email="b@x.edu.ng", password=PW, role=Role.BURSARY)
        cls.staff_user = User.objects.create_user(username="alice", email="alice@x.edu.ng", password=PW, role=Role.STAFF)
        cls.alice = cls.make_profile(cls.staff_user, "UI/0001")

    @classmethod
    def make_profile(cls, user, staff_id, grade=4, step=1, **kw):
        defaults = dict(
            department=cls.dept, employment_type="ACADEMIC", salary_scale="CONUASS",
            grade_level=grade, step=step, date_of_employment=date(2019, 1, 1),
        )
        defaults.update(kw)
        return StaffProfile.objects.create(user=user, staff_id=staff_id, **defaults)

    def add_many_staff(self, n):
        users = User.objects.bulk_create([User(username=f"bulk{i}", email=f"bulk{i}@x.edu.ng") for i in range(n)])
        users = User.objects.filter(username__startswith="bulk").order_by("pk")
        StaffProfile.objects.bulk_create([
            StaffProfile(
                user=u, staff_id=f"UI/B{i:04d}", department=self.dept, employment_type="ACADEMIC",
                salary_scale="CONUASS", grade_level=1 + i % 7, step=1 + i % 9, date_of_employment=date(2019, 1, 1),
                annual_rent_paid=D(i * 7919), cooperative_monthly_dues=D(f"{i * 37 % 20000}.{i % 100:02d}"),
                loan_monthly_repayment=D(f"{i * 53 % 30000}.{(i * 7) % 100:02d}"),
            )
            for i, u in enumerate(users)
        ])

    def login(self, username):
        r = self.client.post("/api/auth/login/", {"username": username, "password": PW}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")

    def trigger(self, **payload):
        body = {"period_year": self.today.year, "period_month": self.today.month, "all_faculties": True, **payload}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/payroll/batches/", body, format="json")

    def new_batch(self, **kw):
        defaults = dict(period_year=self.today.year, period_month=self.today.month, initiated_by=self.bursar)
        defaults.update(kw)
        batch = PayrollBatch.objects.create(**defaults)
        batch.faculties.set([self.faculty])
        return batch

    def assertReconciles(self, batch):
        self.assertEqual(
            batch.processed_count + batch.skipped_count + batch.failed_count, batch.total_staff,
            f"batch does not reconcile: {batch.error_log}",
        )
        self.assertEqual(PayrollRecord.objects.filter(batch=batch).count(), batch.processed_count)

    # ------------------------------------------------------------------ Phase 1
    def test_C1_large_batch_pays_everyone(self):
        """C1: on SQLite, ~60% of rows used to vanish while the batch said COMPLETED."""
        self.add_many_staff(300)
        self.login("bursar")
        r = self.trigger()
        self.assertEqual(r.status_code, 202, r.content)
        batch = PayrollBatch.objects.get(pk=r.data["id"])
        self.assertEqual(batch.status, BatchStatus.COMPLETED, batch.error_log)
        self.assertEqual(batch.total_staff, 301)
        self.assertEqual(batch.processed_count, 301)
        self.assertReconciles(batch)

    def test_C1_M1_db_rejected_row_is_reported_not_dropped(self):
        """C1/M1: a row the database rejects is counted as failed, and the rest of the chunk is saved."""
        self.add_many_staff(5)
        victim = StaffProfile.objects.get(staff_id="UI/B0002")
        real_build = tasks._build_record

        def corrupt_one(batch, staff, breakdown):
            record = real_build(batch, staff, breakdown)
            if staff.pk == victim.pk:
                record.net_pay += D("100.00")  # violates net = gross - deductions
            return record

        self.login("bursar")
        with mock.patch.object(tasks, "_build_record", side_effect=corrupt_one):
            r = self.trigger()
        batch = PayrollBatch.objects.get(pk=r.data["id"])
        self.assertEqual(batch.status, BatchStatus.COMPLETED_WITH_ERRORS)
        self.assertEqual(batch.failed_count, 1)
        self.assertEqual(batch.processed_count, 5)
        self.assertIn("UI/B0002", [e["staff_id"] for e in batch.error_log])
        self.assertFalse(PayrollRecord.objects.filter(staff=victim).exists())
        self.assertReconciles(batch)

    def test_M1_record_paid_by_another_batch_during_run_counts_as_skipped(self):
        """M1: a unique-constraint race with another batch is counted as skipped, not lost."""
        self.login("bursar")
        first = PayrollBatch.objects.get(pk=self.trigger().data["id"])
        self.assertEqual(first.processed_count, 1)

        # Pretend alice was not yet paid when the second batch looked, so it tries to insert again
        # and hits the unique (staff, period) constraint.
        batch = self.new_batch()
        real_filter = PayrollRecord.objects.filter

        def hide_existing(*args, **kwargs):
            qs = real_filter(*args, **kwargs)
            return qs.none() if set(kwargs) == {"period_year", "period_month"} else qs

        with mock.patch.object(PayrollRecord.objects, "filter", side_effect=hide_existing):
            process_payroll_batch.apply(args=[str(batch.pk)])
        batch.refresh_from_db()
        self.assertEqual((batch.processed_count, batch.skipped_count, batch.failed_count), (0, 1, 0))
        self.assertEqual(batch.status, BatchStatus.COMPLETED)

    # ------------------------------------------------------------------ Phase 2
    def test_H1_redelivery_with_same_task_id_resumes(self):
        """H1: acks_late re-delivery keeps the task id and must resume, not skip."""
        batch = self.new_batch(status=BatchStatus.PROCESSING, celery_task_id="task-123", started_at=timezone.now())
        result = process_payroll_batch.apply(args=[str(batch.pk)], task_id="task-123").get()
        self.assertEqual(result, BatchStatus.COMPLETED)
        batch.refresh_from_db()
        self.assertEqual(batch.processed_count, 1)

    def test_H1_resume_counts_records_from_the_interrupted_attempt(self):
        self.add_many_staff(4)
        earlier = self.new_batch()
        process_payroll_batch.apply(args=[str(earlier.pk)])  # pays alice + 4 bulk staff
        # Simulate a crash of `batch` right after alice's record was written by it.
        batch = self.new_batch(status=BatchStatus.PROCESSING, celery_task_id="task-1", started_at=timezone.now())
        PayrollRecord.objects.filter(staff=self.alice).update(batch=batch)

        process_payroll_batch.apply(args=[str(batch.pk)], task_id="task-1")
        batch.refresh_from_db()
        # alice (from the interrupted attempt) is processed by this batch; the bulk staff
        # were already paid by the warm-up batch, so they are skipped.
        self.assertEqual(batch.processed_count, 1)
        self.assertEqual(batch.skipped_count, 4)
        self.assertReconciles(batch)

    def test_H1_other_duplicate_delivery_is_noop(self):
        batch = self.new_batch(status=BatchStatus.PROCESSING, celery_task_id="task-123", started_at=timezone.now())
        self.assertEqual(process_payroll_batch.apply(args=[str(batch.pk)], task_id="other").get(), "skipped")
        batch.refresh_from_db()
        self.assertEqual(batch.status, BatchStatus.PROCESSING)

    def test_H1_stale_batch_is_failed_and_stops_blocking(self):
        stale = self.new_batch(status=BatchStatus.PROCESSING, started_at=timezone.now() - timedelta(days=1))
        self.login("bursar")
        r = self.trigger()
        self.assertEqual(r.status_code, 202, r.content)
        stale.refresh_from_db()
        self.assertEqual(stale.status, BatchStatus.FAILED)
        self.assertIn("Worker lost", stale.error_log[-1]["error"])

    def test_H1_running_batch_still_blocks(self):
        self.new_batch(status=BatchStatus.PROCESSING, started_at=timezone.now())
        self.login("bursar")
        self.assertEqual(self.trigger().status_code, 400)

    def test_H1_fail_stale_batches_command(self):
        self.new_batch(status=BatchStatus.PROCESSING, started_at=timezone.now() - timedelta(days=1))
        out = StringIO()
        call_command("fail_stale_batches", stdout=out)
        self.assertIn("1 stale batch", out.getvalue())

    def test_H1_mark_failed_endpoint(self):
        batch = self.new_batch(status=BatchStatus.PROCESSING, started_at=timezone.now())
        url = f"/api/payroll/batches/{batch.pk}/mark-failed/"
        self.login("alice")
        self.assertEqual(self.client.post(url).status_code, 403)
        self.login("bursar")
        r = self.client.post(url)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], BatchStatus.FAILED)
        self.assertIn("bursar", r.data["error_log"][-1]["error"])
        self.assertEqual(self.client.post(url).status_code, 409)  # already FAILED

    def test_H2_invalid_batch_filter_is_400(self):
        self.login("bursar")
        self.assertEqual(self.client.get("/api/payroll/records/?batch=not-a-uuid").status_code, 400)
        batch = self.new_batch()
        self.assertEqual(self.client.get(f"/api/payroll/records/?batch={batch.pk}").status_code, 200)

    def test_M2_manager_email_failure_still_notifies_staff(self):
        self.login("bursar")
        with mock.patch.object(tasks.EmailMessage, "send", side_effect=OSError("SMTP down")):
            self.trigger()
        self.assertEqual([m.to for m in mail.outbox], [["alice@x.edu.ng"]])

    def test_M4_missing_tax_bands_rejected_up_front(self):
        self.login("bursar")
        r = self.trigger(period_year=self.today.year - 1, period_month=12)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Tax bands", str(r.data))
        self.assertFalse(PayrollBatch.objects.exists())

    def test_M4_seed_tax_year_copies_latest_bands(self):
        year = self.today.year + 1
        call_command("seed_payroll", tax_year=year, stdout=StringIO())
        self.assertEqual(TaxBand.objects.filter(tax_year=year).count(), 6)
        call_command("seed_payroll", tax_year=year, stdout=StringIO())  # idempotent
        self.assertEqual(TaxBand.objects.filter(tax_year=year).count(), 6)

    def test_M5_staff_starting_after_period_not_paid(self):
        last_day = calendar.monthrange(self.today.year, self.today.month)[1]
        later = User.objects.create_user(username="newbie", email="n@x.edu.ng", password=PW)
        newbie = self.make_profile(later, "UI/0002", date_of_employment=date(self.today.year, self.today.month, last_day) + timedelta(days=1))
        self.login("bursar")
        batch = PayrollBatch.objects.get(pk=self.trigger().data["id"])
        self.assertFalse(PayrollRecord.objects.filter(staff=newbie).exists())
        self.assertEqual(batch.total_staff, 1)
        self.assertReconciles(batch)

    def test_M6_clash_rechecked_inside_create(self):
        """M6: a batch created between validate() and save() is caught under the lock."""
        serializer = PayrollBatchCreateSerializer(
            data={"period_year": self.today.year, "period_month": self.today.month, "all_faculties": True},
            context={"request": SimpleNamespace(user=self.bursar)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.new_batch(status=BatchStatus.PENDING)  # the concurrent request wins the race
        with self.assertRaises(serializers.ValidationError):
            serializer.save()

    # ------------------------------------------------------------------ Phase 3
    def test_H3_admin_form_rejects_overlapping_allowance_rule(self):
        form = modelform_factory(AllowanceRule, fields="__all__")(data={
            "allowance_type": "HOUSING", "calc_method": "FIXED_MONTHLY", "value": "1000",
            "employment_type": "ACADEMIC", "min_grade": 1, "max_grade": 17, "is_active": True,
        })
        self.assertFalse(form.is_valid())
        self.assertIn("overlaps", str(form.errors))

    def test_H3_admin_form_rejects_overlapping_tax_band(self):
        form = modelform_factory(TaxBand, fields="__all__")(data={
            "tax_year": self.today.year, "lower_bound": "1000000", "upper_bound": "2000000", "rate_percent": "10",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("overlaps", str(form.errors))

    def test_H3_api_rules_unchanged(self):
        self.login("bursar")
        base = "/api/payroll/allowance-rules/"
        overlap = {"allowance_type": "HOUSING", "calc_method": "FIXED_MONTHLY", "value": "1000"}
        self.assertEqual(self.client.post(base, overlap, format="json").status_code, 400)
        ok = {"allowance_type": "MEDICAL", "calc_method": "FIXED_MONTHLY", "value": "5000"}
        r = self.client.post(base, ok, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        # Editing a rule must not clash with itself.
        self.assertEqual(self.client.patch(f"{base}{r.data['id']}/", {"value": "6000"}, format="json").status_code, 200)
        too_much = {"allowance_type": "RESEARCH", "calc_method": "PERCENT_OF_BASIC", "value": "150"}
        self.assertEqual(self.client.post(base, too_much, format="json").status_code, 400)
        band = {"tax_year": self.today.year + 5, "lower_bound": "0", "upper_bound": "100", "rate_percent": "0"}
        self.assertEqual(self.client.post("/api/payroll/tax-bands/", band, format="json").status_code, 201)
        self.assertEqual(self.client.post("/api/payroll/tax-bands/", band, format="json").status_code, 400)

    def test_M3_superuser_role_change_rejected(self):
        root = User.objects.create_superuser(username="root", email="root@x.edu.ng", password=PW)
        self.login("root")
        r = self.client.patch(f"/api/auth/users/{root.pk}/", {"role": Role.STAFF}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("superuser", str(r.data))
        r = self.client.patch(f"/api/auth/users/{self.bursar.pk}/", {"role": Role.STAFF}, format="json")
        self.assertEqual(r.status_code, 200)
