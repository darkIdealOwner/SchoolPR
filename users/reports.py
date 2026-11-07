"""Отчёты вместо формул рабочей книги школы — в раскладке её листов: дашборд, свод договоров, движение ДС по способам,
контроль оплаты, задолженность за учёбу, свод по актуальным договорам, своды поступлений и расходов, расчёты с учредителями.

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
    AcademicYear, Account, Category, Contract, Counterparty, EmployeePayout, Payment, Transaction, Transfer, UnidentifiedReceipt,
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

    def __init__(self, label, months, style='line', values=None, negative=False, total=None):
        self.label = label
        self.style = style
        self.negative = negative
        self.by_month = defaultdict(Decimal)
        if values:
            for month, value in values.items():
                self.by_month[month] += value
        self.months = months
        self.fixed_total = total      # итог не суммой по месяцам (например, остаток на конец)

    def add(self, month, value):
        self.by_month[month] += value

    @property
    def values(self):
        return [self.by_month.get(m, ZERO) for m in self.months] if self.style != 'section' else []

    @property
    def total(self):
        if self.style == 'section':
            return None
        return self.fixed_total if self.fixed_total is not None else sum(self.values, ZERO)

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
            accrued[month] += contract.accrual_for(month)
    return accrued


def profit_and_loss(months, school=None):
    """Прибыль и убытки по месяцам: доходы и расходы по статьям «в P&L», зарплата = выплаты + статьи «зарплата»."""
    start, end = months[0], shift_month(months[-1], 1)
    in_period = lambda qs, field: qs.filter(**{f'{field}__gte': start, f'{field}__lt': end})

    tuition = Row(_('Оплата за обучение'), months)
    for p in in_period(_scoped(Payment.active.all(), school, 'contract__school'), 'payment_date').only('payment_date', 'amount', 'kind'):
        tuition.add(_month(p.payment_date), p.signed_amount)

    income_rows, expense_rows = {}, {}
    expense_by_name = defaultdict(Decimal)     # по базовому (русскому) названию статьи — для дашборда
    salary = Row(_('Зарплата'), months)
    for p in in_period(_scoped(EmployeePayout.active.all(), school), 'payout_date').only('payout_date', 'amount'):
        salary.add(_month(p.payout_date), p.amount)

    transactions = in_period(_scoped(Transaction.active.all(), school), 'date').filter(category__in_pl=True).select_related('category')
    for t in transactions:
        if t.category.is_salary:
            salary.add(_month(t.date), t.amount)
            continue
        rows = income_rows if t.type == 'INCOME' else expense_rows
        if t.type == 'EXPENSE':
            expense_by_name[t.category.name] += t.amount
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
            'income': total_income.total, 'expense': total_expense.total, 'profit': profit.total, 'margin': margin,
            'income_row': total_income, 'expense_row': total_expense, 'profit_row': profit, 'expense_by_name': expense_by_name}


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
            accrued = contract.accrual_for(month)
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
                     'balance': paid_total - accrued_total, 'debt_now': max(ZERO, -due_balance), 'due_balance': due_balance})
    rows.sort(key=lambda r: _number_key(r['contract'].number))
    running, total_cells = ZERO, []
    for a, p in zip(totals['accrued'], totals['paid']):
        running += p - a
        total_cells.append({'accrued': a, 'paid': p, 'balance': p - a, 'running': running})
    total_row = {
        'cells': total_cells,
        'accrued': sum(totals['accrued'], ZERO), 'paid': sum(totals['paid'], ZERO),
        'debt_now': sum((r['debt_now'] for r in rows), ZERO),
    }
    total_row['balance'] = total_row['paid'] - total_row['accrued']
    labels = [format_short_month(m) for m in months]
    return {'months': months, 'labels': labels, 'groups': [None] + labels, 'rows': rows, 'total': total_row}


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


# --- Отчёты в раскладке листов книги школы

# Способы движения денег — как «Вид поступления / расхода» в книге (по виду счёта)
METHOD_KINDS = (Account.KIND_BANK, Account.KIND_CARD, Account.KIND_CASH, Account.KIND_OTHER)
# Статьи, которые в отчёте о прибыли идут отдельно от операционных расходов (по базовому названию статьи)
TAX_CATEGORY, INTEREST_CATEGORY, OTHER_EXPENSE_CATEGORY = 'Налоги', 'Проценты по кредиту', 'Прочие расходы'


def method_label(kind):
    return dict(Account.KIND_CHOICES).get(kind, kind)


class Move:
    """Одно движение денег по счёту — как строка листа «Поступление ДС» или «Расходы ДС»."""
    __slots__ = ('date', 'kind', 'direction', 'amount', 'label', 'sort', 'transfer')

    def __init__(self, date, kind, direction, amount, label, sort, transfer=False):
        self.date, self.kind, self.direction, self.amount = date, kind or Account.KIND_OTHER, direction, amount
        self.label, self.sort, self.transfer = label, sort, transfer


def movements(start, end, school=None):
    """Все действующие движения денег за [start, end): оплаты, возвраты, нераспознанные, выплаты, приходы/расходы, переводы.

    Перевод между своими счетами — две записи: расход со счёта-источника и приход на счёт-получатель (как в книге).
    """
    period = lambda qs, field: qs.filter(**{f'{field}__gte': start, f'{field}__lt': end})
    moves = []
    salary_category = Category.objects.filter(is_salary=True).order_by('sort_order').first()
    salary_label = salary_category.display_name if salary_category else _('Зарплата')
    salary_sort = 10 + (salary_category.sort_order if salary_category else 10)
    for p in period(_scoped(Payment.active.all(), school, 'contract__school'), 'payment_date').select_related('account'):
        kind = p.account.kind if p.account else None
        if p.is_refund:
            moves.append(Move(p.payment_date, kind, 'out', p.amount, _('Возвраты родителям'), 5))
        else:
            moves.append(Move(p.payment_date, kind, 'in', p.amount, _('Оплата за обучение'), 0))
    for r in period(_scoped(UnidentifiedReceipt.active.filter(payment__isnull=True), school), 'date').select_related('account'):
        moves.append(Move(r.date, r.account.kind, 'in', r.amount, _('Нераспознанные поступления'), 1))
    for p in period(_scoped(EmployeePayout.active.all(), school), 'payout_date').select_related('account'):
        moves.append(Move(p.payout_date, p.account.kind if p.account else None, 'out', p.amount, salary_label, salary_sort))
    for t in period(_scoped(Transaction.active.all(), school), 'date').select_related('account', 'category', 'counterparty'):
        if t.counterparty_id:
            label, sort = t.counterparty.display_name, 500     # в книге вложения и выплаты учредителям — по имени
        elif t.category_id:
            label, sort = t.category.display_name, 10 + t.category.sort_order
        else:
            label, sort = (_('Прочие поступления') if t.type == 'INCOME' else _('Прочие расходы')), 1000
        moves.append(Move(t.date, t.account.kind if t.account else None, 'in' if t.type == 'INCOME' else 'out', t.amount, label, sort))
    for t in period(_scoped(Transfer.active.all(), school), 'date').select_related('from_account', 'to_account'):
        label = _('Перевод: %(from)s → %(to)s') % {'from': t.from_account.display_name, 'to': t.to_account.display_name}
        moves.append(Move(t.date, t.from_account.kind, 'out', t.amount, label, 2000, transfer=True))
        moves.append(Move(t.date, t.to_account.kind, 'in', t.amount, label, 2000, transfer=True))
    return moves


def _period(months):
    return months[0], shift_month(months[-1], 1)


def _opening_by_kind(school, day):
    accounts = list(_scoped(Account.objects.all(), school))
    balances = account_balances(accounts, until=day)
    result = defaultdict(Decimal)
    for account in accounts:
        result[account.kind] += balances[account.pk]
    return result


def bank_and_cash(months, school=None, moves=None):
    """«Банк и касса»: по каждому способу — остаток на начало, приход, расход, остаток на конец периода."""
    start, end = _period(months)
    moves = movements(start, end, school) if moves is None else moves
    opening = _opening_by_kind(school, start - timedelta(days=1))
    rows = []
    for kind in METHOD_KINDS:
        inflow = sum((m.amount for m in moves if m.kind == kind and m.direction == 'in'), ZERO)
        outflow = sum((m.amount for m in moves if m.kind == kind and m.direction == 'out'), ZERO)
        if kind == Account.KIND_OTHER and not (inflow or outflow or opening[kind]):
            continue
        rows.append({'label': method_label(kind), 'opening': opening[kind], 'in': inflow, 'out': outflow,
                     'closing': opening[kind] + inflow - outflow})
    total = {key: sum((r[key] for r in rows), ZERO) for key in ('opening', 'in', 'out', 'closing')}
    return {'rows': rows, 'total': total}


def dashboard(year, school=None, investor=None):
    """Как лист DASHBOARD: главные показатели, прибыль и убытки, месяцы, доходы по источникам, расходы по статьям,
    движение денег по разделам, расчёты с учредителями, банк и касса."""
    months = financial_months(year)
    start, end = _period(months)
    pl = profit_and_loss(months, school)
    cf = cash_flow(months, school)
    moves = movements(start, end, school)

    income = pl['income']
    tax = pl['expense_by_name'].get(TAX_CATEGORY, ZERO)
    interest = pl['expense_by_name'].get(INTEREST_CATEGORY, ZERO)
    other = pl['expense_by_name'].get(OTHER_EXPENSE_CATEGORY, ZERO)
    operating = pl['expense'] - tax - interest - other
    operating_profit = income - operating
    net = operating_profit - tax - interest - other

    def pct(value):
        return (value / income * 100) if income else None

    pl_lines = [
        ('line', _('Доход'), income), ('line', _('Операционные расходы'), operating),
        ('subtotal', _('Операционная прибыль'), operating_profit), ('line', _('Налоги'), tax),
        ('line', _('Проценты по кредиту'), interest), ('line', _('Прочие расходы'), other),
        ('total', _('Чистая прибыль'), net), ('info', _('EBITDA'), operating_profit),
    ]
    pl_rows = [{'style': style, 'label': label, 'amount': amount, 'pct': pct(amount)} for style, label, amount in pl_lines]

    sections = {}
    for row in cf['rows']:
        if row.style == 'subtotal':
            sections[len(sections)] = row.total
    operating_cf, investing_cf, financing_cf = (sections.get(i, ZERO) for i in range(3))

    inflow_by_month = defaultdict(Decimal)
    for m in moves:
        if m.direction == 'in' and not m.transfer:
            inflow_by_month[_month(m.date)] += m.amount
    monthly = []
    for month in months:
        inc, exp = pl['income_row'].by_month[month], pl['expense_row'].by_month[month]
        monthly.append({'month': month, 'label': format_short_month(month), 'income': inc, 'expense': exp, 'profit': inc - exp,
                        'margin': ((inc - exp) / inc * 100) if inc else None, 'inflow': inflow_by_month[month]})
    monthly_total = {key: sum((m[key] for m in monthly), ZERO) for key in ('income', 'expense', 'profit', 'inflow')}
    monthly_total['margin'] = (monthly_total['profit'] / monthly_total['income'] * 100) if monthly_total['income'] else None

    def grouped(direction):
        totals, order = defaultdict(Decimal), {}
        for m in moves:
            if m.direction == direction and not m.transfer:
                totals[m.label] += m.amount
                order[m.label] = min(order.get(m.label, m.sort), m.sort)
        rows = [{'label': label, 'amount': amount} for label, amount in sorted(totals.items(), key=lambda kv: (order[kv[0]], kv[0]))]
        return rows, sum(totals.values(), ZERO)

    sources, sources_total = grouped('in')
    articles, articles_total = grouped('out')
    cash_rows = [{'style': row.style, 'label': row.label, 'amount': row.total, 'negative': row.negative}
                 for row in cf['rows'] if row.style != 'info']
    return {
        'months': months, 'labels': [format_short_month(m) for m in months],
        'kpi': {'income': income, 'operating': operating, 'operating_profit': operating_profit, 'tax': tax, 'net': net,
                'margin': pct(net), 'operating_cf': operating_cf, 'investing_cf': investing_cf, 'financing_cf': financing_cf,
                'net_cf': operating_cf + investing_cf + financing_cf},
        'pl': pl_rows, 'monthly': monthly, 'monthly_total': monthly_total,
        'sources': sources, 'sources_total': sources_total, 'articles': articles, 'articles_total': articles_total,
        'cash_flow': cash_rows, 'founders': founder_settlements(school, investor),
        'bank_cash': bank_and_cash(months, school, moves),
    }


def _class_key(value):
    digits = ''.join(ch for ch in value if ch.isdigit())
    return (0, int(digits), value) if digits else (1, 0, value)


def contract_summary(year, school=None, month=None, today=None):
    """Как лист СВОД: число и сумма договоров по статусу и классу в разрезе месяца договора;
    ниже — оплачено и долги за выбранный месяц учебного года (или за весь год)."""
    contracts = list(_scoped(Contract.objects.filter(academic_year=year), school).only(
        'date', 'student_class', 'is_cancelled', 'total_amount'))
    columns = sorted({_month(c.date) for c in contracts})
    tables = {}
    for measure in ('count', 'sum'):
        rows = []
        grand = Row(_('Итого'), columns, 'total')
        for cancelled, label in ((False, _('Актуальные')), (True, _('Отменённые'))):
            group = [c for c in contracts if c.is_cancelled == cancelled]
            if not group:
                continue
            head = Row(label, columns, 'subtotal')
            lines = []
            for grade in sorted({c.student_class or '—' for c in group}, key=_class_key):
                line = Row(_('%(grade)s класс') % {'grade': grade} if grade != '—' else _('Класс не указан'), columns)
                for c in group:
                    if (c.student_class or '—') == grade:
                        value = Decimal(1) if measure == 'count' else c.total_amount
                        line.add(_month(c.date), value)
                        head.add(_month(c.date), value)
                        grand.add(_month(c.date), value)
                lines.append(line)
            rows += [head] + lines
        tables[measure] = rows + ([grand] if rows else [])

    control = payment_control(year, school, 'all', today=today)
    month_keys = [m.strftime('%Y-%m') for m in control['months']]
    choice = month if month in month_keys else ''
    if choice:
        i = month_keys.index(choice)
        paid = control['total']['cells'][i]['paid']
        balances = [(r, r['cells'][i]['balance']) for r in control['rows']]
    else:
        # За весь год: оплачено всего, остаток — на сегодня (будущие месяцы ещё не начислены)
        paid = control['total']['paid']
        balances = [(r, r['due_balance']) for r in control['rows']]
    debts = sum((-b for r, b in balances if b < 0), ZERO)
    active_debts = sum((-b for r, b in balances if b < 0 and not r['contract'].is_cancelled), ZERO)
    overpaid = sum((b for r, b in balances if b > 0), ZERO)
    return {
        'columns': columns, 'labels': [format_short_month(m) for m in columns], 'count': tables['count'], 'sum': tables['sum'],
        'month_options': list(zip(month_keys, control['labels'])), 'month': choice,
        'paid': paid, 'debts': debts, 'active_debts': active_debts, 'overpaid': overpaid, 'net': overpaid - debts,
    }


def cash_by_method(year, school=None):
    """Как лист «Движение ДС»: поступления, расходы и остаток по способам (перечисление, терминал, наличные) по месяцам."""
    months = financial_months(year)
    start, end = _period(months)
    moves = movements(start, end, school)
    opening = _opening_by_kind(school, start - timedelta(days=1))
    kinds = [k for k in METHOD_KINDS if k != Account.KIND_OTHER or opening[k] or any(m.kind == k for m in moves)]
    rows = []
    flows = {}
    for direction, title in (('in', _('Поступления')), ('out', _('Расходы'))):
        rows.append(Row(title, months, 'section'))
        lines = []
        for kind in kinds:
            line = Row(method_label(kind), months)
            for m in moves:
                if m.kind == kind and m.direction == direction:
                    line.add(_month(m.date), m.amount)
            lines.append(line)
            flows[(direction, kind)] = line
        rows += lines + [_sum_rows(_('Итого'), lines, months)]
    rows.append(Row(_('Остаток на конец месяца'), months, 'section'))
    balance_lines = []
    for kind in kinds:
        running = opening[kind]
        line = Row(method_label(kind), months)
        for month in months:
            running += flows[('in', kind)].by_month[month] - flows[('out', kind)].by_month[month]
            line.add(month, running)
        line.fixed_total = running
        balance_lines.append(line)
    total_balance = _sum_rows(_('Итого'), balance_lines, months, 'total')
    total_balance.fixed_total = sum((line.fixed_total for line in balance_lines), ZERO)
    rows += balance_lines + [total_balance]
    opening_rows = [{'label': method_label(k), 'amount': opening[k]} for k in kinds]
    return {'rows': rows, 'months': months, 'labels': [format_short_month(m) for m in months],
            'opening': opening_rows, 'opening_total': sum((r['amount'] for r in opening_rows), ZERO),
            'opening_day': start - timedelta(days=1)}


def debts_for_month(year, school=None, month=None, today=None):
    """Как лист «Задолженность за учебу»: договоры с долгом на выбранный месяц учебного года (остаток нарастающим итогом)."""
    today = today or timezone.localdate()
    control = payment_control(year, school, 'all', today=today)
    keys = [m.strftime('%Y-%m') for m in control['months']]
    if month not in keys:
        current = today.replace(day=1).strftime('%Y-%m')
        month = current if current in keys else (keys[-1] if control['months'] and control['months'][-1] < today.replace(day=1) else keys[0])
    i = keys.index(month)
    rows = []
    for r in control['rows']:
        cells = r['cells'][:i + 1]
        balance = cells[-1]['balance']
        if balance < 0:
            rows.append({'contract': r['contract'], 'accrued': sum((c['accrued'] for c in cells), ZERO),
                         'paid': sum((c['paid'] for c in cells), ZERO), 'debt': -balance})
    total = {key: sum((r[key] for r in rows), ZERO) for key in ('accrued', 'paid', 'debt')}
    return {'rows': rows, 'total': total, 'month': month, 'month_options': list(zip(keys, control['labels'])),
            'month_label': control['labels'][i]}


def active_by_tariff(year, school=None):
    """Как лист «Свод по актуальным договорам»: сколько действующих договоров на каждую сумму в месяц."""
    contracts = _scoped(Contract.objects.filter(academic_year=year, is_cancelled=False), school).only('total_amount')
    groups = defaultdict(lambda: {'count': 0, 'sum': ZERO})
    for c in contracts:
        groups[c.total_amount]['count'] += 1
        groups[c.total_amount]['sum'] += c.total_amount
    rows = [{'amount': amount, **values} for amount, values in sorted(groups.items())]
    return {'rows': rows, 'count': sum(r['count'] for r in rows), 'sum': sum((r['sum'] for r in rows), ZERO)}


def money_pivots(year, school=None):
    """Как сводные таблицы на листах «Поступление ДС» и «Расходы ДС»: способ → статья, по месяцам."""
    months = financial_months(year)
    start, end = _period(months)
    moves = movements(start, end, school)
    result = {}
    for direction in ('in', 'out'):
        rows, kind_rows = [], []
        for kind in METHOD_KINDS:
            own = [m for m in moves if m.kind == kind and m.direction == direction]
            if not own:
                continue
            head = Row(method_label(kind), months, 'subtotal')
            lines, order = {}, {}
            for m in own:
                line = lines.setdefault(m.label, Row(m.label, months))
                order[m.label] = min(order.get(m.label, m.sort), m.sort)
                line.add(_month(m.date), m.amount)
                head.add(_month(m.date), m.amount)
            rows += [head] + sorted(lines.values(), key=lambda r: (order[r.label], r.label))
            kind_rows.append(head)
        rows.append(_sum_rows(_('Итого'), kind_rows, months, 'total'))
        result[direction] = rows
    return {'months': months, 'labels': [format_short_month(m) for m in months], 'income': result['in'], 'expense': result['out']}


# --- Разбивка по месяцам для графиков главной страницы

# Группы расходов по базовым названиям статей (как в справочнике); остальное в P&L — «прочие»
EXPENSE_GROUPS = {
    'Налоги': 'tax', 'Питание': 'food',
    'Аренда': 'upkeep', 'Коммунальные услуги': 'upkeep', 'Хозяйственные расходы': 'upkeep',
    'Товарно-материальные запасы': 'upkeep', 'Питьевая вода': 'upkeep', 'Связь': 'upkeep', 'Охрана': 'upkeep',
    'Вывоз мусора': 'upkeep',
}
LOAN_REPAYMENT_CATEGORY = 'Погашение кредита'


def monthly_breakdown(months, school=None):
    """По месяцам: расходы по видам (зарплата, налоги, питание, содержание, прочие), прибыль, деньги учредителей,
    кредиты и основные средства. Списки float в порядке months — сразу для графиков."""
    start, end = _period(months)
    index = {m: i for i, m in enumerate(months)}
    keys = ('salary', 'tax', 'food', 'upkeep', 'other', 'profit', 'founders_in', 'founders_out', 'loans_out', 'assets')
    data = {k: [ZERO] * len(months) for k in keys}

    def add(key, day, amount):
        i = index.get(_month(day))
        if i is not None:
            data[key][i] += amount

    for p in _scoped(EmployeePayout.active.filter(payout_date__gte=start, payout_date__lt=end), school).only('payout_date', 'amount'):
        add('salary', p.payout_date, p.amount)
    for t in _scoped(Transaction.active.filter(date__gte=start, date__lt=end), school).select_related('category'):
        c = t.category
        if t.type == 'INCOME':
            if c is not None and not c.in_pl and c.section in ('FINANCING', 'INVESTING'):
                add('founders_in', t.date, t.amount)       # вложения учредителей, полученные кредиты
            continue
        if c is None or c.in_pl:
            key = 'salary' if c is not None and c.is_salary else EXPENSE_GROUPS.get(c.name if c else '', 'other')
        elif t.counterparty_id or c.section == 'FINANCING' and c.name != LOAN_REPAYMENT_CATEGORY:
            key = 'founders_out'
        elif c.name == LOAN_REPAYMENT_CATEGORY:
            key = 'loans_out'
        elif c.section == 'INVESTING':
            key = 'assets'
        else:
            continue        # возврат ошибочного платежа и т. п. — не расход школы
        add(key, t.date, t.amount)
    profit = profit_and_loss(months, school)['profit_row']
    data['profit'] = [profit.by_month[m] for m in months]
    return {f'mb_{k}': [float(v) for v in values] for k, values in data.items()}
