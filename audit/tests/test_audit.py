"""Audit trail: change history (django-simple-history) + activity log (AuditEvent)."""
from datetime import date, timedelta
from decimal import Decimal as D
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Role
from audit.models import AuditAction, AuditEvent
from payroll.models import AllowanceRule, BatchStatus, PayrollBatch, PayrollRecord, SalaryScale, TaxBand
from staff.models import Department, Faculty, StaffProfile

User = get_user_model()
PW = "Correct-Horse-Battery-9"
BANDS = [(0, 800000, 0), (800000, 3000000, 15), (3000000, 12000000, 18),
         (12000000, 25000000, 21), (25000000, 50000000, 23), (50000000, None, 25)]


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AuditTests(APITestCase):
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
        cls.dept2 = Department.objects.create(faculty=cls.faculty, name="Maths", code="MTH")
        cls.scale = SalaryScale.objects.create(scale="CONUASS", grade_level=4, step=1, annual_basic=D("2700000.00"))
        AllowanceRule.objects.create(allowance_type="HOUSING", calc_method="PERCENT_OF_BASIC", value=D("25"))
        for lo, hi, r in BANDS:
            TaxBand.objects.create(tax_year=cls.today.year, lower_bound=D(lo), upper_bound=None if hi is None else D(hi), rate_percent=D(r))
        cls.root = User.objects.create_superuser(username="root", email="root@x.edu.ng", password=PW)
        cls.bursar = User.objects.create_user(username="bursar", email="b@x.edu.ng", password=PW, role=Role.BURSARY)
        cls.alice = User.objects.create_user(username="alice", email="alice@x.edu.ng", password=PW, role=Role.STAFF)
        cls.alice_profile = StaffProfile.objects.create(
            user=cls.alice, staff_id="UI/0001", department=cls.dept, employment_type="ACADEMIC",
            salary_scale="CONUASS", grade_level=4, step=1, date_of_employment=date(2019, 1, 1),
        )

    def login(self, username):
        r = self.client.post("/api/auth/login/", {"username": username, "password": PW}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")

    def changes(self, **params):
        return self.client.get("/api/audit/changes/", params)

    # ------------------------------------------------------------ change history
    def test_api_edit_records_who_and_what_changed(self):
        self.login("bursar")
        r = self.client.patch(f"/api/payroll/salary-scales/{self.scale.pk}/", {"annual_basic": "2900000.00"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

        latest = self.scale.history.first()
        self.assertEqual(latest.history_type, "~")
        self.assertEqual(latest.history_user, self.bursar)  # JWT user captured, not anonymous

        r = self.changes(model="salaryscale", object_id=self.scale.pk)
        self.assertEqual(r.status_code, 200, r.content)
        entry = r.data["results"][0]
        self.assertEqual(entry["change_type"], "updated")
        self.assertEqual(entry["changed_by"], {"id": self.bursar.pk, "username": "bursar"})
        self.assertEqual(entry["changes"], [{"field": "annual_basic", "old": "2700000.00", "new": "2900000.00"}])
        self.assertEqual(r.data["results"][-1]["change_type"], "created")

    def test_create_and_delete_are_recorded(self):
        self.login("bursar")
        r = self.client.post("/api/staff/faculties/", {"name": "Law", "code": "LAW"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        faculty_id = r.data["id"]
        self.assertEqual(self.client.delete(f"/api/staff/faculties/{faculty_id}/").status_code, 204)

        r = self.changes(model="faculty", object_id=faculty_id)
        deleted, created = r.data["results"]
        self.assertEqual((deleted["change_type"], created["change_type"]), ("deleted", "created"))
        self.assertIn({"field": "code", "old": None, "new": "LAW"}, created["changes"])
        self.assertIn({"field": "code", "old": "LAW", "new": None}, deleted["changes"])
        self.assertEqual(deleted["changed_by"]["username"], "bursar")

    def test_foreign_key_change_shows_ids(self):
        self.login("bursar")
        r = self.client.patch(f"/api/staff/profiles/{self.alice_profile.pk}/", {"department": self.dept2.pk}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        entry = self.changes(model="staffprofile", object_id=self.alice_profile.pk).data["results"][0]
        self.assertEqual(entry["changes"], [{"field": "department", "old": self.dept.pk, "new": self.dept2.pk}])

    def test_admin_edit_records_user(self):
        self.client.force_login(self.root)
        r = self.client.post(f"/admin/payroll/salaryscale/{self.scale.pk}/change/", {
            "scale": "CONUASS", "grade_level": 4, "step": 1, "annual_basic": "3000000.00",
        })
        self.assertEqual(r.status_code, 302, r.content[:500])
        latest = self.scale.history.first()
        self.assertEqual(latest.history_user, self.root)
        self.assertEqual(latest.annual_basic, D("3000000.00"))

    def test_seeded_tax_bands_have_history(self):
        call_command("seed_payroll", tax_year=self.today.year + 1, stdout=StringIO())
        band = TaxBand.objects.filter(tax_year=self.today.year + 1).first()
        self.assertEqual(band.history.count(), 1)

    def test_filters(self):
        self.login("bursar")
        self.client.patch(f"/api/payroll/salary-scales/{self.scale.pk}/", {"annual_basic": "2800000.00"}, format="json")
        self.assertEqual(len(self.changes(model="salaryscale", changed_by=self.bursar.pk).data["results"]), 1)
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        self.assertEqual(len(self.changes(model="salaryscale", since=tomorrow).data["results"]), 0)
        self.assertEqual(len(self.changes(model="salaryscale", until=self.today.isoformat()).data["results"]), 2)

    # ------------------------------------------------------------ user privacy
    def test_password_hash_never_enters_history(self):
        field_names = {f.name for f in User.history.model._meta.fields}
        self.assertNotIn("password", field_names)
        self.assertNotIn("last_login", field_names)

    def test_login_updates_last_login_without_history_row(self):
        before = self.alice.history.count()
        self.login("alice")
        self.alice.refresh_from_db()
        self.assertIsNotNone(self.alice.last_login)
        self.assertEqual(self.alice.history.count(), before)

    def test_password_change_is_an_event_not_a_history_row(self):
        before = self.alice.history.count()
        self.login("alice")
        r = self.client.post("/api/auth/change-password/", {"old_password": PW, "new_password": "Another-Long-Passw0rd"}, format="json")
        self.assertEqual(r.status_code, 204, r.content)
        self.assertEqual(self.alice.history.count(), before)
        event = AuditEvent.objects.get(action=AuditAction.PASSWORD_CHANGED)
        self.assertEqual(event.actor, self.alice)

    def test_role_change_is_in_user_history(self):
        self.login("root")
        self.client.patch(f"/api/auth/users/{self.alice.pk}/", {"role": Role.BURSARY}, format="json")
        entry = self.changes(model="user", object_id=self.alice.pk).data["results"][0]
        self.assertEqual(entry["changes"], [{"field": "role", "old": "STAFF", "new": "BURSARY"}])
        self.assertEqual(entry["changed_by"]["username"], "root")

    # ------------------------------------------------------------ permissions & validation
    def test_change_history_permissions(self):
        self.login("alice")
        self.assertEqual(self.changes(model="salaryscale").status_code, 403)
        self.login("bursar")
        self.assertEqual(self.changes(model="salaryscale").status_code, 200)
        self.assertEqual(self.changes(model="user").status_code, 403)  # user management is super-admin only
        self.login("root")
        self.assertEqual(self.changes(model="user").status_code, 200)

    def test_change_history_validation(self):
        self.login("bursar")
        self.assertEqual(self.changes().status_code, 400)
        self.assertEqual(self.changes(model="payrollrecord").status_code, 400)
        self.assertEqual(self.changes(model="salaryscale", since="yesterday").status_code, 400)
        self.assertEqual(self.changes(model="salaryscale", since="2026-13-45").status_code, 400)
        self.assertEqual(self.changes(model="salaryscale", object_id="abc").status_code, 400)

    def test_activity_log_is_super_admin_only(self):
        self.login("bursar")
        self.assertEqual(self.client.get("/api/audit/events/").status_code, 403)
        self.login("root")
        self.assertEqual(self.client.get("/api/audit/events/").status_code, 200)

    # ------------------------------------------------------------ activity log
    def test_login_success_and_failure_are_logged(self):
        self.client.post("/api/auth/login/", {"username": "alice", "password": "wrong-password"}, format="json")
        self.login("alice")
        failed = AuditEvent.objects.get(action=AuditAction.LOGIN_FAILED)
        self.assertIsNone(failed.actor)
        self.assertEqual(failed.actor_username, "alice")
        self.assertEqual(failed.ip_address, "127.0.0.1")
        ok = AuditEvent.objects.get(action=AuditAction.LOGIN_SUCCESS)
        self.assertEqual(ok.actor, self.alice)

    def test_batch_and_payslip_events(self):
        self.login("bursar")
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post("/api/payroll/batches/", {
                "period_year": self.today.year, "period_month": self.today.month, "all_faculties": True,
            }, format="json")
        self.assertEqual(r.status_code, 202, r.content)
        triggered = AuditEvent.objects.get(action=AuditAction.BATCH_TRIGGERED)
        self.assertEqual((triggered.actor, triggered.target_type, triggered.target_id), (self.bursar, "PayrollBatch", r.data["id"]))
        self.assertEqual(triggered.detail["faculty_ids"], [self.faculty.pk])

        record = PayrollRecord.objects.get(staff=self.alice_profile)
        self.client.get(f"/api/payroll/records/{record.pk}/payslip/")  # manager views alice's payslip
        self.login("alice")
        self.client.get(f"/api/payroll/records/my-payslip/?year={self.today.year}&month={self.today.month}")
        downloads = AuditEvent.objects.filter(action=AuditAction.PAYSLIP_DOWNLOADED).order_by("id")
        self.assertEqual([(e.actor_username, e.detail["own_payslip"]) for e in downloads], [("bursar", False), ("alice", True)])
        self.assertEqual(downloads[0].detail["staff_id"], "UI/0001")

        stuck = PayrollBatch.objects.create(period_year=self.today.year, period_month=self.today.month,
                                            initiated_by=self.bursar, status=BatchStatus.PROCESSING, started_at=timezone.now())
        self.login("bursar")
        self.client.post(f"/api/payroll/batches/{stuck.pk}/mark-failed/")
        marked = AuditEvent.objects.get(action=AuditAction.BATCH_MARKED_FAILED)
        self.assertEqual((marked.target_id, marked.detail["previous_status"]), (str(stuck.pk), "PROCESSING"))

    def test_event_filters(self):
        self.client.post("/api/auth/login/", {"username": "ghost", "password": "nope-nope-nope"}, format="json")
        self.login("root")
        r = self.client.get("/api/audit/events/", {"action": "LOGIN_FAILED", "username": "ghost"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["results"]), 1)
        self.assertEqual(self.client.get("/api/audit/events/", {"action": "NOPE"}).status_code, 400)

    def test_events_are_immutable(self):
        self.login("alice")
        event = AuditEvent.objects.first()
        event.actor_username = "someone-else"
        with self.assertRaises(PermissionError):
            event.save()
        with self.assertRaises(PermissionError):
            event.delete()
