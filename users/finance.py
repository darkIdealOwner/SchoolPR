"""Финансовые расчёты платформы: начисления, долги, сводки для дашбордов.

Всё, что считает деньги, живёт здесь, чтобы страницы, экспорт и будущая
синхронизация с 1С опирались на одни и те же правила.
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from django.db.models import Prefetch, Q
from django.utils import timezone
from django.utils.dates import MONTHS, MONTHS_3
from django.utils.translation import gettext

from .models import Contract, Payment, EmployeePayout, Transaction, Transfer, School, UnidentifiedReceipt, shift_month

ZERO = Decimal(0)



def format_russian_month(d):
    """«Январь 2026» на языке интерфейса (названия месяцев — из переводов Django)."""
    # Порядок «месяц год» переводится: в корейском «2026년 10월»
    return gettext('%(month)s %(year)s') % {'month': MONTHS[d.month], 'year': d.year}


def format_short_month(d):
    return gettext('%(month)s %(year)s') % {'month': MONTHS_3[d.month], 'year': d.strftime('%y')}


def current_month():
    return timezone.localdate().replace(day=1)


def parse_month(month_str):
    """'YYYY-MM' -> дата 1-го числа месяца; при ошибке — текущий месяц."""
    try:
        return datetime.strptime(month_str, '%Y-%m').date()
    except (TypeError, ValueError):
        return current_month()


def contracts_with_active_payments(queryset):
    return queryset.select_related('school').prefetch_related(
        Prefetch('payments', queryset=Payment.active.order_by('payment_date')), 'month_prices',
    )


def billing_months(contract, until=None):
    """Месяцы начислений по договору (1-е числа), не позже until: по сроку договора и месяцы со стоимостью, заданной вручную."""
    months = set(m for m in contract.month_price_map if until is None or m <= until)
    start = contract.first_billing_month
    last = contract.last_billing_month
    if last is not None:
        if until is not None:
            last = min(last, until)
        curr = start
        while curr <= last:
            months.add(curr)
            curr = shift_month(curr, 1)
    return sorted(months)


def paid_by_month(contract):
    """Сумма действующих оплат по месяцам начисления. Нужен prefetch активных оплат."""
    result = defaultdict(Decimal)
    for p in contract.payments.all():
        if not p.is_reversed:
            result[p.for_month.replace(day=1)] += p.signed_amount
    return result


def get_debt_details(contract, target_date):
    """Долг по договору на месяц target_date включительно и список неоплаченных месяцев.

    Начисления идут только в пределах срока договора и до месяца отмены.
    Сторнированные оплаты не учитываются.
    """
    target_first = target_date.replace(day=1)
    payment_map = paid_by_month(contract)

    unpaid_months = []
    running_balance = ZERO
    for month in billing_months(contract, until=target_first):
        running_balance += contract.accrual_for(month) - payment_map[month]
        if running_balance > Decimal('0.01'):
            unpaid_months.append(month)

    return max(ZERO, running_balance), unpaid_months


def contract_schedule(contract, until=None):
    """График по договору: начислено / оплачено / остаток по каждому месяцу и нарастающий долг."""
    until = until or current_month()
    payment_map = paid_by_month(contract)
    payments_by_month = defaultdict(list)
    for p in contract.payments.all():
        payments_by_month[p.for_month.replace(day=1)].append(p)

    rows = []
    running = ZERO
    for month in billing_months(contract):
        paid = payment_map[month]
        accrued = contract.accrual_for(month)
        is_due = month <= until
        if is_due:
            running += accrued - paid
        balance = accrued - paid
        if balance <= 0:
            status = 'paid'
        elif paid > 0:
            status = 'partial'
        elif is_due:
            status = 'debt'
        else:
            status = 'upcoming'
        rows.append({
            'month': month,
            'label': format_russian_month(month),
            'month_str': month.strftime('%Y-%m'),
            'accrued': accrued,
            'price': contract.month_price_map.get(month),
            'paid': paid,
            'balance': max(balance, ZERO),
            'status': status,
            'running_debt': max(running, ZERO),
            'payments': payments_by_month[month],
        })
    return rows


def _empty_metrics():
    return {
        'accrued': ZERO,       # начислено за обучение и питание
        'collected': ZERO,     # получено оплат от родителей (по дате оплаты)
        'payouts': ZERO,       # выплаты сотрудникам
        'expenses': ZERO,      # прочие расходы
        'other_income': ZERO,  # прочие поступления и инвестиции
        'capital_out': ZERO,   # выплаты учредителям, погашение кредитов, основные средства
    }


def _finish(metrics):
    metrics['spent'] = metrics['payouts'] + metrics['expenses']
    # Операционный результат: оплаты родителей минус зарплаты и расходы (инвестиции не доход)
    metrics['result'] = metrics['collected'] - metrics['spent']
    # Денежный поток: всё, что пришло, минус всё, что ушло
    metrics['cash_flow'] = metrics['result'] + metrics['other_income'] - metrics['capital_out']
    return metrics


def build_overview(school=None, months=12, today=None):
    """Сводка для дашбордов: помесячная динамика, текущий месяц, долги, разбивка по школам."""
    today = today or timezone.localdate()
    current = today.replace(day=1)
    month_list = [shift_month(current, -i) for i in range(months - 1, -1, -1)]
    period_start = month_list[0]
    period_end = shift_month(current, 1)

    contracts = Contract.objects.all()
    payments = Payment.active.filter(payment_date__gte=period_start, payment_date__lt=period_end)
    payouts = EmployeePayout.active.filter(payout_date__gte=period_start, payout_date__lt=period_end)
    transactions = Transaction.active.filter(date__gte=period_start, date__lt=period_end)
    if school is not None:
        contracts = contracts.filter(school=school)
        payments = payments.filter(contract__school=school)
        payouts = payouts.filter(school=school)
        transactions = transactions.filter(school=school)

    # (school_id, month) -> метрики
    cells = defaultdict(_empty_metrics)
    per_school = defaultdict(lambda: {
        'debt': ZERO, 'debtors': 0, 'active_contracts': 0,
        'accrued_current': ZERO, 'paid_for_current': ZERO,
    })
    debtors = []

    for contract in contracts_with_active_payments(contracts):
        sid = contract.school_id
        payment_map = paid_by_month(contract)
        for month in billing_months(contract, until=current):
            if month >= period_start:
                cells[(sid, month)]['accrued'] += contract.accrual_for(month)
        info = per_school[sid]
        if contract.is_billable_in(current):
            info['active_contracts'] += 1
        if contract.has_accrual_in(current):
            accrued = contract.accrual_for(current)
            info['accrued_current'] += accrued
            info['paid_for_current'] += min(payment_map[current], accrued)
        debt, unpaid = get_debt_details(contract, current)
        if debt > Decimal('0.01'):
            info['debt'] += debt
            info['debtors'] += 1
            contract.total_debt = debt
            contract.unpaid_count = len(unpaid)
            debtors.append(contract)

    for p in payments.select_related('contract'):
        cells[(p.contract.school_id, p.payment_date.replace(day=1))]['collected'] += p.signed_amount
    for p in payouts:
        cells[(p.school_id, p.payout_date.replace(day=1))]['payouts'] += p.amount
    for t in transactions.select_related('category'):
        if t.type == 'INCOME':
            key = 'other_income'
        elif t.category is None or t.category.in_pl:
            key = 'expenses'
        else:
            key = 'capital_out'     # выплаты учредителям, кредиты, основные средства — не расход школы
        cells[(t.school_id, t.date.replace(day=1))][key] += t.amount

    series = []
    for month in month_list:
        m = _empty_metrics()
        for (sid, cell_month), values in cells.items():
            if cell_month == month:
                for k, v in values.items():
                    m[k] += v
        m = _finish(m)
        m['month'] = month
        m['label'] = format_short_month(month)
        series.append(m)

    period_totals = _finish({k: sum((row[k] for row in series), ZERO) for k in _empty_metrics()})

    school_ids = set(per_school) | {sid for sid, _ in cells}
    schools_by_id = School.objects.in_bulk([sid for sid in school_ids if sid is not None])
    school_rows = []
    for sid in school_ids:
        m = _empty_metrics()
        for k in m:
            m[k] = cells[(sid, current)][k] if (sid, current) in cells else ZERO
        m = _finish(m)
        info = per_school[sid]
        m.update(info)
        m['school'] = schools_by_id.get(sid)
        m['name'] = m['school'].name if m['school'] else gettext('Без школы')
        m['collection_rate'] = _rate(info['paid_for_current'], info['accrued_current'])
        school_rows.append(m)
    school_rows.sort(key=lambda r: r['name'])

    totals = {
        'debt': sum((r['debt'] for r in school_rows), ZERO),
        'debtors': sum(r['debtors'] for r in school_rows),
        'active_contracts': sum(r['active_contracts'] for r in school_rows),
        'accrued_current': sum((r['accrued_current'] for r in school_rows), ZERO),
        'paid_for_current': sum((r['paid_for_current'] for r in school_rows), ZERO),
    }
    totals['collection_rate'] = _rate(totals['paid_for_current'], totals['accrued_current'])

    debtors.sort(key=lambda c: c.total_debt, reverse=True)

    return {
        'current_month': current,
        'current_label': format_russian_month(current),
        'current': series[-1],
        'previous': series[-2] if len(series) > 1 else None,
        'series': series,
        'period_totals': period_totals,
        'period_label': f"{format_short_month(period_start)} — {format_short_month(current)}",
        'schools': school_rows,
        'totals': totals,
        'top_debtors': debtors[:10],
    }


def _rate(part, whole):
    if not whole:
        return None
    return int(round(part * 100 / whole))


def chart_payload(series, include_spending=True, breakdown=None):
    """Данные для графиков на странице (числа как float, JSON-совместимо).

    Без include_spending суммы зарплат и расходов в страницу не попадают совсем,
    а не просто скрываются на экране. breakdown — разбивка по месяцам из reports.monthly_breakdown.
    """
    payload = {
        'labels': [row['label'] for row in series],
        'accrued': [float(row['accrued']) for row in series],
        'collected': [float(row['collected']) for row in series],
    }
    if include_spending:
        payload['spent'] = [float(row['spent']) for row in series]
        if breakdown:
            payload.update(breakdown)
    return payload


def account_balances(accounts, until=None):
    """Остаток на конец дня until по каждому счёту: остаток на начало + приход − расход ± переводы.

    Учитываются только действующие записи с даты остатка на начало счёта. Возвращает {account.pk: Decimal}.
    """
    until = until or timezone.localdate()
    accounts = list(accounts)
    balances = {a.pk: (a.opening_balance if a.opening_date <= until else ZERO) for a in accounts}
    starts = {a.pk: a.opening_date for a in accounts}
    ids = list(balances)

    def add(rows, date_attr, sign, account_attr='account_id', amount_attr='amount'):
        for row in rows:
            account_id = getattr(row, account_attr)
            day = getattr(row, date_attr)
            if account_id in balances and starts[account_id] <= day <= until:
                balances[account_id] += sign * getattr(row, amount_attr)

    # Возврат родителю — расход со счёта (signed_amount отрицательный)
    add(Payment.active.filter(account_id__in=ids).only('account_id', 'payment_date', 'amount', 'kind'), 'payment_date', 1,
        amount_attr='signed_amount')
    # Неразобранные поступления уже лежат на счёте; разобранные посчитаны как оплаты
    add(UnidentifiedReceipt.active.filter(account_id__in=ids, payment__isnull=True), 'date', 1)
    add(EmployeePayout.active.filter(account_id__in=ids).only('account_id', 'payout_date', 'amount'), 'payout_date', -1)
    transactions = Transaction.active.filter(account_id__in=ids).only('account_id', 'date', 'amount', 'type')
    add([t for t in transactions if t.type == 'INCOME'], 'date', 1)
    add([t for t in transactions if t.type == 'EXPENSE'], 'date', -1)
    transfers = list(Transfer.active.filter(Q(from_account_id__in=ids) | Q(to_account_id__in=ids)))
    add(transfers, 'date', -1, 'from_account_id')
    add(transfers, 'date', 1, 'to_account_id')
    return balances
