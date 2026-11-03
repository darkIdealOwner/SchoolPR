"""Загрузка рабочей книги школы целиком и сверка итогов с ней.

Из книги берутся только листы, которые заполняются руками: «Договоры», «Поступление ДС», «Расходы ДС».
Остальные листы (контроль оплаты, задолженность, отчёты) платформа считает сама.

Как разносятся строки:
- «Договоры» → договоры (проверка теми же правилами, что и ручной ввод, через excel_import.check_contract);
- «Поступление ДС», краткое назначение «Таълим пули» (или пусто) с номером договора → оплата по договору,
  без номера → нераспознанное поступление; прочие статьи → приходы по справочнику статей;
- «Расходы ДС» → расходы по справочнику статей; пополнение счёта, снятие наличных, терминал → переводы между счетами;
- незнакомая статья (имя человека или фирмы) → учредитель: вложение (приход) или выплата ему (расход).

Строка с ошибкой, которую можно внести с заменой (нет срока или года рождения, неправильный телефон или класс,
дата не того года), загружается с красной пометкой: в поле data_problem записано, что было в книге и что поставлено.
Без номера, ФИО, стоимости, даты или суммы строку загрузить нельзя — она остаётся в списке ошибок.

Повторная загрузка той же книги ничего не задваивает: уже внесённые строки пропускаются. Если в исправленной книге
строка стала правильной, договор с пометкой обновляется по ней, а пометка снимается.
"""
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils.translation import gettext as _

from . import excel_import
from .excel_import import ImportFileError, RowResult, normalize_header, parse_month
from .phone import is_valid_uz_phone, normalize_phone
from .models import (
    Account, AuditLog, Category, Contract, Counterparty, EmployeePayout, Payment, Transaction, Transfer, UnidentifiedReceipt,
    shift_month,
)
from .reports import financial_months

MAX_FILE_SIZE = 20 * 1024 * 1024

SHEETS = {
    'contracts': {
        'names': ('Договоры',),
        'required': ('number', 'date', 'student', 'fee'),
        'key': 'number',
        'columns': {
            'number': ('№ договора', 'Номер договора'), 'date': ('Дата', 'Дата договора'),
            'student': ('ФИО ученика',), 'birth_year': ('Год рождения',), 'grade': ('Класс',),
            'duration': ('Срок, мес', 'Срок'), 'parents': ('ФИО родителей',),
            'passport': ('Серия и номер паспорта', 'Паспорт'), 'inps': ('ИНПС',), 'address': ('Адрес',),
            'phone': ('№ телефона', 'Телефон'), 'specialization': ('Направление',),
            'fee': ('Стоимость учебы', 'Стоимость учёбы', 'Стоимость'), 'discount_pct': ('Скидка, %', 'Скидка %'),
            'discount_sum': ('Скидка, сум',), 'food': ('Стоимость питание', 'Стоимость питания', 'Питание'),
            'total': ('Сумма',), 'status': ('Отметка об отмены договора', 'Статус'), 'notes': ('Примечание',),
        },
    },
    'income': {
        'names': ('Поступление ДС',),
        'required': ('date', 'amount'),
        'key': 'amount',
        'columns': {
            'date': ('Дата',), 'method': ('Вид поступление', 'Вид поступления'), 'contract': ('№ договора',),
            'period': ('Оплата за период',), 'amount': ('Сумма',), 'purpose': ('Назначение платежа',),
            'category': ('Краткое назначение',),
        },
    },
    'expense': {
        'names': ('Расходы ДС',),
        'required': ('date', 'amount'),
        'key': 'amount',
        'columns': {
            'date': ('Дата',), 'method': ('Вид расходы', 'Вид расхода'), 'amount': ('Сумма',),
            'purpose': ('Назначение платежа',), 'category': ('Краткое назначение',), 'notes': ('Примечание',),
        },
    },
}

