"""
Pure payroll calculation engine.

This module deliberately has NO Django/ORM imports so that:
  * it is trivially unit-testable (no database),
  * the same logic is used by the Celery batch task and any future "what-if" API,
  * every money value is a Decimal, quantised once, with explicit rounding.

Calculation order (monthly):
    basic            = annual_basic / 12
    allowances       = % of monthly basic, or fixed monthly amount (per active rule)
    gross            = basic + allowances
    pension_base     = basic + housing + transport       (Pension Reform Act 2014)
    employee pension = pension_rate * pension_base
    PAYE             = progressive tax on annualised taxable income / 12
                       taxable = gross*12 - employee pension*12 - rent relief
    total deductions = pension + PAYE + cooperative dues + loan repayment
    net pay          = gross - total deductions   (must not be negative)
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

TWOPLACES = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")
MONTHS = Decimal("12")


class PayrollCalculationError(Exception):
    """Raised when a staff member's payroll cannot be computed safely."""


def money(value) -> Decimal:
    """Quantise to kobo using commercial rounding."""
    return Decimal(value).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PayrollRates:
    pension_employee_rate: Decimal
    pension_employer_rate: Decimal
    rent_relief_rate: Decimal
    rent_relief_cap: Decimal

    @classmethod
    def from_settings(cls) -> "PayrollRates":
        from django.conf import settings  # imported lazily to keep this module framework-free

        cfg = settings.PAYROLL
        return cls(
            pension_employee_rate=Decimal(cfg["PENSION_EMPLOYEE_RATE"]),
            pension_employer_rate=Decimal(cfg["PENSION_EMPLOYER_RATE"]),
            rent_relief_rate=Decimal(cfg["RENT_RELIEF_RATE"]),
            rent_relief_cap=Decimal(cfg["RENT_RELIEF_CAP"]),
        )


@dataclass(frozen=True)
class TaxBandSpec:
    lower: Decimal                 # annual lower bound (inclusive)
    upper: Optional[Decimal]       # annual upper bound; None = no upper limit
    rate_percent: Decimal          # e.g. Decimal("15.00")


@dataclass(frozen=True)
class AllowanceSpec:
    rule_id: int
    allowance_type: str            # HOUSING | TRANSPORT | RESEARCH | MEDICAL
    calc_method: str               # PERCENT_OF_BASIC | FIXED_MONTHLY
    value: Decimal
    employment_type: str           # ACADEMIC | NON_ACADEMIC | ALL
    salary_scale: Optional[str]    # None = any scale
    min_grade: int
    max_grade: int


@dataclass(frozen=True)
class PayrollBreakdown:
    basic_salary: Decimal
    housing_allowance: Decimal
    transport_allowance: Decimal
    research_allowance: Decimal
    medical_allowance: Decimal
    gross_earnings: Decimal
    pension_employee: Decimal
    pension_employer: Decimal
    annual_taxable_income: Decimal
    paye_tax: Decimal
    cooperative_dues: Decimal
    loan_repayment: Decimal
    total_deductions: Decimal
    net_pay: Decimal


# --------------------------------------------------------------------------
# Tax
# --------------------------------------------------------------------------
def validate_tax_bands(bands: Iterable[TaxBandSpec]) -> list:
    """Return bands sorted ascending after checking they form a gapless ladder."""
    ordered = sorted(bands, key=lambda b: b.lower)
    if not ordered:
        raise PayrollCalculationError("No tax bands are configured for this tax year.")
    if ordered[0].lower != 0:
        raise PayrollCalculationError("The first tax band must start at 0.")
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev.upper is None or prev.upper != nxt.lower:
            raise PayrollCalculationError("Tax bands must be contiguous (no gaps or overlaps).")
    if ordered[-1].upper is not None:
        raise PayrollCalculationError("The top tax band must be open-ended (no upper bound).")
    return ordered


def calculate_annual_paye(annual_taxable_income: Decimal, bands: Iterable[TaxBandSpec]) -> Decimal:
    """Progressive tax: each band's rate applies only to the slice inside that band."""
    ordered = validate_tax_bands(bands)
    income = Decimal(annual_taxable_income)
    tax = Decimal("0")
    for band in ordered:
        if income <= band.lower:
            break
        top = income if band.upper is None else min(income, band.upper)
        tax += (top - band.lower) * band.rate_percent / HUNDRED
    return money(tax)


