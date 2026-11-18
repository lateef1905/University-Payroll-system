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
audit/     activity log (AuditEvent) + change-history API (django-simple-history)
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

Requirements: Python 3.12-3.14 (developed on 3.14, see `.python-version`), Redis 6+ for
background workers, and PostgreSQL 14+ for production.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # pinned versions; ranges live in requirements.in
cp .env.example .env              # then set DJANGO_SECRET_KEY (and DB_* for PostgreSQL)
```

### Choosing a database

* **SQLite** (`DB_ENGINE=django.db.backends.sqlite3`, the `.env.example` default) is fine for
  local development and exploring the API. Note that SQLite stores decimals as floats; the
  payroll constraints tolerate half a kobo so records reconcile, but money is only exact on
  PostgreSQL.
* **PostgreSQL** is required for production and for any realistic payroll run:

  ```sql
  CREATE USER uipayroll WITH PASSWORD 'change-me' CREATEDB;   -- CREATEDB lets tests create their DB
  CREATE DATABASE uipayroll OWNER uipayroll;
  ```

### Database tables and reference data

Migrations are committed in the repo; just apply them:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_payroll                                   # reference data (illustrative scales)
python manage.py seed_payroll --demo --demo-password '<long password>'   # optional demo users
```

### Running

With `CELERY_EAGER=True` (the `.env.example` default) payroll batches run inline and you only
need `python manage.py runserver`. For real background processing set `CELERY_EAGER=False`
and run Redis, a worker, and the API (three terminals):

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
`/api/payroll/records/{id}/payslip/`, `/api/payroll/records/my-payslip/`,
`/api/payroll/batches/{id}/mark-failed/`, `/api/audit/{changes,events}/`.

## Tests

```bash
# Calculator tests need no database. API tests run on SQLite for convenience;
# use PostgreSQL in CI to exercise the real constraints/indexes.
DJANGO_DEBUG=True DB_ENGINE=django.db.backends.sqlite3 DB_NAME=:memory: python manage.py test
```

`payroll/tests/test_review_fixes.py` holds regression tests for issues found in a code review
(silent record loss, stuck batches, validation gaps, ...); each test's name and docstring
describe the bug it guards against.

## Audit trail

Two complementary records, both read-only:

**1. Change history** (who changed what, when, old -> new value), via
[django-simple-history](https://django-simple-history.readthedocs.io/). Tracked models:
users, faculties, departments, staff profiles, salary scales, allowance rules and tax bands.
Every create/update/delete is captured, whether made through the API or the Django admin,
with the user who made it.

```bash
# All changes to one salary-scale row (payroll managers)
curl -s "localhost:8000/api/audit/changes/?model=salaryscale&object_id=12" -H "Authorization: Bearer $ACCESS"
# Everything one user changed in staff profiles during November
curl -s "localhost:8000/api/audit/changes/?model=staffprofile&changed_by=3&since=2026-11-01&until=2026-11-30" -H "Authorization: Bearer $ACCESS"
```

Each entry: `change_type` (created/updated/deleted), `changed_at`, `changed_by`, and
`changes: [{field, old, new}]`. `model` is one of `user, faculty, department, staffprofile,
salaryscale, allowancerule, taxband`; `user` history is Super Administrator only. In the
admin, every tracked record has a **History** button. One-click revert is disabled on purpose
(it would bypass validation); make corrections as normal edits, which are recorded too.
Password hashes and `last_login` are never copied into history.

**2. Activity log** (Super Administrator only): `GET /api/audit/events/`, also in the admin
under *Audit events*. Records successful and failed logins (with IP address and user agent),
password changes, payroll batches triggered or manually marked as failed, and every payslip
download (with whose payslip it was). Filters: `action`, `actor`, `username`, `target_type`,
`target_id`, `since`, `until`.

**Existing databases:** records created before the audit trail was added have no history
yet. After migrating, take a baseline snapshot once: `python manage.py populate_history --auto`.

Limits worth knowing: `QuerySet.update()`, raw SQL and plain `bulk_create()` bypass the
change history (the code uses `bulk_create_with_history` where it matters). In production,
give the application's database user no rights to modify or delete the `historical*` and
`audit_auditevent` tables beyond INSERT/SELECT if you need tamper-resistance. Retention is
unlimited: history is never pruned automatically.

## Operations checklist

* **Production deploy:** `DJANGO_DEBUG=False`, `CELERY_EAGER=False`, PostgreSQL, real SMTP,
  then `python manage.py collectstatic` (WhiteNoise serves the files under gunicorn).
* **Every new tax year (before January payroll):** load and verify that year's PAYE bands.
  `python manage.py seed_payroll --tax-year 2027` copies the latest year's bands as a starting
  point; edit them in the admin or via `/api/payroll/tax-bands/`. A batch for a year without
  valid bands is rejected with a 400.
* **Stuck batches:** if a worker dies, Redis re-delivers its task after the visibility
  timeout (`CELERY_BROKER_TRANSPORT_OPTIONS`, 40 min; keep it above the 35 min task time
  limit) and the re-delivered task resumes the batch automatically (verified with `kill -9`
  on a 60k-staff batch). Batches still stuck 10 min after that are marked FAILED by
  `python manage.py fail_stale_batches` (run it from cron every 15 minutes) or by Celery beat
  (`celery -A config beat`, schedule in `CELERY_BEAT_SCHEDULE`). They are also swept whenever a
  new batch is triggered. A manager can force it with
  `POST /api/payroll/batches/{id}/mark-failed/`. Re-triggering is always safe: already-paid
  staff are skipped.

## Design decisions worth knowing

* **Idempotent, resumable batches.** Staff already paid for a period are skipped;
  `unique(staff, year, month)` is the final guard. A failed/partial batch can simply
  be re-triggered. One bad employee is logged in `error_log` and never aborts the run.
* **Every staff member is accounted for.** `processed + skipped + failed == total_staff`
  for every batch; a row the database rejects is reported in `error_log` (never silently
  dropped), and any mismatch marks the batch `COMPLETED_WITH_ERRORS`.
* **Staff are in scope** if active, with an active user account, employed on or before the
  last day of the period, and in one of the batch's faculties.
* **Immutable records.** `PayrollRecord` stores a full snapshot (amounts, scale, grade,
  step) with DB `CHECK` constraints that gross/deductions/net reconcile. `save()` on an
  existing row and `delete()` raise; the admin is read-only. (Raw SQL or
  `QuerySet.update()` can still bypass this - restrict DB privileges in production.)
* **Overlapping allowance rules (and tax bands) are rejected** at write time, in both the
  API and the Django admin (shared checks in `payroll/validators.py`), and again at run
  time, because silently double-paying is worse than failing.
* **Access control lives in the queryset** for staff, so other people's records are 404s.
* Access tokens last 15 minutes; refresh tokens rotate and are blacklisted on logout.

## Known gaps (intentionally not guessed)

* **Not modelled:** NHF (2.5% of basic) and NHIS deductions, other PAYE reliefs, mid-month
  joiners/leavers (proration), arrears, overtime, and bank payment-file export.
* **Effective-dated scales:** `SalaryScale` has no effective date; history is protected
  only because records snapshot their amounts.
* Verify every statutory rate with the Bursary/Tax adviser before go-live; laws change.
