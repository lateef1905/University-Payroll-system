"""
Populate a FRESH development database with a realistic, internally consistent university:

    python manage.py migrate
    python manage.py seed_demo_data --password 'Some-Long-Demo-Pass-2026'

What it creates (deterministic for a given --random-seed):
  * reference data via `seed_payroll` (faculties, ILLUSTRATIVE salary scales, allowances, PAYE bands)
  * a Central Administration unit and ~80 departments across the faculties
  * ~520 staff (academic on CONUASS, non-academic on CONTISS) with grade-appropriate
    designations, years of service, banks, rent, cooperative dues and loans
  * demo logins: admin.demo (Super Admin), bursar.demo + payroll.officer (Bursary), staff.demo (Staff)
  * monthly payroll from --from to --through (default: January to last month), run through the
    real batch engine, with the events a real year has: a split run, a data-entry error that
    is corrected and re-run, mid-year hires, retirements and October promotions
  * backdated change history and activity log (logins, batch triggers, payslip downloads)

The current month is deliberately left unpaid so it can be run from the dashboard.
Every account shares --password (hashed once). Never run this against a real database.
"""
import calendar
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from simple_history.utils import bulk_create_with_history

from accounts.models import Role
from audit.models import AuditAction, AuditEvent
from payroll.models import PayrollBatch, PayrollRecord, SalaryScale, TaxBand
from payroll.tasks import process_payroll_batch
from staff.models import Department, Faculty, StaffProfile

User = get_user_model()
TWO = Decimal("0.01")

# --------------------------------------------------------------------------- reference
DEPARTMENTS = {
    "ART": [("English", "ENG"), ("History", "HIS"), ("Philosophy", "PHI"), ("Linguistics and African Languages", "LAL"),
            ("Theatre Arts", "THA"), ("Religious Studies", "REL"), ("European Studies", "EUS"), ("Classics", "CLA")],
    "SCI": [("Mathematics", "MAT"), ("Physics", "PHY"), ("Chemistry", "CHM"),
            ("Botany", "BOT"), ("Zoology", "ZOO"), ("Microbiology", "MCB"), ("Geology", "GLY"), ("Statistics", "STA")],
    "EDU": [("Adult Education", "ADE"), ("Educational Management", "EMA"), ("Guidance and Counselling", "GCE"),
            ("Human Kinetics", "HKE"), ("Library, Archival and Information Studies", "LAR"), ("Teacher Education", "TEE")],
    # Faculty of Computing (est. 2025): departments as listed on computing.ui.edu.ng.
    "CMP": [("Computer Science and Artificial Intelligence", "CSC"), ("Data Science", "DSC"),
            ("Information and Communication Technology", "ICT"), ("Software Engineering", "SEN")],
    "SOC": [("Economics", "ECO"), ("Geography", "GEO"), ("Political Science", "POL"), ("Psychology", "PSY"),
            ("Sociology", "SOC"), ("Urban and Regional Planning", "URP")],
    "LAW": [("Commercial and Industrial Law", "CIL"), ("Jurisprudence and International Law", "JIL"),
            ("Private and Property Law", "PPL"), ("Public Law", "PUL")],
    "TEC": [("Civil Engineering", "CVE"), ("Electrical and Electronic Engineering", "EEE"), ("Mechanical Engineering", "MEE"),
            ("Industrial and Production Engineering", "IPE"), ("Petroleum Engineering", "PEE"), ("Food Technology", "FDT"),
            ("Agricultural and Environmental Engineering", "AEE")],
    "AGR": [("Agricultural Economics", "AEC"), ("Agronomy", "AGY"), ("Animal Science", "ANS"),
            ("Crop Protection and Environmental Biology", "CPE"), ("Soil Resources Management", "SRM")],
    "PHA": [("Pharmaceutics and Industrial Pharmacy", "PIP"), ("Pharmacognosy", "PCG"), ("Pharmaceutical Chemistry", "PCH"),
            ("Clinical Pharmacy and Pharmacy Administration", "CPA")],
    "VET": [("Veterinary Anatomy", "VAN"), ("Veterinary Medicine", "VME"), ("Veterinary Surgery and Radiology", "VSR"),
            ("Veterinary Pathology", "VPA"), ("Veterinary Public Health", "VPH")],
    "BMS": [("Anatomy", "ANA"), ("Biochemistry", "BCH"), ("Physiology", "PSL"), ("Pharmacology and Therapeutics", "PHT")],
    "CLS": [("Medicine", "MED"), ("Surgery", "SUR"), ("Paediatrics", "PAE"), ("Obstetrics and Gynaecology", "OBG"),
            ("Radiology", "RAD"), ("Psychiatry", "PSC")],
    "PUH": [("Epidemiology and Medical Statistics", "EMS"), ("Health Promotion and Education", "HPE"),
            ("Environmental Health Sciences", "EHS"), ("Health Policy and Management", "HPM")],
    "DEN": [("Oral and Maxillofacial Surgery", "OMS"), ("Restorative Dentistry", "RDE"), ("Child Oral Health", "COH")],
    "MDS": [("Institute of African Studies", "IAS"), ("Peace and Conflict Studies", "PCS"), ("Sustainability Studies", "SUS")],
    "ADM": [("Registry", "REG"), ("Bursary", "BUR"), ("University Library", "LIB"), ("Works and Services", "WKS"),
            ("Health Services", "HSV"), ("Security Unit", "SEC"), ("Information Technology and Media Services", "ITM")],
}
ADMIN_UNIT = ("Central Administration", "ADM")
MEDICAL_COLLEGE = ["BMS", "CLS", "PUH", "DEN"]

