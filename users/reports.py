"""Отчёты вместо формул рабочей книги школы: прибыль и убытки, движение денег, контроль оплаты, расчёты с учредителями.

Деньги считаются кассовым методом — по дате оплаты, выплаты или операции (как листы «Поступление ДС» / «Расходы ДС»).
Начисления за обучение (сколько родители должны были заплатить) показываются отдельной справочной строкой.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from django.utils.translation import gettext as _

from .finance import account_balances, contracts_with_active_payments, format_short_month, paid_by_month
from .models import (
    AcademicYear, Account, Category, Contract, Counterparty, EmployeePayout, Payment, Transaction, UnidentifiedReceipt,
    shift_month,
)

ZERO = Decimal(0)


def month_range(start, end):
    months, current = [], start
    while current <= end:
        months.append(current)
        current = shift_month(current, 1)
    return months


def financial_months(year):
    """12 месяцев, заканчивающихся последним месяцем учебного года: для 2026–2027 — июль 2026 … июнь 2027."""
    if year is None:
        end = timezone.localdate().replace(day=1)
    else:
        end = year.end_month
    return month_range(shift_month(end, -11), end)


def academic_months(year):
    return month_range(year.start_month, year.end_month)


class Row:
    """Строка отчёта: значения по месяцам и итог. style: section | line | subtotal | total | info."""

    def __init__(self, label, months, style='line', values=None, negative=False):
        self.label = label
        self.style = style
        self.negative = negative
        self.by_month = defaultdict(Decimal)
        if values:
            for month, value in values.items():
                self.by_month[month] += value
        self.months = months

    def add(self, month, value):
        self.by_month[month] += value

    @property
    def values(self):
        return [self.by_month.get(m, ZERO) for m in self.months] if self.style != 'section' else []

    @property
    def total(self):
        return sum(self.values, ZERO) if self.style != 'section' else None

    @property
    def is_empty(self):
        return self.style == 'line' and not any(self.values)


def _sum_rows(label, rows, months, style='subtotal'):
    total = Row(label, months, style)
    for row in rows:
        for month in months:
            total.add(month, row.by_month.get(month, ZERO))
    return total


def _scoped(qs, school, field='school'):
    return qs.filter(**{field: school}) if school is not None else qs


def _month(day):
    return day.replace(day=1)


def tuition_accrued(months, school=None):
    """Сколько за каждый месяц начислено по договорам (справочно, для сравнения с поступлениями)."""
    accrued = defaultdict(Decimal)
    for contract in contracts_with_active_payments(_scoped(Contract.objects.all(), school)):
        for month in months:
            if contract.is_billable_in(month):
                accrued[month] += contract.total_amount
    return accrued


def profit_and_loss(months, school=None):
    """Прибыль и убытки по месяцам: доходы и расходы по статьям «в P&L», зарплата = выплаты + статьи «зарплата»."""
    start, end = months[0], shift_month(months[-1], 1)
    in_period = lambda qs, field: qs.filter(**{f'{field}__gte': start, f'{field}__lt': end})

    tuition = Row(_('Оплата за обучение'), months)
    for p in in_period(_scoped(Payment.active.all(), school, 'contract__school'), 'payment_date').only('payment_date', 'amount', 'kind'):
        tuition.add(_month(p.payment_date), p.signed_amount)

    income_rows, expense_rows = {}, {}
    salary = Row(_('Зарплата'), months)
    for p in in_period(_scoped(EmployeePayout.active.all(), school), 'payout_date').only('payout_date', 'amount'):
        salary.add(_month(p.payout_date), p.amount)

    transactions = in_period(_scoped(Transaction.active.all(), school), 'date').filter(category__in_pl=True).select_related('category')
    for t in transactions:
        if t.category.is_salary:
            salary.add(_month(t.date), t.amount)
            continue
        rows = income_rows if t.type == 'INCOME' else expense_rows
        row = rows.setdefault(t.category_id, Row(t.category.display_name, months))
        row.sort_key = (t.category.sort_order, t.category.display_name)
        row.add(_month(t.date), t.amount)

    incomes = [tuition] + sorted(income_rows.values(), key=lambda r: r.sort_key)
    expenses = [salary] + sorted(expense_rows.values(), key=lambda r: r.sort_key)
    total_income = _sum_rows(_('Итого доходы'), incomes, months)
    total_expense = _sum_rows(_('Итого расходы'), expenses, months)
    profit = Row(_('Прибыль'), months, 'total')
    for month in months:
        profit.add(month, total_income.by_month[month] - total_expense.by_month[month])

    rows = [Row(_('Доходы'), months, 'section')] + incomes + [total_income]
    rows += [Row(_('Расходы'), months, 'section')] + expenses + [total_expense]
    rows += [profit, Row(_('Начислено за обучение (справочно)'), months, 'info', tuition_accrued(months, school))]
    margin = (profit.total / total_income.total * 100) if total_income.total else None
    return {'rows': [r for r in rows if not r.is_empty or r is tuition or r is salary], 'months': months,
            'income': total_income.total, 'expense': total_expense.total, 'profit': profit.total, 'margin': margin}


SECTION_LABELS = {
    'OPERATING': lambda: _('Операционная деятельность'),
    'INVESTING': lambda: _('Инвестиционная деятельность'),
    'FINANCING': lambda: _('Финансовая деятельность'),
}


def cash_flow(months, school=None):
    """Движение денег по разделам (операционный, инвестиционный, финансовый) и остатки на начало и конец."""
    start, end = months[0], shift_month(months[-1], 1)
    in_period = lambda qs, field: qs.filter(**{f'{field}__gte': start, f'{field}__lt': end})
    flows = {key: {'in': {}, 'out': {}} for key in SECTION_LABELS}

    def line(section, direction, key, label, sort=0):
        bucket = flows[section][direction]
        if key not in bucket:
            bucket[key] = Row(label, months, negative=direction == 'out')
            bucket[key].sort_key = (sort, label)
        return bucket[key]

    payments = in_period(_scoped(Payment.active.all(), school, 'contract__school'), 'payment_date').only('payment_date', 'amount', 'kind')
    for p in payments:
        if p.is_refund:
            line('OPERATING', 'out', 'refunds', _('Возвраты родителям'), 5).add(_month(p.payment_date), p.amount)
        else:
            line('OPERATING', 'in', 'tuition', _('Оплата за обучение'), 0).add(_month(p.payment_date), p.amount)
    receipts = in_period(_scoped(UnidentifiedReceipt.active.filter(payment__isnull=True), school), 'date')
    for r in receipts:
        line('OPERATING', 'in', 'receipts', _('Нераспознанные поступления'), 1).add(_month(r.date), r.amount)
    for p in in_period(_scoped(EmployeePayout.active.all(), school), 'payout_date').only('payout_date', 'amount'):
        line('OPERATING', 'out', 'payouts', _('Зарплата сотрудникам'), 0).add(_month(p.payout_date), p.amount)
    for t in in_period(_scoped(Transaction.active.all(), school), 'date').select_related('category'):
        category = t.category
        direction = 'in' if t.type == 'INCOME' else 'out'
        line(category.section, direction, category.pk, category.display_name, 10 + category.sort_order).add(_month(t.date), t.amount)

    rows, net_rows = [], []
    for section, label in SECTION_LABELS.items():
        inflow = sorted(flows[section]['in'].values(), key=lambda r: r.sort_key)
        outflow = sorted(flows[section]['out'].values(), key=lambda r: r.sort_key)
        net = Row(_('Итого: %(section)s') % {'section': label().lower()}, months, 'subtotal')
        for month in months:
            net.add(month, sum((r.by_month[month] for r in inflow), ZERO) - sum((r.by_month[month] for r in outflow), ZERO))
        rows.append(Row(label(), months, 'section'))
        rows += inflow + outflow + [net]
        net_rows.append(net)

    total = _sum_rows(_('Чистый денежный поток'), net_rows, months, 'total')
    accounts = list(_scoped(Account.objects.select_related('school'), school))
    opening_day = start - timedelta(days=1)
    closing_day = end - timedelta(days=1)
    opening = account_balances(accounts, until=opening_day)
    closing = account_balances(accounts, until=closing_day)
    account_rows = [{'account': a, 'opening': opening[a.pk], 'closing': closing[a.pk], 'change': closing[a.pk] - opening[a.pk]}
                    for a in accounts if a.is_active or opening[a.pk] or closing[a.pk]]

    # Остаток на конец каждого месяца — по всем счетам
    balance_row = Row(_('Остаток денег на конец месяца'), months, 'info')
    for month in months:
        day = shift_month(month, 1) - timedelta(days=1)
        balance_row.add(month, sum(account_balances(accounts, until=day).values(), ZERO))
    rows += [total, balance_row]
    return {'rows': rows, 'months': months, 'accounts': account_rows,
            'opening': sum(opening.values(), ZERO), 'closing': sum(closing.values(), ZERO),
            'opening_day': opening_day, 'closing_day': closing_day}


def payment_control(year, school=None, status='all', today=None):
    """Как лист «Контроль оплаты»: по каждому договору и месяцу учебного года — начислено, оплачено, остаток нарастающим итогом.

    Остаток: отрицательный — долг, положительный — переплата (переносится на следующие месяцы).
    """
    today = today or timezone.localdate()
    months = academic_months(year)
    contracts = Contract.objects.filter(academic_year=year)
    if status == 'active':
        contracts = contracts.filter(is_cancelled=False)
    elif status == 'cancelled':
        contracts = contracts.filter(is_cancelled=True)
    contracts = contracts_with_active_payments(_scoped(contracts, school))
    current = today.replace(day=1)
    rows = []
    totals = {'accrued': [ZERO] * len(months), 'paid': [ZERO] * len(months)}
    for contract in contracts:
        paid_map = paid_by_month(contract)
        running = ZERO
        cells = []
        for i, month in enumerate(months):
            accrued = contract.total_amount if contract.is_billable_in(month) else ZERO
            paid = paid_map.get(month, ZERO)
            running += paid - accrued
            cells.append({'month': month, 'accrued': accrued, 'paid': paid, 'balance': running,
                          'due': month <= current})
            totals['accrued'][i] += accrued
            totals['paid'][i] += paid
        accrued_total = sum((c['accrued'] for c in cells), ZERO)
        paid_total = sum((c['paid'] for c in cells), ZERO)
        due_balance = sum((c['paid'] - c['accrued'] for c in cells if c['due']), ZERO)
        rows.append({'contract': contract, 'cells': cells, 'accrued': accrued_total, 'paid': paid_total,
                     'balance': paid_total - accrued_total, 'debt_now': max(ZERO, -due_balance)})
    rows.sort(key=lambda r: _number_key(r['contract'].number))
    total_row = {
        'cells': [{'accrued': a, 'paid': p, 'balance': p - a} for a, p in zip(totals['accrued'], totals['paid'])],
        'accrued': sum(totals['accrued'], ZERO), 'paid': sum(totals['paid'], ZERO),
        'debt_now': sum((r['debt_now'] for r in rows), ZERO),
    }
    total_row['balance'] = total_row['paid'] - total_row['accrued']
    return {'months': months, 'labels': [format_short_month(m) for m in months], 'rows': rows, 'total': total_row}


def _number_key(number):
    digits = ''.join(ch for ch in number if ch.isdigit())
    return (0, int(digits), number) if digits and number.strip().isdigit() else (1, 0, number)


def founder_settlements(school=None, investor=None):
    """Расчёты с учредителями: сколько вложили (приходы) и сколько им выплачено (расходы), сальдо."""
    counterparties = Counterparty.objects.filter(kind='FOUNDER')
    if investor is not None:
        counterparties = counterparties.filter(investor=investor)
    result = []
    for cp in counterparties.order_by('name'):
        operations = _scoped(Transaction.active.filter(counterparty=cp), school).select_related('category', 'school', 'account') \
            .order_by('-date')
        invested = sum((t.amount for t in operations if t.type == 'INCOME'), ZERO)
        paid = sum((t.amount for t in operations if t.type == 'EXPENSE'), ZERO)
        result.append({'counterparty': cp, 'invested': invested, 'paid': paid, 'balance': invested - paid,
                       'operations': list(operations[:50])})
    totals = {key: sum((r[key] for r in result), ZERO) for key in ('invested', 'paid', 'balance')}
    return {'rows': result, 'totals': totals}


def current_academic_year():
    today = timezone.localdate()
    return AcademicYear.for_date(today) or AcademicYear.objects.order_by('-start_month').first()
