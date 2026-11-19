"""API tests: RBAC, batch processing, idempotency and payslip isolation.

Run with:  DJANGO_DEBUG=True DB_ENGINE=django.db.backends.sqlite3 DB_NAME=:memory: python manage.py test
"""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role
from payroll.models import AllowanceRule, PayrollBatch, PayrollRecord, SalaryScale, TaxBand
from staff.models import Department, Faculty, StaffProfile

User = get_user_model()
PW = "Correct-Horse-Battery-9"


def make_staff(username, dept, staff_id, **kw):
    user = User.objects.create_user(username=username, email=f"{username}@x.edu.ng", password=PW, role=Role.STAFF)
    profile = StaffProfile.objects.create(
        user=user, staff_id=staff_id, department=dept, employment_type="ACADEMIC", salary_scale="CONUASS",
        grade_level=4, step=1, date_of_employment=date(2019, 1, 1), **kw,
    )
    return user, profile


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PayrollFixtures(APITestCase):
    """Shared data and helpers: two faculties, three staff, one bursar."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Run Celery tasks inline. (override_settings cannot reach an already-configured Celery app.)
        from config.celery import app

        app.conf.task_always_eager = True
        app.conf.task_eager_propagates = True

    def setUp(self):
        from django.core.cache import cache

        cache.clear()  # reset DRF throttle counters between tests

    @classmethod
    def setUpTestData(cls):
        cls.faculty = Faculty.objects.create(name="Science", code="SCI")
        cls.other_faculty = Faculty.objects.create(name="Arts", code="ART")
        cls.dept = Department.objects.create(faculty=cls.faculty, name="CS", code="CSC")
        cls.other_dept = Department.objects.create(faculty=cls.other_faculty, name="History", code="HIS")
        SalaryScale.objects.create(scale="CONUASS", grade_level=4, step=1, annual_basic=D("2700000.00"))
        AllowanceRule.objects.create(allowance_type="HOUSING", calc_method="PERCENT_OF_BASIC", value=D("25"))
        for lo, hi, r in [(0, 800000, 0), (800000, 3000000, 15), (3000000, 12000000, 18), (12000000, 25000000, 21), (25000000, 50000000, 23), (50000000, None, 25)]:
            TaxBand.objects.create(tax_year=timezone.localdate().year, lower_bound=D(lo), upper_bound=None if hi is None else D(hi), rate_percent=D(r))
        cls.bursar = User.objects.create_user(username="bursar", email="b@x.edu.ng", password=PW, role=Role.BURSARY)
        cls.u1, cls.p1 = make_staff("alice", cls.dept, "UI/001")
        cls.u2, cls.p2 = make_staff("bob", cls.dept, "UI/002")
        cls.u3, cls.p3 = make_staff("carol", cls.other_dept, "UI/003")

    def login(self, username):
        r = self.client.post("/api/auth/login/", {"username": username, "password": PW}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")

    def trigger(self, **payload):
        today = timezone.localdate()
        body = {"period_year": today.year, "period_month": today.month, **payload}
        # The batch is enqueued via transaction.on_commit; TestCase never commits, so run callbacks explicitly.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/payroll/batches/", body, format="json")


class PayrollApiTests(PayrollFixtures):
    # ---- RBAC ----
    def test_anonymous_is_rejected(self):
        self.assertEqual(self.client.get("/api/payroll/records/").status_code, 401)

    def test_staff_cannot_trigger_payroll_or_edit_scales(self):
        self.login("alice")
        self.assertEqual(self.trigger(all_faculties=True).status_code, 403)
        r = self.client.post("/api/payroll/salary-scales/", {"scale": "CONUASS", "grade_level": 5, "step": 1, "annual_basic": "1.00"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get("/api/payroll/salary-scales/").status_code, 403)

    def test_bursar_cannot_manage_users(self):
        self.login("bursar")
        self.assertEqual(self.client.get("/api/auth/users/").status_code, 403)

    # ---- Batch processing ----
    def test_bursar_batch_pays_only_selected_faculties(self):
        self.login("bursar")
        r = self.trigger(faculty_ids=[self.faculty.pk])
        self.assertEqual(r.status_code, 202, r.content)
        batch = PayrollBatch.objects.get(pk=r.data["id"])
        self.assertEqual(batch.status, "COMPLETED")
        self.assertEqual(batch.processed_count, 2)
        self.assertEqual(PayrollRecord.objects.filter(staff=self.p3).count(), 0)
        rec = PayrollRecord.objects.get(staff=self.p1)
        self.assertEqual(rec.net_pay, rec.gross_earnings - rec.total_deductions)

    def test_rerun_is_idempotent(self):
        self.login("bursar")
        self.trigger(all_faculties=True)
        r = self.trigger(all_faculties=True)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(PayrollRecord.objects.count(), 3)
        self.assertEqual(PayrollBatch.objects.get(pk=r.data["id"]).skipped_count, 3)

    def test_future_period_rejected(self):
        self.login("bursar")
        r = self.client.post("/api/payroll/batches/", {"period_year": timezone.localdate().year + 1, "period_month": 1, "all_faculties": True}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_must_select_faculty(self):
        self.login("bursar")
        self.assertEqual(self.trigger().status_code, 400)

    # ---- Payslip isolation ----
    def test_staff_sees_only_own_records_and_payslips(self):
        self.login("bursar")
        self.trigger(all_faculties=True)
        rec_alice = PayrollRecord.objects.get(staff=self.p1)
        rec_bob = PayrollRecord.objects.get(staff=self.p2)

        self.login("alice")
        listing = self.client.get("/api/payroll/records/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([x["id"] for x in listing.data["results"]], [rec_alice.pk])

        own = self.client.get(f"/api/payroll/records/{rec_alice.pk}/payslip/")
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own["Content-Type"], "application/pdf")
        self.assertTrue(own.content.startswith(b"%PDF"))
        self.assertEqual(own["Cache-Control"], "no-store")

        # Another person's payslip: 404, existence not revealed.
        self.assertEqual(self.client.get(f"/api/payroll/records/{rec_bob.pk}/payslip/").status_code, 404)

        today = timezone.localdate()
        mine = self.client.get(f"/api/payroll/records/my-payslip/?year={today.year}&month={today.month}")
        self.assertEqual(mine.status_code, 200)

    def test_records_are_immutable(self):
        self.login("bursar")
        self.trigger(all_faculties=True)
        rec = PayrollRecord.objects.first()
        rec.net_pay = D("1.00")
        with self.assertRaises(PermissionError):
            rec.save()
        with self.assertRaises(PermissionError):
            rec.delete()


class PayslipAcceptHeaderTests(PayrollFixtures):
    """Regression: `Accept: application/pdf` used to get 406 Not Acceptable from DRF content negotiation."""

    def test_payslip_download_accepts_pdf_header(self):
        self.login("bursar")
        self.trigger(all_faculties=True)
        self.login("alice")
        today = timezone.localdate()
        url = f"/api/payroll/records/my-payslip/?year={today.year}&month={today.month}"
        r = self.client.get(url, HTTP_ACCEPT="application/pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        rec = PayrollRecord.objects.get(staff=self.p1)
        r = self.client.get(f"/api/payroll/records/{rec.pk}/payslip/", HTTP_ACCEPT="application/pdf")
        self.assertEqual(r.status_code, 200)

    def test_payslip_errors_stay_json_with_pdf_header(self):
        self.login("alice")
        r = self.client.get("/api/payroll/records/my-payslip/?year=2001&month=1", HTTP_ACCEPT="application/pdf")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r["Content-Type"], "application/json")
        self.assertIn("detail", r.json())


class PayrollStatsTests(PayrollFixtures):
    """Dashboard aggregates (/api/payroll/stats/)."""

    def test_stats_are_manager_only(self):
        self.login("alice")
        self.assertEqual(self.client.get("/api/payroll/stats/").status_code, 403)

    def test_stats_empty_before_any_run(self):
        self.login("bursar")
        r = self.client.get("/api/payroll/stats/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data["period"])
        self.assertEqual(r.data["trend"], [])
        self.assertEqual(r.data["headcount"]["active"], 3)

    def test_stats_totals_match_records(self):
        self.login("bursar")
        self.trigger(all_faculties=True)
        r = self.client.get("/api/payroll/stats/")
        self.assertEqual(r.status_code, 200)
        today = timezone.localdate()
        self.assertEqual(r.data["period"]["label"], f"{today.year}-{today.month:02d}")
        [point] = r.data["trend"]
        recs = PayrollRecord.objects.all()
        self.assertEqual(point["staff"], 3)
        self.assertEqual(D(point["net"]), sum(x.net_pay for x in recs))
        self.assertEqual(sum(f["staff"] for f in r.data["by_faculty"]), 3)
        self.assertEqual(sum(D(f["gross"]) for f in r.data["by_faculty"]), D(point["gross"]))
        self.assertEqual(r.data["batches"]["COMPLETED"], 1)

    def test_stats_rejects_bad_month(self):
        self.login("bursar")
        self.assertEqual(self.client.get("/api/payroll/stats/?year=2026&month=13").status_code, 400)