FIRST_NAMES = [
    # Yoruba (most common in Ibadan), Igbo, Hausa/Fulani, and others
    "Adebayo", "Oluwaseun", "Folake", "Babatunde", "Yetunde", "Olumide", "Titilayo", "Adewale", "Funmilayo", "Kehinde",
    "Taiwo", "Olufemi", "Bukola", "Gbenga", "Morenike", "Ayodeji", "Omolara", "Segun", "Abimbola", "Tolulope",
    "Opeyemi", "Damilola", "Temitope", "Ifeoluwa", "Oyinkansola", "Kayode", "Modupe", "Rotimi", "Adunni", "Wale",
    "Chukwuemeka", "Ngozi", "Ifeanyi", "Chiamaka", "Obinna", "Adaeze", "Uchenna", "Nneka", "Chidi", "Ebere",
    "Ibrahim", "Aisha", "Musa", "Hadiza", "Abubakar", "Zainab", "Usman", "Fatima", "Sadiq", "Halima",
    "Efe", "Ese", "Tamuno", "Ekaette", "Bassey", "Ofonime", "Nkechi", "Emeka", "Lateef", "Rasheedat",
]
LAST_NAMES = [
    "Adeyemi", "Ogunleye", "Okafor", "Bello", "Adebayo", "Olaniyan", "Akinola", "Afolabi", "Oyelaran", "Ogundipe",
    "Adeleke", "Balogun", "Ajayi", "Oladipo", "Fashola", "Ogunbiyi", "Olatunji", "Adesina", "Oyewole", "Akintola",
    "Babalola", "Odunsi", "Lawal", "Salami", "Oyebanji", "Alabi", "Ilori", "Oyedepo", "Adeniran", "Ojo",
    "Okonkwo", "Eze", "Nwosu", "Okeke", "Obi", "Uzor", "Chukwu", "Onyekachi", "Nwachukwu", "Ibe",
    "Abdullahi", "Mohammed", "Yusuf", "Suleiman", "Danjuma", "Garba", "Aliyu", "Ibrahim",
    "Etim", "Edet", "Okon", "Ekpo", "Ighodaro", "Omoregie", "Briggs", "Tonye", "Akpan", "Oghene",
]
BANKS = [
    ("First Bank of Nigeria", 14), ("Guaranty Trust Bank", 14), ("Access Bank", 13), ("Zenith Bank", 12),
    ("United Bank for Africa", 11), ("Wema Bank", 8), ("Fidelity Bank", 6), ("Stanbic IBTC Bank", 5),
    ("Union Bank", 5), ("First City Monument Bank", 4), ("Ecobank Nigeria", 4), ("Polaris Bank", 2), ("Sterling Bank", 2),
]