TUITION = 'Таълим пули'
# Статьи книги (как в скрытом листе «Список») → что это на платформе
INCOME_MAP = {
    TUITION: ('tuition',),
    'Банкдан олинган пул': ('bank_cash_in',),
    'Бошқалар': ('category', 'Прочие поступления'),
    'Ҳисоб-рақамга қайтариш': ('category', 'Возврат на счёт'),
    'Нотўғри тўлов': ('category', 'Ошибочный платёж'),
}
EXPENSE_MAP = {
    'Иш ҳақи': ('category', 'Зарплата'), 'Солиқ': ('category', 'Налоги'), 'Озиқ-овқат': ('category', 'Питание'),
    'Коммунал тўловлар': ('category', 'Коммунальные услуги'), 'Ижара': ('category', 'Аренда'),
    'Хўжалик харажатлари': ('category', 'Хозяйственные расходы'),
    'Товар моддий захиралар': ('category', 'Товарно-материальные запасы'),
    'Асосий воситаларга харажат': ('category', 'Основные средства'), 'Банк хизмати': ('category', 'Услуги банка'),
    'Кредит': ('category', 'Погашение кредита'), 'Бошқалар': ('category', 'Прочие расходы'),
    'Ичимлик суви': ('category', 'Питьевая вода'), 'Алоқа': ('category', 'Связь'), 'Қўриқлаш': ('category', 'Охрана'),
    'Чиқинди': ('category', 'Вывоз мусора'), 'Нотўғри тўлов': ('category', 'Возврат ошибочного платежа'),
    TUITION: ('category', 'Возвраты родителям'),
    'Ҳисоб рақамни тўлдириш': ('transfer', None, Account.KIND_BANK),        # откуда — по виду расхода
    'Банкдан олинган нақд пул': ('transfer', Account.KIND_BANK, Account.KIND_CASH),
    'Терминал': ('transfer', Account.KIND_CARD, Account.KIND_BANK),
}
NORMALIZED_INCOME = {normalize_header(k): v for k, v in INCOME_MAP.items()}
NORMALIZED_EXPENSE = {normalize_header(k): v for k, v in EXPENSE_MAP.items()}
REFUND_CATEGORY = ('Возвраты родителям', 'Ota-onalarga qaytarishlar', 'Refunds to parents', '보호자 환불')

REASON_WORDS = [
    ('SIBLING_3', ('3farzand', '3chifarzand')),
    ('SIBLING_2', ('2farzand', '2chifarzand')),
    ('SOCIAL', ('ijtimoiyreestr', 'ijtimoiy')),
    ('STAFF_CHILD', ('hodim', 'xodim')),
    ('PRIVILEGE', ('imtiyoz',)),
]


# --- чтение книги

