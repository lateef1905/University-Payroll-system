"""Which models keep a change history, and how a history row is turned into a readable diff."""
from django.apps import apps

# API name -> model label. Only these models are exposed through /api/audit/changes/.
TRACKED_MODELS = {
    "user": "accounts.User",
    "faculty": "staff.Faculty",
    "department": "staff.Department",
    "staffprofile": "staff.StaffProfile",
    "salaryscale": "payroll.SalaryScale",
    "allowancerule": "payroll.AllowanceRule",
    "taxband": "payroll.TaxBand",
}

# Changes to these models are visible to Super Administrators only (user management is theirs).
SUPER_ADMIN_ONLY = {"user"}

CHANGE_TYPES = {"+": "created", "~": "updated", "-": "deleted"}

# Bookkeeping columns that change on every save and add no audit value.
IGNORED_FIELDS = {"updated_at", "created_at"}


def history_model(name):
    return apps.get_model(TRACKED_MODELS[name]).history.model


def describe_changes(record):
    """List of {field, old, new} for one history row (all non-empty values for a creation)."""
    tracked = [
        f for f in record.instance_type._meta.fields
        if f.attname in {hf.attname for hf in record._meta.fields} and f.name not in IGNORED_FIELDS
    ]
    if record.history_type == "+":
        return [
            {"field": f.name, "old": None, "new": _plain(getattr(record, f.attname))}
            for f in tracked
            if getattr(record, f.attname) not in (None, "")
        ]
    previous = record.prev_record
    if previous is None:
        return []
    if record.history_type == "-":
        return [{"field": f.name, "old": _plain(getattr(previous, f.attname)), "new": None} for f in tracked]
    delta = record.diff_against(previous, excluded_fields=IGNORED_FIELDS)
    by_name = {f.name: f for f in tracked}
    return [
        {"field": c.field, "old": _plain(c.old), "new": _plain(c.new)}
        for c in delta.changes
        if c.field in by_name
    ]


def _plain(value):
    """JSON-friendly representation (Decimals/dates as strings, ids as ints)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if hasattr(value, "pk"):
        return value.pk
    return str(value)