CONUASS_TITLES = {1: "Graduate Assistant", 2: "Assistant Lecturer", 3: "Lecturer II", 4: "Lecturer I",
                  5: "Senior Lecturer", 6: "Reader", 7: "Professor"}
CONUASS_WEIGHTS = {1: 6, 2: 12, 3: 20, 4: 22, 5: 18, 6: 8, 7: 14}
CONUASS_YEARS = {1: 1, 2: 3, 3: 6, 4: 9, 5: 13, 6: 17, 7: 22}

# grade -> (designations, placement). Placement: a unit code, "FAC" (any academic department) or "MIX".
CONTISS_ROLES = {
    1: (["Cleaner", "Messenger", "Gardener"], "MIX"),
    2: (["Porter", "Security Guard", "Gardener"], "MIX"),
    3: (["Driver", "Security Guard", "Laboratory Attendant", "Store Attendant"], "MIX"),
    4: (["Clerical Assistant", "Library Assistant", "Driver Mechanic"], "MIX"),
    5: (["Clerical Officer", "Laboratory Assistant", "Typist"], "MIX"),
    6: (["Senior Clerical Officer", "Secretarial Assistant", "Technical Officer"], "MIX"),
    7: (["Executive Officer", "Technologist II", "Library Officer", "Accountant II"], "MIX"),
    8: (["Assistant Registrar", "Technologist I", "Accountant I", "Librarian II", "Computer Programmer"], "MIX"),
    9: (["Senior Assistant Registrar", "Senior Technologist", "Senior Accountant", "Systems Analyst"], "MIX"),
    10: (["Principal Executive Officer", "Principal Technologist", "Senior Librarian"], "MIX"),
    11: (["Principal Assistant Registrar", "Principal Accountant", "Chief Technologist"], "MIX"),
    12: (["Deputy Registrar", "Chief Accountant", "Deputy University Librarian"], "MIX"),
    13: (["Deputy Bursar", "Deputy Director, Works", "Director, Health Services"], "MIX"),
}
CONTISS_WEIGHTS = {1: 5, 2: 6, 3: 9, 4: 9, 5: 10, 6: 10, 7: 12, 8: 12, 9: 9, 10: 7, 11: 5, 12: 3, 13: 2}
PRINCIPAL_OFFICERS = [("Registrar", "REG", 15), ("Bursar", "BUR", 15), ("University Librarian", "LIB", 15),
                      ("Director, Works and Services", "WKS", 14), ("Director, ITeMS", "ITM", 14)]

UNIT_KEYWORDS = [  # first match wins
    (("Technologist", "Laboratory", "Technical"), "FAC"),
    (("Librar",), "LIB"),
    (("Accountant", "Bursar"), "BUR"),
    (("Security",), "SEC"),
    (("Health",), "HSV"),
    (("Programmer", "Systems"), "ITM"),
    (("Driver", "Mechanic", "Gardener", "Porter", "Store", "Works"), "WKS"),
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; TECNO KI7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36",
]

DEMO_ACCOUNTS = [  # username, first, last, role
    ("admin.demo", "Olumide", "Bakare", Role.SUPER_ADMIN),
    ("bursar.demo", "Folasade", "Adeyemi", Role.BURSARY),
    ("payroll.officer", "Chinedu", "Okeke", Role.BURSARY),
]


