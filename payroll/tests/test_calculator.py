"""Database-free unit tests for the calculation engine."""
from decimal import Decimal as D

from django.test import SimpleTestCase

from payroll.calculator import (
    AllowanceSpec,
    PayrollCalculationError,
    PayrollRates,
    TaxBandSpec,
    calculate_annual_paye,
    calculate_payroll,
    select_allowance_rules,
    validate_tax_bands,
)

BANDS = [
    TaxBandSpec(D(0), D(800000), D(0)),
    TaxBandSpec(D(800000), D(3000000), D(15)),
    TaxBandSpec(D(3000000), D(12000000), D(18)),
    TaxBandSpec(D(12000000), D(25000000), D(21)),
    TaxBandSpec(D(25000000), D(50000000), D(23)),
    TaxBandSpec(D(50000000), None, D(25)),
]
RATES = PayrollRates(D("0.08"), D("0.10"), D("0.20"), D("500000"))
RULES = [
    AllowanceSpec(1, "HOUSING", "PERCENT_OF_BASIC", D(25), "ALL", None, 1, 17),
    AllowanceSpec(2, "TRANSPORT", "PERCENT_OF_BASIC", D(15), "ALL", None, 1, 17),
    AllowanceSpec(3, "RESEARCH", "PERCENT_OF_BASIC", D(10), "ACADEMIC", None, 1, 17),
    AllowanceSpec(4, "MEDICAL", "FIXED_MONTHLY", D(5000), "ALL", None, 1, 17),
]


def run(**overrides):
    args = dict(
        annual_basic=D("3600000"), employment_type="ACADEMIC", salary_scale="CONUASS", grade_level=5,
        annual_rent_paid=D("0"), cooperative_dues=D("0"), loan_repayment=D("0"),
        allowance_rules=RULES, tax_bands=BANDS, rates=RATES,
    )
    args.update(overrides)
    return calculate_payroll(**args)


class PayeTests(SimpleTestCase):
    def test_zero_band(self):
        self.assertEqual(calculate_annual_paye(D(800000), BANDS), D("0.00"))

    def test_published_example_5m(self):
        self.assertEqual(calculate_annual_paye(D(5000000), BANDS), D("690000.00"))

    def test_published_example_3_102m(self):
        self.assertEqual(calculate_annual_paye(D(3102000), BANDS), D("348360.00"))

    def test_gap_in_bands_rejected(self):
        broken = [BANDS[0], TaxBandSpec(D(900000), None, D(15))]
        with self.assertRaises(PayrollCalculationError):
            validate_tax_bands(broken)

    def test_closed_top_band_rejected(self):
        with self.assertRaises(PayrollCalculationError):
            validate_tax_bands(BANDS[:-1])


class PayrollTests(SimpleTestCase):
    def test_identities_hold(self):
        b = run(annual_rent_paid=D("600000"), cooperative_dues=D("10000"))
        self.assertEqual(b.gross_earnings, b.basic_salary + b.housing_allowance + b.transport_allowance + b.research_allowance + b.medical_allowance)
        self.assertEqual(b.total_deductions, b.pension_employee + b.paye_tax + b.cooperative_dues + b.loan_repayment)
        self.assertEqual(b.net_pay, b.gross_earnings - b.total_deductions)

    def test_known_values(self):
        b = run()
        self.assertEqual(b.basic_salary, D("300000.00"))
        self.assertEqual(b.housing_allowance, D("75000.00"))
        self.assertEqual(b.pension_employee, D("33600.00"))  # 8% of (300k+75k+45k)

    def test_non_academic_gets_no_research_allowance(self):
        b = run(employment_type="NON_ACADEMIC", salary_scale="CONTISS")
        self.assertEqual(b.research_allowance, D("0.00"))

    def test_rent_relief_reduces_tax_and_is_capped(self):
        no_rent = run().paye_tax
        some = run(annual_rent_paid=D("600000")).paye_tax
        huge = run(annual_rent_paid=D("9000000")).paye_tax
        capped = run(annual_rent_paid=D("2500000")).paye_tax  # 20% = 500k = cap
        self.assertLess(some, no_rent)
        self.assertEqual(huge, capped)

    def test_negative_net_is_rejected(self):
        with self.assertRaises(PayrollCalculationError):
            run(cooperative_dues=D("10000000"))

    def test_negative_inputs_rejected(self):
        with self.assertRaises(PayrollCalculationError):
            run(loan_repayment=D("-1"))

    def test_overlapping_rules_are_a_hard_error(self):
        dup = RULES + [AllowanceSpec(9, "HOUSING", "FIXED_MONTHLY", D(1000), "ACADEMIC", None, 1, 17)]
        with self.assertRaises(PayrollCalculationError):
            select_allowance_rules(dup, employment_type="ACADEMIC", salary_scale="CONUASS", grade_level=5)
