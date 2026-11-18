# University of Ibadan - Employee Payroll Management System

Django + DRF + PostgreSQL + Celery/Redis backend with JWT role-based access, a
Decimal-safe calculation engine, bulk monthly payroll batches and PDF payslips.

> **Read this first.** The salary figures produced by `seed_payroll` are
> **illustrative placeholders**, not the official CONUASS/CONTISS tables. Load the
> official scales (via `/api/payroll/salary-scales/` or the admin) before using
> any output for real pay. Statutory rates (pension, rent relief) live in
> `config/settings.py -> PAYROLL`; PAYE bands live in the `TaxBand` table.

## Architecture

```
config/    settings, celery app, root urls
accounts/  custom User + role (SUPER_ADMIN | BURSARY | STAFF), JWT auth, permissions
staff/     Faculty, Department, StaffProfile
payroll/   SalaryScale, AllowanceRule, TaxBand, PayrollBatch, PayrollRecord
           calculator.py  pure Decimal engine (no ORM, unit-tested)
           tasks.py       Celery: process_payroll_batch, send_batch_report
           payslip.py     ReportLab PDF renderer
```

### Role matrix

| Capability                                  | Super Admin | Bursary | Staff |
|---------------------------------------------|:-----------:|:-------:|:-----:|
| Manage users                                | yes         | no      | no    |
| Edit salary scales / allowances / tax bands | yes         | yes     | no    |
| Manage faculties, departments, profiles     | yes         | yes     | read faculties/departments only |
| Trigger payroll batches                     | yes         | yes     | no    |
| View all payroll records                    | yes         | yes     | no    |
| View / download **own** payslips            | yes         | yes     | yes (only own; others return 404) |

### Calculation (monthly)

```
basic            = annual_basic(scale, grade, step) / 12
gross            = basic + housing + transport + research + medical
pension (CPS)    = 8% x (basic + housing + transport)      employer 10% shown, not deducted
PAYE             = progressive bands on (gross*12 - pension*12 - rent relief) / 12
deductions       = pension + PAYE + cooperative dues + loan repayment
net              = gross - deductions        (batch records an error if net < 0)
```
Rent relief = min(20% of annual rent, NGN 500,000). Bands seeded for 2026 per the
Nigeria Tax Act 2025: 0% to 800k, 15% to 3m, 18% to 12m, 21% to 25m, 23% to 50m, 25% above.

## Setup

Requirements: Python 3.10+, PostgreSQL 14+, Redis 6+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit DJANGO_SECRET_KEY and DB_PASSWORD
```

Create the database:

```sql
CREATE USER uipayroll WITH PASSWORD 'change-me';
CREATE DATABASE uipayroll OWNER uipayroll;
```

Migrations (the repo ships empty migration packages; generate them once, in this
order, and commit them):

```bash
python manage.py makemigrations accounts staff payroll
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_payroll                                   # reference data
python manage.py seed_payroll --demo --demo-password '<long password>'   # optional demo users
```

Start Redis, the Celery worker, and the API (three terminals):

```bash
docker run --rm -p 6379:6379 redis:7          # or: redis-server
celery -A config worker -l info
python manage.py runserver
```

Swagger UI: http://127.0.0.1:8000/api/docs/ - ReDoc: `/api/redoc/` - schema: `/api/schema/`
(with `DJANGO_DEBUG=False` these are admin-only).

## Testing the endpoints (curl / Postman)

```bash
# 1. Login (returns access + refresh)
curl -s -X POST localhost:8000/api/auth/login/ -H 'Content-Type: application/json' \
  -d '{"username":"bursar.demo","password":"<password>"}'

# 2. Trigger payroll for one faculty (returns 202 immediately)
curl -s -X POST localhost:8000/api/payroll/batches/ \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"period_year":2026,"period_month":11,"faculty_ids":[2]}'

# 3. Poll the batch
curl -s localhost:8000/api/payroll/batches/<batch-id>/ -H "Authorization: Bearer $ACCESS"

# 4. Staff downloads their own payslip
curl -s -o payslip.pdf "localhost:8000/api/payroll/records/my-payslip/?year=2026&month=11" \
  -H "Authorization: Bearer $STAFF_ACCESS"
```

In Postman: create an environment variable `access`, add `Authorization: Bearer {{access}}`
at the collection level, and use "Send and Download" for the payslip PDF.

Key endpoints: `/api/auth/{login,refresh,logout,me,change-password,users}/`,
`/api/staff/{faculties,departments,profiles}/`, `/api/staff/profiles/me/`,
`/api/payroll/{salary-scales,allowance-rules,tax-bands,batches,records}/`,
`/api/payroll/records/{id}/payslip/`, `/api/payroll/records/my-payslip/`.

## Tests

```bash
# Calculator tests need no database. API tests run on SQLite for convenience;
# use PostgreSQL in CI to exercise the real constraints/indexes.
DJANGO_DEBUG=True DB_ENGINE=django.db.backends.sqlite3 DB_NAME=:memory: python manage.py test
```

## Design decisions worth knowing

* **Idempotent, resumable batches.** Staff already paid for a period are skipped;
  `unique(staff, year, month)` is the final guard. A failed/partial batch can simply
  be re-triggered. One bad employee is logged in `error_log` and never aborts the run.
* **Immutable records.** `PayrollRecord` stores a full snapshot (amounts, scale, grade,
  step) with DB `CHECK` constraints that gross/deductions/net reconcile. `save()` on an
  existing row and `delete()` raise; the admin is read-only. (Raw SQL or
  `QuerySet.update()` can still bypass this - restrict DB privileges in production.)
* **Overlapping allowance rules are rejected** at write time and again at run time,
  because silently double-paying is worse than failing.
* **Access control lives in the queryset** for staff, so other people's records are 404s.
* Access tokens last 15 minutes; refresh tokens rotate and are blacklisted on logout.

## Known gaps (intentionally not guessed)

* **Not modelled:** NHF (2.5% of basic) and NHIS deductions, other PAYE reliefs, mid-month
  joiners/leavers (proration), arrears, overtime, and bank payment-file export.
* **No audit trail** of who edited salary scales or tax bands - add `django-simple-history`
  before production.
* **Effective-dated scales:** `SalaryScale` has no effective date; history is protected
  only because records snapshot their amounts.
* Verify every statutory rate with the Bursary/Tax adviser before go-live; laws change.