def _aware(d: date, hour=10, minute=0, second=0):
    return timezone.make_aware(datetime.combine(d, time(hour, minute, second)))


def _payday(year, month):
    """The 24th, moved back to Friday when it falls on a weekend."""
    d = date(year, month, min(24, calendar.monthrange(year, month)[1]))
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _parse_period(value, name):
    try:
        y, m = (int(x) for x in value.split("-"))
        if not 1 <= m <= 12:
            raise ValueError
        return y, m
    except ValueError:
        raise CommandError(f"--{name} must look like YYYY-MM")


def _months(start, end):
    y, m = start
    while (y, m) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class Command(BaseCommand):
    help = "Seed a fresh dev database with a realistic university, staff and a year of payroll history."

    def add_arguments(self, parser):
        parser.add_argument("--password", required=True, help="Password for every seeded account (min 10 chars).")
        parser.add_argument("--staff", type=int, default=520, help="Approximate number of staff (default 520).")
        parser.add_argument("--from", dest="start", help="First payroll period YYYY-MM (default: January this year).")
        parser.add_argument("--through", help="Last payroll period YYYY-MM (default: last month).")
        parser.add_argument("--random-seed", type=int, default=2026)

    def handle(self, *args, **o):
        if len(o["password"]) < 10:
            raise CommandError("--password must be at least 10 characters.")
        if StaffProfile.objects.exists() or PayrollRecord.objects.exists():
            raise CommandError(
                "This database already has staff or payroll records. seed_demo_data only runs on a fresh "
                "database (e.g. delete db.sqlite3, then `migrate`)."
            )
        today = timezone.localdate()
        last = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        self.start = _parse_period(o["start"], "from") if o["start"] else (today.year, 1)
        self.end = _parse_period(o["through"], "through") if o["through"] else last
        if self.end >= (today.year, today.month):
            raise CommandError("--through must be before the current month (the current month is left to run from the UI).")
        if self.start > self.end:
            raise CommandError("--from must not be after --through.")

        self.rng = random.Random(o["random_seed"])
        self.password_hash = make_password(o["password"])
        self.onboarding = _aware(date(self.start[0], 1, 1) - timedelta(days=24), 9, 30)
        self.events = []

        call_command("seed_payroll", verbosity=0)
        for year in sorted({y for y, _ in _months(self.start, self.end)}):
            if not TaxBand.objects.filter(tax_year=year).exists():
                call_command("seed_payroll", tax_year=year, verbosity=0)
                self.stdout.write(self.style.WARNING(f"Copied PAYE bands into {year}; verify them before trusting {year} figures."))

        with transaction.atomic():
            self._structure()
            self._accounts()
            self._staff(o["staff"])
        self.stdout.write(f"Created {StaffProfile.objects.count()} staff in {Department.objects.count()} departments.")

        # Payroll runs commit per batch, like production.
        with mock.patch("payroll.tasks.send_batch_report"):  # no email storm while seeding
            for year, month in _months(self.start, self.end):
                self._month(year, month)

        self._flush_events()
        self._summary(o["password"])

    # ------------------------------------------------------------------ structure & people
    def _structure(self):
        Faculty.objects.get_or_create(code=ADMIN_UNIT[1], defaults={"name": ADMIN_UNIT[0]})
        self.faculties = {f.code: f for f in Faculty.objects.all()}
        depts = [
            Department(faculty=self.faculties[code], name=name, code=dcode)
            for code, rows in DEPARTMENTS.items() if code in self.faculties
            for name, dcode in rows
            if not Department.objects.filter(faculty=self.faculties[code], code=dcode).exists()
        ]
        bulk_create_with_history(depts, Department, default_date=self.onboarding)
        self.depts = {}
        for d in Department.objects.select_related("faculty"):
            self.depts.setdefault(d.faculty.code, []).append(d)
        self.academic_depts = [d for code, ds in self.depts.items() if code != "ADM" for d in ds]
        self.unit = {d.code: d for d in self.depts["ADM"]}

    def _new_user(self, username, first, last, role, joined):
        return User(
            username=username, email=f"{username}@staff.ui.test", first_name=first, last_name=last,
            role=role, password=self.password_hash, date_joined=joined,
        )

    def _accounts(self):
        self.admin = None
        self.managers = []
        for username, first, last, role in DEMO_ACCOUNTS:
            user = User.objects.filter(username=username).first()
            if user is None:
                user = self._new_user(username, first, last, role, self.onboarding)
                user._history_date = self.onboarding
                user.save()
            if role == Role.SUPER_ADMIN:
                self.admin = user
            else:
                self.managers.append(user)

    def _unique_username(self, first, last, taken):
        base = f"{first}.{last}".lower()
        name, n = base, 1
        while name in taken:
            n += 1
            name = f"{base}{n}"
        taken.add(name)
        return name

    def _amounts(self, scale, grade, step):
        """Rent, cooperative dues and loan, scaled to the person's pay so net pay stays sensible."""
        monthly = self.scales[(scale, grade, step)] / 12
        r = self.rng
        rent = Decimal(0)
        if r.random() > 0.22:  # ~22% live in staff quarters
            lo, hi = (300_000 + 120_000 * grade, 700_000 + 350_000 * grade) if scale == "CONUASS" else (150_000 + 60_000 * grade, 350_000 + 150_000 * grade)
            rent = Decimal(round(r.uniform(lo, hi), -4))
        coop = Decimal(max(2000, round(float(monthly) * r.uniform(0.03, 0.12), -2))) if r.random() < 0.62 else Decimal(0)
        loan = Decimal(round(float(monthly) * r.uniform(0.05, 0.22), -2)) if r.random() < 0.24 else Decimal(0)
        return rent.quantize(TWO), coop.quantize(TWO), loan.quantize(TWO)

    def _staff(self, target):
        r = self.rng
        self.scales = {(s.scale, s.grade_level, s.step): s.annual_basic for s in SalaryScale.objects.all()}
        taken = set(User.objects.values_list("username", flat=True))
        ac_ids = iter(r.sample(range(1000, 9999), 4000))
        na_ids = iter(r.sample(range(10000, 99999), 6000))
        bank_names, bank_weights = zip(*BANKS)
        first_period = date(*self.start, 1)
        last_period = date(*self.end, 1)
        plans = []  # (user kwargs, profile kwargs, hire date)

        def add(first, last, emp, scale, grade, step, designation, dept, hired, username=None):
            username = username or self._unique_username(first, last, taken)
            rent, coop, loan = self._amounts(scale, grade, step)
            staff_id = f"UI/AC/{next(ac_ids):04d}" if emp == "ACADEMIC" else f"UI/NA/{next(na_ids):05d}"
            plans.append((
                dict(username=username, first=first, last=last),
                dict(staff_id=staff_id, department=dept, designation=designation, employment_type=emp,
                     salary_scale=scale, grade_level=grade, step=step, date_of_employment=hired,
                     bank_name=r.choices(bank_names, bank_weights)[0], account_number=f"{r.randrange(10**9, 10**10)}",
                     annual_rent_paid=rent, cooperative_monthly_dues=coop, loan_monthly_repayment=loan),
            ))

        def seniority_hire(years):
            days = int(max(0.6, r.gauss(years, years * 0.25 + 0.8)) * 365)
            return first_period - timedelta(days=days)

        # The documented demo staff member.
        add("Adebola", "Ogunleye", "ACADEMIC", "CONUASS", 5, 4, "Senior Lecturer",
            next(d for d in self.depts["CMP"] if d.code == "CSC"), date(2013, 10, 1), username="staff.demo")
        # Principal officers.
        for title, unit, grade in PRINCIPAL_OFFICERS:
            add(r.choice(FIRST_NAMES), r.choice(LAST_NAMES), "NON_ACADEMIC", "CONTISS", grade, r.randint(5, 9),
                title, self.unit[unit], seniority_hire(26))

        n_academic = int(target * 0.4)
        for _ in range(n_academic):
            grade = r.choices(list(CONUASS_WEIGHTS), list(CONUASS_WEIGHTS.values()))[0]
            years = CONUASS_YEARS[grade]
            step = min(9, max(1, int(r.triangular(1, 9, min(9, 1 + years / 3)))))
            add(r.choice(FIRST_NAMES), r.choice(LAST_NAMES), "ACADEMIC", "CONUASS", grade, step,
                CONUASS_TITLES[grade], r.choice(self.academic_depts), seniority_hire(years))

        for _ in range(target - n_academic - len(PRINCIPAL_OFFICERS) - 1):
            grade = r.choices(list(CONTISS_WEIGHTS), list(CONTISS_WEIGHTS.values()))[0]
            designation = r.choice(CONTISS_ROLES[grade][0])
            step = r.randint(1, 9)
            add(r.choice(FIRST_NAMES), r.choice(LAST_NAMES), "NON_ACADEMIC", "CONTISS", grade, step,
                designation, self._place(designation), seniority_hire(grade * 1.6))

        # ~4% are new hires during the seeded period (junior grades, step 1).
        span = max(1, (last_period - first_period).days)
        for _ in range(int(target * 0.04)):
            hired = first_period + timedelta(days=r.randint(25, span + 20))
            if r.random() < 0.45:
                grade = r.choice([1, 2, 2, 3])
                add(r.choice(FIRST_NAMES), r.choice(LAST_NAMES), "ACADEMIC", "CONUASS", grade, 1,
                    CONUASS_TITLES[grade], r.choice(self.academic_depts), hired)
            else:
                grade = r.choice([3, 4, 5, 7, 8])
                designation = r.choice(CONTISS_ROLES[grade][0])
                add(r.choice(FIRST_NAMES), r.choice(LAST_NAMES), "NON_ACADEMIC", "CONTISS", grade, 1,
                    designation, self._place(designation), hired)

        def joined(hired):  # existing staff got accounts at system onboarding, new hires on joining
            return self.onboarding if hired < first_period else _aware(hired - timedelta(days=3))

        users = [self._new_user(u["username"], u["first"], u["last"], Role.STAFF, joined(p["date_of_employment"])) for u, p in plans]
        bulk_create_with_history(users, User, batch_size=500, default_user=self.admin, default_date=self.onboarding)
        by_name = {u.username: u for u in User.objects.filter(username__in=[u.username for u in users])}
        profiles = [StaffProfile(user=by_name[u["username"]], **p) for u, p in plans]
        # History rows: existing staff were captured at system onboarding, new hires on joining.
        existing = [p for p in profiles if p.date_of_employment < first_period]
        joiners = [p for p in profiles if p.date_of_employment >= first_period]
        bulk_create_with_history(existing, StaffProfile, batch_size=500, default_user=self.admin, default_date=self.onboarding)
        for p in joiners:
            p._history_user = self.managers[0]
            p._history_date = _aware(p.date_of_employment - timedelta(days=3), 11, r.randint(0, 59))
            p.save()
        self.staff = list(StaffProfile.objects.select_related("user", "department__faculty"))
        self.demo_staff = next(s for s in self.staff if s.user.username == "staff.demo")

    def _place(self, designation):
        for words, unit in UNIT_KEYWORDS:
            if any(w in designation for w in words):
                return self.rng.choice(self.academic_depts) if unit == "FAC" else self.unit[unit]
        # Registry, clerical and secretarial staff: half central, half in faculties.
        return self.unit["REG"] if self.rng.random() < 0.5 else self.rng.choice(self.academic_depts)

    # ------------------------------------------------------------------ the payroll year
    def _edit(self, profile, when, by, **changes):
        for field, value in changes.items():
            setattr(profile, field, value)
        profile._history_user = by
        profile._history_date = when
        profile.save()

    def _month(self, year, month):
        r = self.rng
        payday = _payday(year, month)
        bursar, officer = self.managers[0], self.managers[-1]
        index = list(_months(self.start, self.end)).index((year, month))

        # Retirements take effect before the month is paid.
        if index in (4, 7, 8):
            seniors = [s for s in self.staff if s.is_active and s.grade_level >= (7 if s.salary_scale == "CONUASS" else 12) and s is not self.demo_staff]
            for s in r.sample(seniors, min(2, len(seniors))):
                self._edit(s, _aware(date(year, month, 1) - timedelta(days=2), 14, 5), bursar, is_active=False)

        # October: annual promotions (a step for ~12%, a grade for a few), effective before the run.
        if month == 10:
            when = _aware(date(year, 9, 29), 15, 20)
            for s in r.sample([s for s in self.staff if s.is_active], k=int(len(self.staff) * 0.12)):
                max_grade = 7 if s.salary_scale == "CONUASS" else 15
                if r.random() < 0.25 and s.grade_level < max_grade:
                    title = CONUASS_TITLES.get(s.grade_level + 1) if s.salary_scale == "CONUASS" else s.designation
                    self._edit(s, when, officer, grade_level=s.grade_level + 1, step=1, designation=title)
                elif s.step < 9:
                    self._edit(s, when, officer, step=s.step + 1)

        # July: the cooperative society revises some members' dues.
        if month == 7:
            for s in r.sample([s for s in self.staff if s.cooperative_monthly_dues > 0], 25):
                new = (s.cooperative_monthly_dues * Decimal(r.choice(["1.10", "1.20", "1.25", "0.80"]))).quantize(Decimal("100"))
                self._edit(s, _aware(date(year, 6, 27), 10, 40), officer, cooperative_monthly_dues=new)

        # The June story: a loan keyed with two extra zeros fails that person's payslip; it is fixed and re-run.
        typo = None
        if index == 5:
            typo = next(s for s in self.staff if s.is_active and s.loan_monthly_repayment > 0 and s.department.faculty.code == "SCI")
            self._correct_loan = typo.loan_monthly_repayment
            self._edit(typo, _aware(payday - timedelta(days=4), 16, 12), officer, loan_monthly_repayment=typo.loan_monthly_repayment * 100)

        everyone = [f for f in self.faculties.values()]
        if index == 2:  # March: the College of Medicine is paid early, everyone else on payday.
            college = [self.faculties[c] for c in MEDICAL_COLLEGE if c in self.faculties]
            self._run(year, month, college, officer, _aware(payday - timedelta(days=3), 11, 5))
            self._run(year, month, everyone, bursar, _aware(payday, 9, 40))
        else:
            self._run(year, month, everyone, bursar if month % 3 else officer, _aware(payday, 9, r.randint(5, 50)))

        if typo is not None:
            fixed_at = _aware(payday + timedelta(days=1), 10, 2)
            self._edit(typo, fixed_at, officer, loan_monthly_repayment=self._correct_loan)
            self._run(year, month, [typo.department.faculty], officer, fixed_at + timedelta(minutes=9))

        self._payslip_downloads(year, month, payday)

    def _run(self, year, month, faculties, by, when):
        batch = PayrollBatch.objects.create(period_year=year, period_month=month, initiated_by=by)
        batch.faculties.set(faculties)
        process_payroll_batch.apply(args=[str(batch.pk)])
        batch.refresh_from_db()
        started = when + timedelta(seconds=self.rng.randint(2, 9))
        finished = started + timedelta(seconds=max(4, batch.total_staff // 6) + self.rng.randint(0, 40))
        # Backdating demo rows (a seeding-only use of update(); records stay otherwise untouched).
        PayrollBatch.objects.filter(pk=batch.pk).update(created_at=when, started_at=started, completed_at=finished)
        PayrollRecord.objects.filter(batch=batch).update(created_at=finished)
        self._login(by, when - timedelta(minutes=self.rng.randint(3, 25)))
        self._event(AuditAction.BATCH_TRIGGERED, by, when, target=("PayrollBatch", str(batch.pk)),
                    detail={"period": f"{year}-{month:02d}", "faculty_ids": sorted(f.pk for f in faculties)})
        self.stdout.write(f"  {year}-{month:02d}  {batch.get_status_display():<22} paid {batch.processed_count:>4}"
                          f"  skipped {batch.skipped_count:>4}  failed {batch.failed_count}")

    def _payslip_downloads(self, year, month, payday):
        r = self.rng
        records = list(PayrollRecord.objects.filter(period_year=year, period_month=month).select_related("staff__user"))
        for rec in r.sample(records, int(len(records) * r.uniform(0.12, 0.2))):
            when = _aware(payday + timedelta(days=r.choice([0, 0, 0, 1, 1, 2, 3, 6])), r.randint(7, 22), r.randint(0, 59), r.randint(0, 59))
            user = rec.staff.user
            if r.random() < 0.04:
                self._event(AuditAction.LOGIN_FAILED, None, when - timedelta(minutes=2), username=user.username)
            self._login(user, when - timedelta(seconds=r.randint(20, 300)))
            self._event(AuditAction.PAYSLIP_DOWNLOADED, user, when, target=("PayrollRecord", str(rec.pk)),
                        detail={"staff_id": rec.staff.staff_id, "period": f"{year}-{month:02d}", "own_payslip": True})

    # ------------------------------------------------------------------ activity log
    def _login(self, user, when):
        self._event(AuditAction.LOGIN_SUCCESS, user, when)

    def _event(self, action, actor, when, target=("", ""), detail=None, username=""):
        on_campus = self.rng.random() < 0.6
        ip = f"10.{self.rng.randint(10, 40)}.{self.rng.randint(0, 255)}.{self.rng.randint(2, 254)}" if on_campus \
            else f"172.{self.rng.randint(16, 31)}.{self.rng.randint(0, 255)}.{self.rng.randint(2, 254)}"
        self.events.append(AuditEvent(
            timestamp=when, action=action, actor=actor,
            actor_username=actor.username if actor else username,
            target_type=target[0], target_id=target[1], ip_address=ip,
            user_agent=self.rng.choice(USER_AGENTS), detail=detail or {},
        ))

    def _flush_events(self):
        field = AuditEvent._meta.get_field("timestamp")
        field.auto_now_add = False  # keep the backdated timestamps
        try:
            AuditEvent.objects.bulk_create(sorted(self.events, key=lambda e: e.timestamp), batch_size=1000)
        finally:
            field.auto_now_add = True

    def _summary(self, password):
        batches = PayrollBatch.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"\nSeeded {StaffProfile.objects.count()} staff, {batches} payroll batches, "
            f"{PayrollRecord.objects.count()} payslips and {AuditEvent.objects.count()} activity-log events."
        ))
        self.stdout.write("Sign in with any of these (password as given with --password):")
        for username, role in [("admin.demo", "Super Administrator"), ("bursar.demo", "Bursary Officer"),
                               ("payroll.officer", "Bursary Officer"), ("staff.demo", "Staff (Senior Lecturer, Computer Science & AI, Faculty of Computing)")]:
            self.stdout.write(f"  {username:<17} {role}")
        today = timezone.localdate()
        self.stdout.write(f"The {today:%B %Y} payroll has not been run: trigger it from the dashboard.")
        self.stdout.write(self.style.WARNING("Salary figures are ILLUSTRATIVE placeholders, not official tables."))