# --------------------------------------------------------------------------
# Allowances
# --------------------------------------------------------------------------
def select_allowance_rules(rules: Iterable[AllowanceSpec], *, employment_type: str, salary_scale: str, grade_level: int) -> dict:
    """Pick the (single) matching rule per allowance type.

    Two matching rules for the same type would silently double-pay, so it is a
    hard error instead of a guess.
    """
    chosen: dict = {}
    for rule in rules:
        if rule.employment_type not in ("ALL", employment_type):
            continue
        if rule.salary_scale not in (None, "", salary_scale):
            continue
        if not (rule.min_grade <= grade_level <= rule.max_grade):
            continue
        if rule.allowance_type in chosen:
            raise PayrollCalculationError(
                f"Multiple active {rule.allowance_type} allowance rules match "
                f"(ids {chosen[rule.allowance_type].rule_id} and {rule.rule_id})."
            )
        chosen[rule.allowance_type] = rule
    return chosen


def _allowance_amount(rule: Optional[AllowanceSpec], monthly_basic: Decimal) -> Decimal:
    if rule is None:
        return ZERO
    if rule.calc_method == "PERCENT_OF_BASIC":
        return money(monthly_basic * rule.value / HUNDRED)
    if rule.calc_method == "FIXED_MONTHLY":
        return money(rule.value)
    raise PayrollCalculationError(f"Unknown allowance calculation method: {rule.calc_method}")


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
def calculate_payroll(
    *,
    annual_basic: Decimal,
    employment_type: str,
    salary_scale: str,
    grade_level: int,
    annual_rent_paid: Decimal,
    cooperative_dues: Decimal,
    loan_repayment: Decimal,
    allowance_rules: Iterable[AllowanceSpec],
    tax_bands: Iterable[TaxBandSpec],
    rates: PayrollRates,
) -> PayrollBreakdown:
    for label, value in (
        ("annual basic salary", annual_basic),
        ("annual rent paid", annual_rent_paid),
        ("cooperative dues", cooperative_dues),
        ("loan repayment", loan_repayment),
    ):
        if value is None or Decimal(value) < 0:
            raise PayrollCalculationError(f"Invalid (negative or missing) {label}.")
    if Decimal(annual_basic) == 0:
        raise PayrollCalculationError("Annual basic salary is zero.")

    basic = money(Decimal(annual_basic) / MONTHS)

    selected = select_allowance_rules(
        allowance_rules, employment_type=employment_type, salary_scale=salary_scale, grade_level=grade_level
    )
    housing = _allowance_amount(selected.get("HOUSING"), basic)
    transport = _allowance_amount(selected.get("TRANSPORT"), basic)
    research = _allowance_amount(selected.get("RESEARCH"), basic)
    medical = _allowance_amount(selected.get("MEDICAL"), basic)
    gross = basic + housing + transport + research + medical

    # Contributory Pension Scheme (PRA 2014): basic + housing + transport.
    pension_base = basic + housing + transport
    pension_employee = money(pension_base * rates.pension_employee_rate)
    pension_employer = money(pension_base * rates.pension_employer_rate)

    # PAYE (Nigeria Tax Act 2025): annualise, subtract pension + rent relief, apply bands.
    rent_relief = min(money(Decimal(annual_rent_paid) * rates.rent_relief_rate), rates.rent_relief_cap)
    annual_taxable = max(ZERO, gross * MONTHS - pension_employee * MONTHS - rent_relief)
    paye = money(calculate_annual_paye(annual_taxable, tax_bands) / MONTHS)

    coop = money(cooperative_dues)
    loan = money(loan_repayment)
    total_deductions = pension_employee + paye + coop + loan
    net = gross - total_deductions
    if net < 0:
        raise PayrollCalculationError(
            f"Deductions ({total_deductions}) exceed gross earnings ({gross}); review cooperative/loan deductions."
        )

    return PayrollBreakdown(
        basic_salary=basic,
        housing_allowance=housing,
        transport_allowance=transport,
        research_allowance=research,
        medical_allowance=medical,
        gross_earnings=gross,
        pension_employee=pension_employee,
        pension_employer=pension_employer,
        annual_taxable_income=money(annual_taxable),
        paye_tax=paye,
        cooperative_dues=coop,
        loan_repayment=loan,
        total_deductions=total_deductions,
        net_pay=net,
    )
