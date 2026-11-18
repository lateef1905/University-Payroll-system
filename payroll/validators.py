"""
Business-rule validation shared by the models (Django admin, `full_clean()`) and the
API serializers, so the admin can never accept data the API would reject.

Each function takes a model instance (saved or not) and raises
django.core.exceptions.ValidationError.
"""
from django.core.exceptions import ValidationError
from django.db.models import Q


def check_allowance_rule(rule) -> None:
    from .models import AllowanceMethod, AllowanceRule

    if rule.value is not None and rule.calc_method == AllowanceMethod.PERCENT_OF_BASIC and rule.value > 100:
        raise ValidationError({"value": "A percentage cannot exceed 100."})
    if rule.min_grade is not None and rule.max_grade is not None and rule.min_grade > rule.max_grade:
        raise ValidationError({"min_grade": "min_grade cannot exceed max_grade."})

    # Two active rules of the same type that can match the same employee would double-pay.
    if not rule.is_active:
        return
    qs = AllowanceRule.objects.filter(
        is_active=True, allowance_type=rule.allowance_type,
        min_grade__lte=rule.max_grade, max_grade__gte=rule.min_grade,
    )
    if rule.pk:
        qs = qs.exclude(pk=rule.pk)
    if rule.employment_type != "ALL":
        qs = qs.filter(Q(employment_type="ALL") | Q(employment_type=rule.employment_type))
    if rule.salary_scale:
        qs = qs.filter(Q(salary_scale__isnull=True) | Q(salary_scale="") | Q(salary_scale=rule.salary_scale))
    if qs.exists():
        raise ValidationError(
            "An active rule of this allowance type already overlaps this employment type / scale / grade range."
        )


def check_tax_band(band) -> None:
    from .models import TaxBand

    lower, upper = band.lower_bound, band.upper_bound
    if lower is None:
        return  # field-level validation reports the missing value
    if upper is not None and upper <= lower:
        raise ValidationError({"upper_bound": "Must be greater than lower_bound."})
    qs = TaxBand.objects.filter(tax_year=band.tax_year)
    if band.pk:
        qs = qs.exclude(pk=band.pk)
    for other in qs:
        other_upper_ok = other.upper_bound is None or other.upper_bound > lower
        this_upper_ok = upper is None or upper > other.lower_bound
        if other_upper_ok and this_upper_ok:
            raise ValidationError("This band overlaps an existing band for the same tax year.")
