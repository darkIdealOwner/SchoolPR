from datetime import date

from django.db import migrations


def shift_month(month_first, delta):
    total = month_first.year * 12 + month_first.month - 1 + delta
    return month_first.replace(year=total // 12, month=total % 12 + 1, day=1)


def forwards(apps, schema_editor):
    """Текущий учебный год и первый месяц обучения у уже внесённых договоров."""
    AcademicYear = apps.get_model('users', 'AcademicYear')
    Contract = apps.get_model('users', 'Contract')
    year, _ = AcademicYear.objects.get_or_create(
        name='2026–2027', defaults={'start_month': date(2026, 9, 1), 'end_month': date(2027, 6, 1)})
    for contract in Contract.objects.filter(billing_start__isnull=True):
        month = contract.date.replace(day=1)
        # Раньше начисления шли с месяца договора — для старых договоров так и оставляем,
        # учебный год проставляем только тем, кто к нему относится
        if year.start_month <= shift_month(month, 3) and month <= year.end_month:
            contract.academic_year = year
        contract.billing_start = month
        contract.save(update_fields=['academic_year', 'billing_start'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0014_academic_year'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