def _plain(value):
    """Значение ячейки → то, что можно хранить в сессии (JSON)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _header_map(spec, row):
    lookup = {}
    for key, variants in spec['columns'].items():
        for variant in variants:
            lookup.setdefault(normalize_header(variant), key)
    mapping = {}
    for position, value in enumerate(row):
        key = lookup.get(normalize_header(value)) if value else None
        if key and key not in mapping.values():
            mapping[position] = key
    return mapping


def read_workbook(uploaded):
    """Книга → {'contracts': [...], 'income': [...], 'expense': [...]}; строка — словарь с номером строки '_row'."""
    import openpyxl
    if uploaded.size > MAX_FILE_SIZE:
        raise ImportFileError(_("Файл больше 20 МБ."))
    if not (uploaded.name or '').lower().endswith(('.xlsx', '.xlsm')):
        raise ImportFileError(_("Нужна книга Excel (.xlsx)."))
    try:
        workbook = openpyxl.load_workbook(uploaded, read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise ImportFileError(_("Не удалось открыть файл как таблицу Excel (.xlsx).")) from exc
    try:
        by_name = {normalize_header(ws.title): ws for ws in workbook.worksheets}
        missing_sheets = [spec['names'][0] for spec in SHEETS.values()
                          if not any(normalize_header(n) in by_name for n in spec['names'])]
        if missing_sheets:
            raise ImportFileError(_("В книге нет листов: %(sheets)s.") % {'sheets': ', '.join(f'«{n}»' for n in missing_sheets)})
        result = {}
        for kind, spec in SHEETS.items():
            sheet = next(by_name[normalize_header(n)] for n in spec['names'] if normalize_header(n) in by_name)
            rows = list(sheet.iter_rows(values_only=True))
            header_index, mapping = None, {}
            for index, row in enumerate(rows[:10]):
                candidate = _header_map(spec, row)
                if len(candidate) > len(mapping):
                    header_index, mapping = index, candidate
            missing = [spec['columns'][k][0] for k in spec['required'] if k not in mapping.values()]
            if header_index is None or missing:
                raise ImportFileError(_("На листе «%(sheet)s» нет колонок: %(columns)s.")
                                      % {'sheet': sheet.title, 'columns': ', '.join(missing or ['—'])})
            items = []
            for index in range(header_index + 1, len(rows)):
                row = rows[index]
                values = {key: _plain(row[pos]) if pos < len(row) else None for pos, key in mapping.items()}
                if values.get(spec['key']) in (None, ''):
                    continue
                values['_row'] = index + 1
                items.append(values)
            result[kind] = items
        return result
    finally:
        workbook.close()


# --- разбор значений

def _text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _decimal(value):
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    try:
        return excel_import.parse_amount(str(value))
    except excel_import.RowError:
        return None


def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        try:
            return excel_import.parse_date(str(value))
        except excel_import.RowError:
            return None


def _method(text):
    word = normalize_header(text or '')
    for kind, words in excel_import.ACCOUNT_WORDS.items():
        if word in words or any(word.startswith(w) for w in words if len(w) >= 5):
            return kind
    return Account.KIND_CASH


PAYMENT_METHOD = {Account.KIND_CASH: 'CASH', Account.KIND_BANK: 'BANK', Account.KIND_CARD: 'CARD'}


def contract_number(prefix, value):
    return f"{prefix}{_text(value)}" if _text(value) else ''


def discount_parts(fee, pct_value, total, food):
    """Скидка книги → (процент, фиксированная сумма). Итог договора из книги («Сумма») главнее процента."""
    pct = Decimal(0)
    if pct_value not in (None, ''):
        pct = _decimal(pct_value) or Decimal(0)
        if pct <= 1:
            pct = pct * 100           # в книге 10 % записано как 0,1
    pct = pct.quantize(Decimal(1))
    if fee is None or total is None:
        return int(pct), Decimal(0)
    target = fee + (food or 0) - total
    pct_part = fee * pct / 100
    if target < 0:
        return int(pct), Decimal(0)
    if abs(target - pct_part) < Decimal('0.5'):
        return int(pct), Decimal(0)
    if pct_part <= target:
        return int(pct), (target - pct_part).quantize(Decimal('0.01'))
    return 0, target.quantize(Decimal('0.01'))


def book_phones(text):
    """Ячейка телефонов из книги → (основной, дополнительный, нераспознанные).

    В книгах встречаются «901234567\n907654321», «911112233, 331112233», «9xxxxxxxx.xxxxxx» (два номера через точку),
    лишняя цифра. Берём правильные номера, остальные возвращаем, чтобы сохранить их в примечании.
    """
    parts = [part.strip() for part in re.split(r'[\n,;/]+|\.(?=\d{5,})', text or '') if part.strip()]
    valid, invalid = [], []
    for part in parts:
        phone = normalize_phone(part)
        (valid if is_valid_uz_phone(phone) else invalid).append(phone if is_valid_uz_phone(phone) else part)
    return (valid[0] if valid else (text or '').strip()), (valid[1] if len(valid) > 1 else ''), invalid + valid[2:]


def discount_reason(notes):
    words = normalize_header(notes or '')
    for code, keys in REASON_WORDS:
        if any(k in words for k in keys):
            return code
    return ''


# --- проверка

# Колонки договора, ошибку в которых можно пропустить с пометкой; в остальных (номер, ФИО, школа, дата, стоимость,
# учебный год) без исправления книги договор не внести
FLAGGABLE = {
    'student_birth_year', 'student_class', 'duration_months', 'parents_full_name', 'passport_data', 'inps', 'address',
    'phone_number', 'phone_number_2', 'specialization', 'discount_percentage', 'discount_fixed', 'discount_reason',
    'food_cost',
}


def months_to_year_end(contract):
    """Сколько месяцев от первого месяца обучения до конца учебного года (срок вместо пустого)."""
    start, end = contract.first_billing_month, contract.academic_year.end_month if contract.academic_year else None
    if end is None or end < start:
        return 12
    return (end.year - start.year) * 12 + end.month - start.month + 1


class Item(RowResult):
    def __init__(self, sheet, number):
        super().__init__(number)
        self.sheet = sheet
        self.action = None
        self.payload = {}
        self.problems = []          # что не так в книге — строка всё равно загружается, с красной пометкой
        self.instance = None        # договор, собранный вручную из строки с ошибками
        self.clear_problem = None   # уже загруженная запись с пометкой, которую исправленная строка подтверждает

    @property
    def flagged(self):
        return self.status == RowResult.OK and bool(self.problems)

    @property
    def fixes_contract(self):
        return self.status == RowResult.OK and self.instance is None and self.form is not None and self.form.instance.pk

    def problem_text(self):
        return '\n'.join(self.problems)


class WorkbookPlan:
    """Что будет загружено: проверка всех строк книги без сохранения."""

    def __init__(self, user, school, year, prefix, sheets):
        self.user, self.school, self.year, self.prefix, self.sheets = user, school, year, prefix, sheets
        months = financial_months(year)
        self.window = (months[0], shift_month(months[-1], 1) - timedelta(days=1))
        self.items = {'contracts': [], 'income': [], 'expense': []}
        self.mapping = defaultdict(lambda: {'rows': 0, 'amount': Decimal(0)})
        self.new_founders = set()
        self.contract_numbers = {}       # номер → 'new' | Contract | None (ошибка)

    def run(self):
        self.check_contracts()
        # Снятие наличных из банка в книге бывает записано дважды: расходом банка и приходом кассы
        cash_out = normalize_header('Банкдан олинган нақд пул')
        self.unmatched_bank_out = [r for r in self.sheets['expense'] if normalize_header(r.get('category') or '') == cash_out]
        self.check_money('income', self.sheets['income'])
        self.check_money('expense', self.sheets['expense'])
        self.apply_existing()
        return self

    # договоры
    def check_contracts(self):
        ctx = excel_import.Context(self.user)
        for row in self.sheets['contracts']:
            item = Item('contracts', row['_row'])
            number = contract_number(self.prefix, row.get('number'))
            other = Contract.objects.filter(number=number).exclude(school=self.school).first() if number else None
            if other:
                item.errors.append(_("номер %(number)s уже занят договором другой школы — задайте префикс номеров")
                                   % {'number': number})
                item.status = RowResult.ERROR
                item.summary = [number, _text(row.get('student'))]
                self.contract_numbers[number] = None
                self.items['contracts'].append(item)
                continue
            existing = Contract.objects.filter(number=number, school=self.school).first() if number else None
            fixing = existing if existing and existing.data_problem else None
            fee = _decimal(row.get('fee'))
            pct, fixed = discount_parts(fee, row.get('discount_pct'), _decimal(row.get('total')), _decimal(row.get('food')))
            reason = discount_reason(row.get('notes')) if (pct or fixed) else ''
            values = {
                'number': number, 'date': _text(row.get('date')), 'student_full_name': _text(row.get('student')),
                'student_birth_year': _text(row.get('birth_year')), 'student_class': _text(row.get('grade')),
                'duration_months': _text(row.get('duration')), 'specialization': _text(row.get('specialization')),
                'parents_full_name': _text(row.get('parents')), 'passport_data': _text(row.get('passport')),
                'inps': _text(row.get('inps')), 'address': _text(row.get('address')), 'phone_number': _text(row.get('phone')),
                'tuition_fee': _text(row.get('fee')), 'discount_percentage': str(pct) if pct else '',
                'discount_fixed': str(fixed) if fixed else '', 'food_cost': _text(row.get('food')),
                'discount_reason': dict(Contract.DISCOUNT_REASON_CHOICES).get(reason, '') if reason else '',
                'cancelled': 'да' if normalize_header(row.get('status') or '').startswith('отмен') else '',
                'notes': _text(row.get('notes')), 'academic_year': self.year.name, 'school': self.school.name,
            }
            main, extra, unknown = book_phones(_text(row.get('phone')))
            values['phone_number'], values['phone_number_2'] = main, extra
            if unknown and is_valid_uz_phone(main):
                values['notes'] = (values['notes'] + ' ' + _("[Телефон из книги не распознан: %(phones)s]")
                                   % {'phones': ', '.join(unknown)}).strip()
                item.notes.append(_("Телефон «%(phones)s» не распознан — сохранён в примечании.") % {'phones': ', '.join(unknown)})
            values = {k: str(v) for k, v in values.items()}
            excel_import.check_contract(values, ctx, item, instance=fixing)
            if item.errors and fixing:
                # В книге ошибка осталась — загруженный договор с пометкой не трогаем
                item.errors, item.status = [], RowResult.EXISTS
                item.notes.append(_("Договор уже загружен с красной пометкой, в книге ошибка осталась — пропущено."))
            elif item.errors:
                self.flag_contract(item, values, ctx)
            if item.errors:
                item.status = RowResult.ERROR
            if not item.summary:
                item.summary = [number, values['student_full_name']]
            if item.status == RowResult.OK:
                self.contract_numbers[number] = 'new'
            elif item.status == RowResult.EXISTS:
                self.contract_numbers[number] = existing
            else:
                self.contract_numbers.setdefault(number, None)
            self.items['contracts'].append(item)

    def flag_contract(self, item, values, ctx):
        """Договор с ошибками только в заменяемых колонках → собрать вручную и записать в пометку, что не так."""
        form = item.form
        if form is None or not item.bad_keys or not item.bad_keys <= FLAGGABLE:
            return
        obj = form.instance        # правильные колонки форма уже перенесла в договор
        obj.user = ctx.user
        if ctx.user.role != 'OWNER':
            obj.school = ctx.user.school
        if not obj.school_id:
            return
        if values.get('cancelled'):
            obj.is_cancelled = True
        labels = {str(c.label): c.key for c in excel_import.KINDS['contracts']['columns']}
        problems = []
        for error in item.errors:
            match = re.match(r'«(.+?)»:', error)
            key = labels.get(match.group(1)) if match else None
            raw = (values.get(key) or '').strip() if key else ''
            if key == 'duration_months':
                obj.duration_months = months_to_year_end(obj)
                fix = _("поставлено %(months)s мес. — до конца учебного года") % {'months': obj.duration_months}
            elif key == 'student_birth_year':
                obj.student_birth_year = None
                fix = _("оставлено пустым")
            else:
                field = Contract._meta.get_field(key)
                if raw and field.get_internal_type() in ('CharField', 'TextField') and not field.choices:
                    setattr(obj, key, raw[:field.max_length] if field.max_length else raw)
                    fix = _("сохранено как в книге: «%(value)s»") % {'value': raw}
                else:
                    setattr(obj, key, field.get_default())
                    fix = _("оставлено по умолчанию")
            problems.append(_("строка %(row)s: %(error)s — %(fix)s") % {'row': item.number, 'error': error, 'fix': fix})
        item.problems = problems
        item.instance = obj
        item.errors = []
        item.notes.append(_("Загрузится с красной пометкой — потом исправьте договор."))

    # деньги
    def in_window(self, item, day):
        """Дата строки внутри финансового года; дата не того года (опечатка в годе) исправляется с пометкой."""
        if day is None:
            item.errors.append(_("не понятна дата"))
            return None
        if self.window[0] <= day <= self.window[1]:
            return day
        for year in sorted({self.window[0].year, self.window[1].year}):
            try:
                fixed = day.replace(year=year)
            except ValueError:      # 29 февраля
                continue
            if self.window[0] <= fixed <= self.window[1]:
                item.problems.append(_("строка %(row)s: в книге дата %(day)s — не тот учебный год, поставлено %(fixed)s, проверьте")
                                     % {'row': item.number, 'day': day.strftime('%d.%m.%Y'), 'fixed': fixed.strftime('%d.%m.%Y')})
                return fixed
        item.errors.append(_("дата %(day)s вне учебного года %(year)s — проверьте") % {
            'day': day.strftime('%d.%m.%Y'), 'year': self.year.name})
        return None

    def target(self, kind, row):
        """Что это за строка: (действие, параметры, подпись для таблицы соответствия)."""
        text = _text(row.get('category'))
        table = NORMALIZED_INCOME if kind == 'income' else NORMALIZED_EXPENSE
        if not text:
            if kind == 'income' and _text(row.get('contract')):
                return ('tuition',), TUITION
            return ('category', 'Прочие поступления' if kind == 'income' else 'Прочие расходы'), '—'
        mapped = table.get(normalize_header(text))
        if mapped:
            return mapped, text
        direction = 'INCOME' if kind == 'income' else 'EXPENSE'
        category = Category.find(text, direction)
        if category:
            return ('category', category.name), text
        # Незнакомое имя — это учредитель/инвестор (в книге так записаны вложения и выплаты им)
        return ('founder', text), text

    def check_money(self, kind, rows):
        for row in rows:
            item = Item(kind, row['_row'])
            self.items[kind].append(item)
            day = _date(row.get('date'))
            amount = _decimal(row.get('amount'))
            if amount is None or amount <= 0:
                item.errors.append(_("не понятна сумма «%(value)s»") % {'value': _text(row.get('amount'))})
            day = self.in_window(item, day)
            target, label = self.target(kind, row)
            account_kind = _method(row.get('method'))
            purpose = ' / '.join(t for t in (_text(row.get('purpose')), _text(row.get('notes'))) if t)
            item.summary = [day.strftime('%d.%m.%Y') if day else _text(row.get('date')), label, excel_import.money(amount or 0)]
            if item.errors:
                item.status = RowResult.ERROR
                continue
            self.mapping[(kind, label)]['rows'] += 1
            self.mapping[(kind, label)]['amount'] += amount
            action = target[0]
            if action == 'tuition':
                self.plan_tuition(item, row, day, amount, account_kind, purpose)
            elif action == 'bank_cash_in':
                match = next((r for r in self.unmatched_bank_out if _decimal(r.get('amount')) == amount
                              and _date(r.get('date')) and abs((_date(r.get('date')) - day).days) <= 7), None)
                if match:
                    self.unmatched_bank_out.remove(match)
                    item.status = RowResult.SKIPPED
                    item.notes.append(_("Вторая сторона перевода банк → касса (строка %(row)s листа «Расходы ДС») — не дублируется.")
                                      % {'row': match['_row']})
                    self.mapping[(kind, label)]['target'] = _('перевод банк → касса')
                else:
                    item.action = 'transfer'
                    item.payload = {'from': Account.KIND_BANK, 'to': Account.KIND_CASH, 'date': day, 'amount': amount, 'notes': purpose}
                    self.mapping[(kind, label)]['target'] = _('перевод банк → касса')
            elif action == 'transfer':
                source = target[1] or account_kind
                if source == target[2]:
                    source = Account.KIND_CASH if target[2] != Account.KIND_CASH else Account.KIND_BANK
                item.action = 'transfer'
                item.payload = {'from': source, 'to': target[2], 'date': day, 'amount': amount, 'notes': purpose}
                self.mapping[(kind, label)]['target'] = _('перевод: %(from)s → %(to)s') % {
                    'from': dict(Account.KIND_CHOICES)[source].lower(), 'to': dict(Account.KIND_CHOICES)[target[2]].lower()}
            else:
                direction = 'INCOME' if kind == 'income' else 'EXPENSE'
                if action == 'founder':
                    category_name = 'Вложения учредителей' if kind == 'income' else 'Выплаты учредителям'
                    founder = target[1]
                    if not Counterparty.objects.filter(name__iexact=founder).exists():
                        self.new_founders.add(founder)
                else:
                    category_name, founder = target[1], ''
                item.action = 'transaction'
                item.payload = {'category': category_name, 'direction': direction, 'founder': founder, 'date': day,
                                'amount': amount, 'account': account_kind, 'notes': purpose}
                self.mapping[(kind, label)]['target'] = category_name + (f" · {founder}" if founder else '')

    def plan_tuition(self, item, row, day, amount, account_kind, purpose):
        number = contract_number(self.prefix, row.get('contract'))
        if not number:
            item.action = 'receipt'
            item.payload = {'date': day, 'amount': amount, 'account': account_kind, 'payer': purpose[:255]}
            item.notes.append(_("Без номера договора — станет нераспознанным поступлением, его можно будет разобрать."))
            self.mapping[(item.sheet, TUITION)]['target'] = _('оплата за обучение')
            return
        if number not in self.contract_numbers:
            # Договор не из этой книги — может быть уже на платформе
            self.contract_numbers[number] = Contract.objects.filter(number=number, school=self.school).first()
        state = self.contract_numbers[number]
        if state is None:
            item.errors.append(_("договор № %(number)s не загружается или не найден") % {'number': number})
            item.status = RowResult.ERROR
            return
        try:
            for_month = parse_month(_text(row.get('period'))) if row.get('period') else day.replace(day=1)
        except excel_import.RowError:
            item.errors.append(_("не понятен месяц «%(value)s»") % {'value': _text(row.get('period'))})
            item.status = RowResult.ERROR
            return
        item.action = 'payment'
        item.payload = {'number': number, 'for_month': for_month, 'date': day, 'amount': amount,
                        'method': PAYMENT_METHOD.get(account_kind, 'CASH'), 'account': account_kind, 'notes': purpose}
        self.mapping[(item.sheet, TUITION)]['target'] = _('оплата за обучение')

    # уже внесённое
    def key(self, item):
        p = item.payload
        if item.action == 'payment':
            return ('payment', p['number'], p['for_month'], p['amount'], p['date'])
        if item.action == 'receipt':
            return ('receipt', p['date'], p['amount'], p['payer'])
        if item.action == 'transaction':
            return ('transaction', p['category'], p['founder'], p['date'], p['amount'])
        return ('transfer', p['from'], p['to'], p['date'], p['amount'])

    def existing_keys(self):
        """Какие такие записи уже есть на платформе (для повторной загрузки): ключ → список записей."""
        found = defaultdict(list)
        start, end = self.window
        for p in Payment.active.filter(contract__school=self.school, kind=Payment.KIND_PAYMENT,
                                       payment_date__range=(start, end)).select_related('contract'):
            found[('payment', p.contract.number, p.for_month, p.amount, p.payment_date)].append(p)
        for r in UnidentifiedReceipt.active.filter(school=self.school, date__range=(start, end)):
            found[('receipt', r.date, r.amount, r.payer)].append(r)
        for t in Transaction.active.filter(school=self.school, date__range=(start, end)).select_related('category', 'counterparty'):
            found[('transaction', t.category.name if t.category else '', t.counterparty.name if t.counterparty else '',
                   t.date, t.amount)].append(t)
        for t in Transfer.active.filter(school=self.school, date__range=(start, end)).select_related('from_account', 'to_account'):
            found[('transfer', t.from_account.kind, t.to_account.kind, t.date, t.amount)].append(t)
        return found

    def apply_existing(self):
        found = self.existing_keys()
        for kind in ('income', 'expense'):
            for item in self.items[kind]:
                if item.status != RowResult.OK or not item.action:
                    continue
                records = found.get(self.key(item))
                if records:
                    # Строка с ошибкой ↔ запись с пометкой, исправленная строка ↔ запись без пометки — если есть
                    records.sort(key=lambda r: bool(r.data_problem) != bool(item.problems))
                    record = records.pop(0)
                    item.status = RowResult.EXISTS
                    item.notes.append(_("Уже есть на платформе — пропущено."))
                    if record.data_problem and not item.problems:
                        item.clear_problem = record
                        item.notes.append(_("В книге строка исправлена — красная пометка будет снята."))

    # итоги для предпросмотра
    def counts(self):
        result = {}
        for kind, items in self.items.items():
            result[kind] = {s: sum(1 for i in items if i.status == s) for s in ('ok', 'exists', 'skipped', 'error')}
            result[kind]['flagged'] = sum(1 for i in items if i.flagged)
            result[kind]['fixed'] = sum(1 for i in items if i.fixes_contract or i.clear_problem is not None)
        return result

    def problems(self):
        return [i for kind in ('contracts', 'income', 'expense') for i in self.items[kind] if i.errors]

    def flagged(self):
        return [i for kind in ('contracts', 'income', 'expense') for i in self.items[kind] if i.flagged]

    def mapping_rows(self):
        rows = []
        for (kind, label), info in sorted(self.mapping.items(), key=lambda kv: (kv[0][0] != 'income', -kv[1]['amount'])):
            rows.append({'sheet': kind, 'label': label, 'target': info.get('target', ''), 'rows': info['rows'],
                         'amount': info['amount']})
        return rows

    # сохранение
    def save(self, filename):
        created = Counter()
        with db_transaction.atomic():
            contracts = {}
            for item in self.items['contracts']:
                if item.status == RowResult.OK:
                    if item.instance is not None:
                        obj = item.instance
                    else:
                        obj = item.form.save(commit=False)
                        item.prepare(obj)
                    fixing = obj.pk is not None
                    obj.data_problem = item.problem_text()
                    obj.save()
                    contracts[obj.number] = obj
                    details = {'source': 'workbook', 'file': filename[:100], 'row': item.number}
                    if obj.data_problem:
                        details['data_problem'] = obj.data_problem
                        created['flagged'] += 1
                    AuditLog.record(self.user, 'UPDATE' if fixing else 'CREATE', obj, details)
                    created['fixed' if fixing else 'contracts'] += 1
            for number, state in self.contract_numbers.items():
                if isinstance(state, Contract):
                    contracts.setdefault(number, state)
            if any(i.status == RowResult.OK and i.payload.get('category') == REFUND_CATEGORY[0]
                   for i in self.items['expense']):
                name, uz, en, ko = REFUND_CATEGORY
                Category.objects.get_or_create(name=name, direction='EXPENSE', defaults={
                    'name_uz': uz, 'name_en': en, 'name_ko': ko, 'sort_order': 165})
            for kind in ('income', 'expense'):
                for item in self.items[kind]:
                    if item.clear_problem is not None:
                        record = item.clear_problem
                        AuditLog.record(self.user, 'UPDATE', record, {'source': 'workbook', 'file': filename[:100],
                                                                      'data_problem': [record.data_problem, '']})
                        record.data_problem = ''
                        record.save(update_fields=['data_problem', 'updated_at'])
                        created['fixed'] += 1
                    if item.status != RowResult.OK or not item.action:
                        continue
                    obj = self.create(item, contracts)
                    if item.problems:
                        obj.data_problem = item.problem_text()
                        obj.save(update_fields=['data_problem'])
                        created['flagged'] += 1
                    AuditLog.record(self.user, 'CREATE', obj, {'source': 'workbook', 'file': filename[:100],
                                                               'sheet': kind, 'row': item.number})
                    created[item.action] += 1
        return created

    def account(self, kind):
        return Account.default_for(self.school, kind)

    def create(self, item, contracts):
        p = item.payload
        if item.action == 'payment':
            return Payment.objects.create(
                contract=contracts[p['number']], for_month=p['for_month'], amount=p['amount'], payment_date=p['date'],
                method=p['method'], account=self.account(p['account']), notes=p['notes'], created_by=self.user)
        if item.action == 'receipt':
            return UnidentifiedReceipt.objects.create(school=self.school, account=self.account(p['account']), date=p['date'],
                                                      amount=p['amount'], payer=p['payer'], created_by=self.user)
        if item.action == 'transfer':
            return Transfer.objects.create(school=self.school, from_account=self.account(p['from']),
                                           to_account=self.account(p['to']), amount=p['amount'], date=p['date'],
                                           notes=p['notes'], created_by=self.user)
        category = Category.objects.filter(name=p['category'], direction=p['direction']).first() or Category.other(p['direction'])
        counterparty = None
        if p['founder']:
            counterparty = Counterparty.objects.filter(name__iexact=p['founder']).first() or \
                Counterparty.objects.create(name=p['founder'], kind='FOUNDER')
        return Transaction.objects.create(school=self.school, category=category, type=p['direction'],
                                          account=self.account(p['account']), counterparty=counterparty, amount=p['amount'],
                                          date=p['date'], notes=p['notes'], created_by=self.user)


def build_plan(user, school, year, prefix, sheets):
    return WorkbookPlan(user, school, year, prefix, sheets).run()


# --- сверка

def reconcile(plan):
    """Сверка итогов книги с тем, что теперь на платформе. Возвращает список проверок и расхождения по договорам."""
    school, prefix = plan.school, plan.prefix
    book = plan.sheets
    checks = []

    numbers = [contract_number(prefix, r.get('number')) for r in book['contracts']]
    numbers = [n for n in numbers if n]
    platform_contracts = list(Contract.objects.filter(school=school, number__in=numbers))
    loaded = {c.number for c in platform_contracts}
    is_cancelled = lambda r: normalize_header(r.get('status') or '').startswith('отмен')
    loaded_rows = [r for r in book['contracts'] if contract_number(prefix, r.get('number')) in loaded]
    skipped_rows = len(numbers) - len(loaded_rows)
    checks.append({'label': _('Договоров'), 'book': len(numbers), 'platform': len(platform_contracts),
                   'note': _('не загружено строк с ошибками: %(count)s') % {'count': skipped_rows} if skipped_rows else ''})
    # Остальное сравниваем по загруженным договорам — расхождение тогда значит ошибку в разноске
    checks.append({'label': _('Из них отменённых'), 'book': sum(1 for r in loaded_rows if is_cancelled(r)),
                   'platform': sum(1 for c in platform_contracts if c.is_cancelled)})
    checks.append({'label': _('Сумма в месяц по действующим договорам'),
                   'book': sum((_decimal(r.get('total')) or Decimal(0) for r in loaded_rows if not is_cancelled(r)), Decimal(0)),
                   'platform': sum((c.total_amount for c in platform_contracts if not c.is_cancelled), Decimal(0)), 'money': True})

    # Оплаты за обучение по месяцам оплаты («Оплата за период»)
    book_tuition = defaultdict(Decimal)
    book_by_contract = defaultdict(Decimal)
    for item in plan.items['income']:
        if item.action == 'payment' and item.status in (RowResult.OK, RowResult.EXISTS):
            book_tuition[item.payload['for_month']] += item.payload['amount']
            book_by_contract[item.payload['number']] += item.payload['amount']
    platform_tuition = defaultdict(Decimal)
    platform_by_contract = defaultdict(Decimal)
    for p in Payment.active.filter(contract__in=platform_contracts).select_related('contract'):
        platform_tuition[p.for_month.replace(day=1)] += p.signed_amount
        platform_by_contract[p.contract.number] += p.signed_amount
    for month in sorted(set(book_tuition) | set(platform_tuition)):
        checks.append({'label': _('Оплаты за %(month)s') % {'month': month.strftime('%m.%Y')},
                       'book': book_tuition[month], 'platform': platform_tuition[month], 'money': True})

    # Деньги по месяцам (без переводов между своими счетами)
    start, end = plan.window
    book_in, book_out = defaultdict(Decimal), defaultdict(Decimal)
    for kind, target in (('income', book_in), ('expense', book_out)):
        for item in plan.items[kind]:
            if item.status in (RowResult.OK, RowResult.EXISTS) and item.action and item.action != 'transfer':
                target[item.payload['date'].replace(day=1)] += item.payload['amount']
    platform_in, platform_out = defaultdict(Decimal), defaultdict(Decimal)
    for p in Payment.active.filter(contract__school=school, payment_date__range=(start, end)):
        (platform_in if not p.is_refund else platform_out)[p.payment_date.replace(day=1)] += p.amount
    for r in UnidentifiedReceipt.active.filter(school=school, date__range=(start, end), payment__isnull=True):
        platform_in[r.date.replace(day=1)] += r.amount
    for t in Transaction.active.filter(school=school, date__range=(start, end)):
        (platform_in if t.type == 'INCOME' else platform_out)[t.date.replace(day=1)] += t.amount
    for p in EmployeePayout.active.filter(school=school, payout_date__range=(start, end)):
        platform_out[p.payout_date.replace(day=1)] += p.amount
    for month in sorted(set(book_in) | set(book_out) | set(platform_in) | set(platform_out)):
        checks.append({'label': _('Поступления за %(month)s') % {'month': month.strftime('%m.%Y')},
                       'book': book_in[month], 'platform': platform_in[month], 'money': True})
        checks.append({'label': _('Расходы за %(month)s') % {'month': month.strftime('%m.%Y')},
                       'book': book_out[month], 'platform': platform_out[month], 'money': True})

    for check in checks:
        check['diff'] = check['platform'] - check['book']
        check['ok'] = check['diff'] == 0
        # Разница только из-за строк с ошибками — это не ошибка разноски
        check['explained'] = not check['ok'] and bool(check.get('note')) and -check['diff'] == skipped_rows
        check.setdefault('note', '')
    mismatches = [{'number': n, 'book': book_by_contract[n], 'platform': platform_by_contract[n]}
                  for n in sorted(set(book_by_contract) | set(platform_by_contract), key=lambda x: (len(x), x))
                  if book_by_contract[n] != platform_by_contract[n]]
    return {'checks': checks, 'mismatches': mismatches[:100],
            'ok': all(c['ok'] or c['explained'] for c in checks) and not mismatches,
            'errors': len(plan.problems())}

