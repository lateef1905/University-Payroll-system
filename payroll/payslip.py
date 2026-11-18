"""PDF payslip rendering (ReportLab, in-memory)."""
import calendar
from io import BytesIO

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _fmt(amount) -> str:
    # Built-in PDF fonts lack the naira glyph, so the ISO code is used.
    return f"{settings.PAYROLL['CURRENCY']} {amount:,.2f}"


def _mask_account(number: str) -> str:
    return f"******{number[-4:]}" if number else "-"


def _table(rows, col_widths, header=True, total_last=False):
    t = Table(rows, colWidths=col_widths)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    if total_last:
        style += [("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef2f7"))]
    t.setStyle(TableStyle(style))
    return t


def render_payslip_pdf(record) -> bytes:
    """Render a PayrollRecord (with staff -> user/department/faculty loaded) to PDF bytes."""
    staff = record.staff
    user = staff.user
    month_name = calendar.month_name[record.period_month]

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Payslip {month_name} {record.period_year}", author=settings.PAYROLL["INSTITUTION_NAME"],
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=styles["Title"], fontSize=16, spaceAfter=2)
    sub = ParagraphStyle("s", parent=styles["Normal"], alignment=1, fontSize=10, textColor=colors.HexColor("#444444"))
    small = ParagraphStyle("sm", parent=styles["Normal"], fontSize=7.5, textColor=colors.grey)

    story = [
        Paragraph(settings.PAYROLL["INSTITUTION_NAME"].upper(), title),
        Paragraph(f"Payslip for {month_name} {record.period_year}", sub),
        Spacer(1, 8 * mm),
    ]

    info = [
        ["Name", user.get_full_name() or user.username, "Staff ID", staff.staff_id],
        ["Faculty", staff.department.faculty.name, "Department", staff.department.name],
        ["Salary scale", f"{record.salary_scale} GL{record.grade_level} / Step {record.step}", "Employment", staff.get_employment_type_display()],
        ["Bank", staff.bank_name or "-", "Account", _mask_account(staff.account_number)],
    ]
    t = Table(info, colWidths=[28 * mm, 58 * mm, 28 * mm, 58 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [t, Spacer(1, 6 * mm)]

    w = [120 * mm, 52 * mm]
    earnings = [["EARNINGS", "Amount"],
                ["Basic salary", _fmt(record.basic_salary)],
                ["Housing allowance", _fmt(record.housing_allowance)],
                ["Transport allowance", _fmt(record.transport_allowance)],
                ["Research allowance", _fmt(record.research_allowance)],
                ["Medical allowance", _fmt(record.medical_allowance)],
                ["GROSS EARNINGS", _fmt(record.gross_earnings)]]
    deductions = [["DEDUCTIONS", "Amount"],
                  ["Pension (employee, CPS)", _fmt(record.pension_employee)],
                  ["PAYE tax", _fmt(record.paye_tax)],
                  ["Cooperative dues", _fmt(record.cooperative_dues)],
                  ["Loan repayment", _fmt(record.loan_repayment)],
                  ["TOTAL DEDUCTIONS", _fmt(record.total_deductions)]]
    story += [_table(earnings, w, total_last=True), Spacer(1, 5 * mm), _table(deductions, w, total_last=True), Spacer(1, 5 * mm)]

    net = Table([["NET PAY", _fmt(record.net_pay)]], colWidths=w)
    net.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 12), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#dff0e0")), ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#2e7d32")),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [net, Spacer(1, 6 * mm)]
    story.append(Paragraph(
        f"Employer pension contribution (not deducted from you): {_fmt(record.pension_employer)}. "
        f"Annual taxable income used for PAYE: {_fmt(record.annual_taxable_income)}.", small))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        f"System-generated on {timezone.localtime().strftime('%d %b %Y %H:%M')}. This document is confidential and needs no signature.", small))

    doc.build(story)
    return buf.getvalue()
