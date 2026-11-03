"""Загрузка договоров, оплат, выплат и транзакций из таблиц Excel (.xlsx) и CSV.

Как это работает:
1. read_table() читает файл в список строк (все значения — строки), find_columns() находит строку
   с названиями колонок — на любом из языков интерфейса, в том числе как в выгрузке «⬇ Excel».
2. check_rows() проверяет каждую строку теми же формами, что и ручной ввод, и ничего не сохраняет —
   по результату строится предпросмотр.
3. save_rows() сохраняет только правильные строки, все вместе в одной транзакции, с записью в журнал.

Строки, которые уже есть на платформе (договор с тем же номером, такая же оплата, выплата или
транзакция), пропускаются: повторная загрузка того же файла ничего не задваивает.
"""
import csv
import io
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import cache

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import translation
from django.utils.dates import MONTHS, MONTHS_3, MONTHS_ALT
from django.utils.translation import gettext as _, gettext_lazy

from .finance import format_russian_month
from .forms import ContractForm, PaymentForm, EmployeePayoutForm, TransactionForm
from .models import Account, AcademicYear, AuditLog, Category, Counterparty, Contract, Staff, EmployeePayout, Payment, School, Transaction, User
from .phone import normalize_phone, is_valid_uz_phone, format_phone
from .templatetags.money import money as format_money

MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_ROWS = 2000
HEADER_SEARCH_ROWS = 10


class ImportFileError(Exception):
    """Файл целиком не подходит (не открывается, нет нужных колонок)."""


class Column:
    def __init__(self, key, label, field=None, model=None, required=False, owner_only=False,
                 aliases=(), hint='', width=18, text=False):
        self.key = key
        self.label = label
        self.field = field          # поле формы, ошибки которого относятся к этой колонке
        self.model = model          # подпись поля модели тоже узнаётся как название колонки
        self.required = required
        self.owner_only = owner_only
        self.aliases = aliases      # дополнительные русские варианты названия
        self.hint = hint
        self.width = width
        self.text = text            # в шаблоне — текстовый формат (чтобы Excel не портил номера)

    def is_required(self, user):
        return self.required and (not self.owner_only or user.role == 'OWNER')

    def names(self):
        """Все варианты названия колонки, по которым она узнаётся в файле."""
        names = {normalize_header(self.key)}
        names.update(normalize_header(a) for a in self.aliases)
        for code, _name in settings.LANGUAGES:
            with translation.override(code):
                names.add(normalize_header(self.label))
                if self.model and self.field:
                    names.add(normalize_header(self.model._meta.get_field(self.field).verbose_name))
        names.discard('')
        return names


SCHOOL_HINT = gettext_lazy("Название школы, как на платформе. Заполняет только владелец — директору и сотруднику подставляется их школа.")
NOTES_HINT = gettext_lazy("Необязательно.")
DATE_HINT = gettext_lazy("Например: 01.09.2026")
MONTH_HINT = gettext_lazy("Например: 09.2026 или Сентябрь 2026")
AMOUNT_HINT = gettext_lazy("Число в сумах, например 1500000")
ACCOUNT_HINT = gettext_lazy("Касса, расчётный счёт или терминал (можно «наличные», «перечисление»). Пусто — касса.")
REVERSED_HINT = gettext_lazy("Есть в выгрузке с платформы. Строки с отметкой «да» не загружаются.")

KINDS = {
    'contracts': {
        'title': gettext_lazy("Договоры"),
        'roles': ('OWNER', 'DIRECTOR', 'EMPLOYEE'),
        'list_url': 'contract_list',
        'filename': 'shablon_dogovory',
        'columns': [
            Column('number', gettext_lazy('№ договора'), 'number', Contract, required=True, text=True,
                   hint=gettext_lazy("Уникальный номер. Если договор с таким номером уже есть, строка пропускается.")),
            Column('school', gettext_lazy('Школа'), 'school', Contract, required=True, owner_only=True, hint=SCHOOL_HINT, width=24),
            Column('date', gettext_lazy('Дата'), 'date', Contract, required=True, aliases=('Дата договора',),
                   hint=gettext_lazy("Дата подписания договора. Например: 01.06.2026")),
            Column('academic_year', gettext_lazy('Учебный год'), 'academic_year', Contract, width=12,
                   hint=gettext_lazy("Например: 2026–2027. Пусто — определится по дате договора.")),
            Column('billing_start', gettext_lazy('Первый месяц обучения'), 'billing_start', Contract, aliases=('Обучение с',),
                   hint=gettext_lazy("С него начисляется оплата, например 09.2026. Пусто — первый месяц учебного года или месяц договора, если он позже.")),
            Column('student_full_name', gettext_lazy('ФИО ученика'), 'student_full_name', Contract, required=True,
                   aliases=('Ученик',), width=28),
            Column('student_birth_year', gettext_lazy('Год рождения'), 'student_birth_year', Contract, required=True,
                   hint=gettext_lazy("Например: 2015"), width=12),
            Column('student_class', gettext_lazy('Класс'), 'student_class', Contract, required=True, width=10),
            Column('duration_months', gettext_lazy('Срок, мес'), 'duration_months', Contract, required=True,
                   hint=gettext_lazy("Сколько месяцев начисляется оплата, например 9 или 12"), width=10),
            Column('specialization', gettext_lazy('Направление'), 'specialization', Contract, required=True,
                   hint=gettext_lazy("Русский язык или Узбекский язык (можно коротко: рус, узб)")),
            Column('parents_full_name', gettext_lazy('ФИО родителей'), 'parents_full_name', Contract, required=True,
                   aliases=('Родитель', 'ФИО родителя'), width=28),
            Column('passport_data', gettext_lazy('Паспорт'), 'passport_data', Contract, text=True,
                   hint=gettext_lazy("Серия и номер паспорта родителя, например AA1234567")),
            Column('inps', gettext_lazy('ИНПС'), 'inps', Contract, text=True,
                   hint=gettext_lazy("14 цифр. Можно оставить пустым — договор будет помечен «неполные данные».")),
            Column('address', gettext_lazy('Адрес'), 'address', Contract, width=30),
            Column('phone_number', gettext_lazy('Телефон'), 'phone_number', Contract, required=True, text=True,
                   aliases=('Телефон родителей', 'Телефон родителя'),
                   hint=gettext_lazy("Например: +998 90 123 45 67. Два номера в одной ячейке разделятся на основной и дополнительный.")),
            Column('phone_number_2', gettext_lazy('Доп. телефон'), 'phone_number_2', Contract, text=True),
            Column('tuition_fee', gettext_lazy('Стоимость'), 'tuition_fee', Contract, required=True,
                   hint=gettext_lazy("Стоимость учёбы в месяц, в сумах")),
            Column('discount_percentage', gettext_lazy('Скидка %'), 'discount_percentage', Contract,
                   hint=gettext_lazy("От 0 до 100. Пусто — без скидки."), width=10),
            Column('discount_fixed', gettext_lazy('Скидка суммой'), 'discount_fixed', Contract,
                   hint=gettext_lazy("Скидка в сумах в месяц. Можно вместе с процентом.")),
            Column('discount_reason', gettext_lazy('Причина скидки'), 'discount_reason', Contract, width=22,
                   hint=gettext_lazy("Второй ребёнок, третий ребёнок, социальный реестр, ребёнок сотрудника, льгота, другое.")),
            Column('food_cost', gettext_lazy('Питание'), 'food_cost', Contract,
                   hint=gettext_lazy("Стоимость питания в месяц. Пусто — без питания.")),
            Column('cancelled', gettext_lazy('Отменён'), 'is_cancelled', Contract,
                   hint=gettext_lazy("«да», если договор уже отменён. Дату отмены можно не указывать — тогда начисления закончатся последним оплаченным месяцем."), width=10),
            Column('cancelled_at', gettext_lazy('Дата отмены'), 'cancelled_at', Contract, hint=DATE_HINT),
            Column('notes', gettext_lazy('Примечание'), 'notes', Contract, hint=NOTES_HINT, width=30),
        ],
    },
    'payments': {
        'title': gettext_lazy("Оплаты"),
        'roles': ('OWNER', 'DIRECTOR', 'EMPLOYEE'),
        'list_url': 'payment_list',
        'filename': 'shablon_oplaty',
        'columns': [
            Column('contract', gettext_lazy('№ договора'), 'contract', required=True, text=True,
                   hint=gettext_lazy("Номер договора, который уже есть на платформе.")),
            Column('payment_date', gettext_lazy('Дата оплаты'), 'payment_date', Payment, required=True, hint=DATE_HINT),
            Column('for_month', gettext_lazy('За месяц'), 'for_month', Payment, aliases=('Месяц',),
                   hint=gettext_lazy("За какой месяц оплата, например 09.2026. Пусто — месяц даты оплаты.")),
            Column('amount', gettext_lazy('Сумма'), 'amount', Payment, required=True, hint=AMOUNT_HINT),
            Column('method', gettext_lazy('Способ оплаты'), 'method', Payment,
                   hint=gettext_lazy("Наличные, карта, Click, Payme, Uzum или перечисление. Пусто — наличные.")),
            Column('notes', gettext_lazy('Примечание'), 'notes', Payment, hint=NOTES_HINT, width=30),
        ],
    },
    'payouts': {
        'title': gettext_lazy("Выплаты сотрудникам"),
        'roles': ('OWNER', 'DIRECTOR'),
        'list_url': 'payout_list',
        'filename': 'shablon_vyplaty',
        'columns': [
            Column('employee', gettext_lazy('Сотрудник'), 'employee', EmployeePayout, required=True, width=30,
                   hint=gettext_lazy("Телефон сотрудника (надёжнее) или ФИО, как в справочнике сотрудников.")),
            Column('school', gettext_lazy('Школа'), 'school', EmployeePayout, owner_only=True, width=24,
                   hint=gettext_lazy("Только для владельца. Пусто — школа сотрудника.")),
            Column('for_month', gettext_lazy('За месяц'), 'for_month', EmployeePayout, required=True, hint=MONTH_HINT),
            Column('account', gettext_lazy('Счёт'), 'account', EmployeePayout, width=18, hint=ACCOUNT_HINT),
            Column('amount', gettext_lazy('Сумма'), 'amount', EmployeePayout, required=True, hint=AMOUNT_HINT),
            Column('payout_date', gettext_lazy('Дата выплаты'), 'payout_date', EmployeePayout, required=True, hint=DATE_HINT),
            Column('notes', gettext_lazy('Примечание'), 'notes', EmployeePayout, hint=NOTES_HINT, width=30),
            Column('reversed', gettext_lazy('Сторно'), hint=REVERSED_HINT, width=10),
        ],
    },
    'transactions': {
        'title': gettext_lazy("Транзакции"),
        'roles': ('OWNER', 'DIRECTOR'),
        'list_url': 'transaction_list',
        'filename': 'shablon_tranzakcii',
        'columns': [
            Column('date', gettext_lazy('Дата'), 'date', Transaction, required=True, hint=DATE_HINT),
            Column('school', gettext_lazy('Школа'), 'school', Transaction, required=True, owner_only=True, hint=SCHOOL_HINT, width=24),
            Column('type', gettext_lazy('Тип'), 'type', Transaction,
                   hint=gettext_lazy("Приход или Расход. Можно не заполнять, если указана статья.")),
            Column('category', gettext_lazy('Статья'), 'category', Transaction, width=26,
                   aliases=('Категория', 'Краткое назначение', 'Категория/Назначение'),
                   hint=gettext_lazy("Статья из справочника, например: Аренда, Зарплата, Вложения учредителей. Пусто — «Прочие».")),
            Column('account', gettext_lazy('Счёт'), 'account', Transaction, width=18,
                   aliases=('Вид поступление', 'Вид поступления', 'Вид расходы', 'Вид расхода'), hint=ACCOUNT_HINT),
            Column('counterparty', gettext_lazy('Контрагент'), 'counterparty', Transaction, width=22,
                   hint=gettext_lazy("Учредитель, банк или поставщик из справочника. Можно не заполнять.")),
            Column('amount', gettext_lazy('Сумма'), 'amount', Transaction, required=True, hint=AMOUNT_HINT),
            Column('notes', gettext_lazy('Примечание'), 'notes', Transaction, hint=NOTES_HINT, width=30,
                   aliases=('Назначение платежа',)),
            Column('reversed', gettext_lazy('Сторно'), hint=REVERSED_HINT, width=10),
        ],
    },
}


def kinds_for(user):
    return [key for key, kind in KINDS.items() if user.role in kind['roles']]


def columns_for(kind, user):
    """Колонки шаблона для пользователя: директору и сотруднику колонка «Школа» не нужна."""
    return [c for c in KINDS[kind]['columns'] if not c.owner_only or user.role == 'OWNER']


# --- чтение файла

def normalize_header(text):
    """Для сравнения названий: только буквы и цифры, без регистра — «№договора», «Срок мес» и «срок, мес.» совпадут."""
    return re.sub(r"[\W_ʻʼ‘’'`]+", '', str(text)).casefold()


def cell_to_str(value):
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(Decimal(str(value)), 'f')
    return str(value).strip()


def _read_xlsx(uploaded):
    import openpyxl
    try:
        workbook = openpyxl.load_workbook(uploaded, read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise ImportFileError(_("Не удалось открыть файл как таблицу Excel (.xlsx).")) from exc
    try:
        sheet = workbook.worksheets[0]
        rows = []
        for values in sheet.iter_rows(values_only=True):
            rows.append([cell_to_str(v) for v in values])
            if len(rows) > MAX_ROWS + HEADER_SEARCH_ROWS:
                break
        return rows
    finally:
        workbook.close()


def _read_csv(uploaded):
    raw = uploaded.read()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        # Excel на русской Windows сохраняет CSV в cp1251
        text = raw.decode('cp1251', errors='replace')
    first_line = text.split('\n', 1)[0]
    delimiter = max([';', ',', '\t'], key=first_line.count)
    rows = []
    for values in csv.reader(io.StringIO(text), delimiter=delimiter):
        rows.append([v.strip() for v in values])
        if len(rows) > MAX_ROWS + HEADER_SEARCH_ROWS:
            break
    return rows


def read_table(uploaded):
    """Файл → список строк таблицы (каждая строка — список строковых значений)."""
    name = (uploaded.name or '').lower()
    if uploaded.size > MAX_FILE_SIZE:
        raise ImportFileError(_("Файл больше 5 МБ. Разбейте таблицу на несколько файлов."))
    if name.endswith(('.xlsx', '.xlsm')):
        return _read_xlsx(uploaded)
    if name.endswith('.csv'):
        return _read_csv(uploaded)
    if name.endswith('.xls'):
        raise ImportFileError(_("Старый формат .xls не поддерживается. Откройте файл в Excel и сохраните как «Книга Excel (.xlsx)»."))
    raise ImportFileError(_("Нужен файл Excel (.xlsx) или CSV."))


def find_columns(kind, user, rows):
    """Находит строку с названиями колонок. Возвращает (номер строки заголовка, {индекс: ключ}, неизвестные колонки)."""
    columns = columns_for(kind, user)
    lookup = {}
    for column in columns:
        for name in column.names():
            lookup.setdefault(name, column.key)

    best = None
    for index, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        mapping = {}
        for position, value in enumerate(row):
            key = lookup.get(normalize_header(value))
            if key and key not in mapping.values():
                mapping[position] = key
        if len(mapping) >= 2 and (best is None or len(mapping) > len(best[1])):
            best = (index, mapping)
    if best is None:
        raise ImportFileError(_("Не нашли строку с названиями колонок. Скачайте шаблон и заполните его."))

    index, mapping = best
    found = set(mapping.values())
    missing = [str(c.label) for c in columns if c.is_required(user) and c.key not in found]
    if missing:
        raise ImportFileError(_("В файле нет обязательных колонок: %(columns)s.") % {'columns': ', '.join(missing)})
    ignored = [v for position, v in enumerate(rows[index]) if v and position not in mapping]
    return index, mapping, ignored


def extract_rows(kind, user, rows):
    """Строки файла → [(номер строки в Excel, {ключ колонки: значение})] и список неизвестных колонок."""
    header_index, mapping, ignored = find_columns(kind, user, rows)
    result = []
    for index in range(header_index + 1, len(rows)):
        row = rows[index]
        values = {key: (row[position] if position < len(row) else '') for position, key in mapping.items()}
        if any(values.values()):
            result.append((index + 1, values))
    if not result:
        raise ImportFileError(_("В файле нет строк с данными."))
    if len(result) > MAX_ROWS:
        raise ImportFileError(_("Слишком много строк: за один раз можно загрузить не больше %(max)s.") % {'max': MAX_ROWS})
    return result, ignored


# --- разбор значений

class RowError(Exception):
    pass


@cache
def _month_names():
    names = {}
    for code, _name in settings.LANGUAGES:
        with translation.override(code):
            for table in (MONTHS, MONTHS_ALT, MONTHS_3):
                for number, name in table.items():
                    names[normalize_header(name).rstrip('.')] = number
    return names


@cache
def _choice_names(choices):
    """Варианты написания значения из списка: код, подпись на всех языках, части подписи через «/»."""
    names = []
    for code, label in choices:
        variants = {normalize_header(code)}
        for lang, _name in settings.LANGUAGES:
            with translation.override(lang):
                text = str(label)
            variants.add(normalize_header(text))
            variants.update(normalize_header(part) for part in text.split('/'))
        variants.discard('')
        names.append((code, variants))
    return tuple(names)


def parse_choice(text, choices):
    wanted = normalize_header(text)
    names = _choice_names(tuple(choices))
    for code, variants in names:
        if wanted in variants:
            return code
    # «рус» → «Русский язык»: подходит, если начало совпадает ровно с одним вариантом
    if len(wanted) >= 3:
        matches = {code for code, variants in names if any(v.startswith(wanted) for v in variants)}
        if len(matches) == 1:
            return matches.pop()
    raise RowError(text)


DATE_FORMATS = ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%y', '%Y-%m-%d %H:%M:%S', '%d.%m.%Y %H:%M:%S')


def parse_date(text):
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise RowError(text)


def parse_month(text):
    text = text.strip()
    try:
        return parse_date(text).replace(day=1)
    except RowError:
        pass
    for fmt in ('%m.%Y', '%Y-%m', '%m/%Y', '%m-%Y'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    year = month = None
    for token in re.split(r'[\s,./-]+', text):
        digits = re.sub(r'\D', '', token)
        if len(digits) == 4:
            year = int(digits)
        elif digits and len(digits) <= 2 and token.rstrip('월').isdigit():
            month = int(digits)
        elif token:
            month = _month_names().get(normalize_header(token).rstrip('.'), month)
    if year and month and 1 <= month <= 12:
        return date(year, month, 1)
    # «сент.26», «нояб.26», «мая27» — сокращённый месяц и двузначный год, как в книгах школ
    match = re.fullmatch(r'([^\W\d_]+)\.?\s*(\d{2}|\d{4})', text.strip())
    if match:
        prefix = normalize_header(match.group(1))
        found = {number for name, number in _month_names().items() if len(prefix) >= 3 and name.startswith(prefix)}
        if len(found) == 1:
            short_year = int(match.group(2))
            return date(short_year + 2000 if short_year < 100 else short_year, found.pop(), 1)
    raise RowError(text)


def parse_amount(text):
    """«1 500 000», «1500000,50», «1.500.000» → Decimal."""
    cleaned = re.sub(r'[^\d,.\-]', '', text)
    if not cleaned:
        raise RowError(text)
    if ',' in cleaned and '.' in cleaned:
        decimal_sep = ',' if cleaned.rfind(',') > cleaned.rfind('.') else '.'
        thousands = '.' if decimal_sep == ',' else ','
        cleaned = cleaned.replace(thousands, '').replace(decimal_sep, '.')
    else:
        for sep in (',', '.'):
            parts = cleaned.split(sep)
            # Несколько разделителей или ровно три цифры после — это разряды: 1.500.000, 1,500
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                cleaned = cleaned.replace(sep, '')
            elif len(parts) == 2:
                cleaned = cleaned.replace(sep, '.')
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise RowError(text)


def parse_int(text):
    amount = parse_amount(text)
    if amount != amount.to_integral_value():
        raise RowError(text)
    return int(amount)


YES = {'да', 'отменено', 'bekor qilingan', 'yes', 'y', 'ha', 'xa', 'ҳа', '예', '네', '1', 'true', '+', 'истина', 'x', 'х', 'отменён', 'отменен'}


def parse_bool(text):
    return text.strip().casefold() in YES


ACCOUNT_WORDS = {
    # Как способ оплаты пишут в таблицах школ — к какому виду счёта это относится
    Account.KIND_CASH: ('наличные', 'наличный', 'наличныйрасчет', 'наличныйрасчёт', 'касса', 'naqd', 'naqdpul', 'cash', '현금'),
    Account.KIND_BANK: ('перечисление', 'безнал', 'банк', 'расчетныйсчет', 'расчётныйсчёт', 'otkazma', 'bank', 'banktransfer', '계좌이체'),
    Account.KIND_CARD: ('терминал', 'карта', 'terminal', 'card', 'karta', '카드단말기'),
}


def find_account(row, school, key):
    """Счёт школы по названию, виду («Касса», «Терминал») или способу («Наличный расчёт», «Перечисление»)."""
    text = row.raw(key)
    if not text or school is None:
        return None
    wanted = normalize_header(text)
    accounts = list(Account.objects.filter(school=school, is_active=True))
    for account in accounts:
        names = {normalize_header(account.name)} if account.name else set()
        for code, _name in settings.LANGUAGES:
            with translation.override(code):
                names.add(normalize_header(account.get_kind_display()))
        if wanted in names:
            return account
    for kind, words in ACCOUNT_WORDS.items():
        if wanted in words:
            account = Account.default_for(school, kind)
            if account and account.kind == kind:
                return account
    row.error(key, _("счёт «%(name)s» не найден у школы") % {'name': text})
    return None


def split_phones(text):
    """«901234567\n907654321», «911112233, 331112233» → два номера в виде +998XXXXXXXXX."""
    parts = [p for p in re.split(r'[\n,;/]+', text or '') if p.strip()]
    if len(parts) <= 1:
        parts = [text or '']
    phones = []
    for part in parts[:2]:
        normalized = normalize_phone(part)
        phones.append(normalized if is_valid_uz_phone(normalized) else part.strip())
    while len(phones) < 2:
        phones.append('')
    return phones[0], phones[1]


def find_by_name(objects, text, attr='name'):
    wanted = normalize_header(text)
    return [o for o in objects if normalize_header(getattr(o, attr)) == wanted]


# --- проверка строк

class RowResult:
    OK, EXISTS, SKIPPED, ERROR = 'ok', 'exists', 'skipped', 'error'

    def __init__(self, number):
        self.number = number
        self.status = self.OK
        self.errors = []
        self.bad_keys = set()   # колонки с ошибками
        self.notes = []
        self.summary = []
        self.form = None
        self.prepare = None     # что доделать с объектом перед сохранением

    @property
    def is_ok(self):
        return self.status == self.OK


class RowChecker:
    """Разбирает значения одной строки и собирает все ошибки сразу, а не только первую."""

    def __init__(self, kind, values, result):
        self.columns = {c.key: c for c in KINDS[kind]['columns']}
        self.values = values
        self.result = result
        self.bad_keys = result.bad_keys

    def label(self, key):
        return str(self.columns[key].label)

    def raw(self, key):
        return (self.values.get(key) or '').strip()

    def error(self, key, message):
        self.bad_keys.add(key)
        self.result.errors.append(f"«{self.label(key)}»: {message}" if key else message)

    def get(self, key, parser=None, required=False, invalid=None):
        text = self.raw(key)
        if not text:
            if required:
                self.error(key, _("не заполнено"))
            return None
        if parser is None:
            return text
        try:
            return parser(text)
        except RowError:
            self.error(key, invalid or _("не понятно значение «%(value)s»") % {'value': text})
            return None

    def date(self, key, required=False):
        return self.get(key, parse_date, required, _("не понятна дата «%(value)s». Пишите как 01.09.2026") % {'value': self.raw(key)})

    def month(self, key, required=False):
        return self.get(key, parse_month, required, _("не понятен месяц «%(value)s». Пишите как 09.2026") % {'value': self.raw(key)})

    def amount(self, key, required=False, allow_zero=False):
        value = self.get(key, parse_amount, required, _("не понятна сумма «%(value)s»") % {'value': self.raw(key)})
        if value is not None and (value < 0 or (value == 0 and not allow_zero)):
            self.error(key, _("сумма должна быть больше нуля"))
            return None
        return value

    def integer(self, key, required=False):
        return self.get(key, parse_int, required, _("нужно целое число, а не «%(value)s»") % {'value': self.raw(key)})

    def choice(self, key, choices, required=False):
        return self.get(key, lambda t: parse_choice(t, choices), required,
                        _("«%(value)s» — нет такого варианта") % {'value': self.raw(key)})

    def school(self, ctx, required):
        """Школа строки: владелец указывает её в файле, у остальных — своя школа."""
        if ctx.user.role != 'OWNER':
            return ctx.user.school
        name = self.get('school', required=required)
        if not name:
            return None
        found = find_by_name(ctx.schools, name)
        if not found:
            self.error('school', _("школа «%(name)s» не найдена на платформе") % {'name': name})
            return None
        return found[0]

    def form_errors(self, form, extra=None):
        """Ошибки формы — с названием колонки, к которой они относятся."""
        by_field = {c.field: c.key for c in self.columns.values() if c.field}
        by_field.update(extra or {})
        for field, errors in form.errors.items():
            key = by_field.get(field)
            if key in self.bad_keys:
                continue    # об этой колонке уже сказано точнее, «Обязательное поле» не повторяем
            for message in errors:
                self.error(key, message)


class Context:
    """Общие для всех строк данные: что пользователю разрешено и что уже встретилось в файле."""

    def __init__(self, user):
        self.user = user
        self.schools = list(School.objects.all())
        contracts = Contract.objects.select_related('school')
        employees = Staff.objects.filter(is_active=True).select_related('school', 'user')
        if user.role != 'OWNER':
            contracts = contracts.filter(school=user.school)
            employees = employees.filter(school=user.school)
        self.contract_queryset = contracts
        self.contracts = {c.number.strip(): c for c in contracts}
        self.employees = list(employees)
        self.academic_years = list(AcademicYear.objects.all())
        self.counterparties = list(Counterparty.objects.all())
        self.seen = {}


def check_contract(values, ctx, result, instance=None):
    """instance — уже загруженный договор, который эта строка исправляет (повторная загрузка книги)."""
    row = RowChecker('contracts', values, result)
    number = row.get('number', required=True)
    if number:
        if number in ctx.seen:
            row.error('number', _("номер повторяется — уже был в строке %(row)s") % {'row': ctx.seen[number]})
        else:
            ctx.seen[number] = result.number
            if instance is None and Contract.objects.filter(number=number).exists():
                result.status = RowResult.EXISTS
                result.notes.append(_("Договор с таким номером уже есть — строка пропущена."))
                result.summary = [number, row.raw('student_full_name')]
                return
    school = row.school(ctx, required=True)
    start = row.date('date', required=True)
    birth_year = row.integer('student_birth_year', required=True)
    duration = row.integer('duration_months', required=True)
    specialization = row.choice('specialization', Contract.SPECIALIZATION_CHOICES, required=True)
    fee = row.amount('tuition_fee', required=True, allow_zero=True)
    discount = row.integer('discount_percentage')
    food = row.amount('food_cost', allow_zero=True)
    discount_fixed = row.amount('discount_fixed', allow_zero=True)
    discount_reason = row.choice('discount_reason', Contract.DISCOUNT_REASON_CHOICES)
    cancelled = parse_bool(row.raw('cancelled'))
    cancelled_at = row.date('cancelled_at')
    billing_start = row.month('billing_start')
    year = None
    if row.raw('academic_year'):
        found = find_by_name(ctx.academic_years, row.raw('academic_year'))
        if found:
            year = found[0]
        else:
            row.error('academic_year', _("учебный год «%(name)s» не найден на платформе") % {'name': row.raw('academic_year')})
    if duration is not None and duration <= 0:
        row.error('duration_months', _("срок должен быть больше нуля"))
    if discount is not None and not 0 <= discount <= 100:
        row.error('discount_percentage', _("скидка должна быть от 0 до 100"))
    if cancelled and cancelled_at and start and cancelled_at < start:
        row.error('cancelled_at', _("дата отмены раньше даты договора"))

    phone, phone_2 = split_phones(row.raw('phone_number'))
    phone_2 = row.raw('phone_number_2') or phone_2
    data = {
        'number': number or '',
        'date': start.isoformat() if start else '',
        'month': Contract.MONTH_CHOICES[start.month - 1][0] if start else '',
        'student_full_name': row.raw('student_full_name'),
        'student_birth_year': birth_year if birth_year is not None else '',
        'student_class': row.raw('student_class'),
        'duration_months': duration if duration is not None else '',
        'parents_full_name': row.raw('parents_full_name'),
        'passport_data': row.raw('passport_data'),
        'inps': row.raw('inps'),
        'address': row.raw('address'),
        'phone_number': phone,
        'phone_number_2': phone_2,
        'discount_fixed': discount_fixed or 0,
        'discount_reason': discount_reason or '',
        'specialization': specialization or '',
        'tuition_fee': fee if fee is not None else '',
        'discount_percentage': discount or 0,
        'discount_amount': 0,     # скидка в сумах и итог считаются при сохранении
        'food_cost': food or 0,
        'total_amount': 0,
        'notes': row.raw('notes'),
        'academic_year': year.pk if year else '',
        'billing_start': billing_start.strftime('%Y-%m') if billing_start else '',
    }
    if ctx.user.role == 'OWNER':
        data['school'] = school.pk if school else ''
    for column in row.columns.values():
        if column.required and column.key not in ('school',) and column.key not in row.bad_keys and not row.raw(column.key):
            row.error(column.key, _("не заполнено"))
    form = ContractForm(data=data, user=ctx.user, instance=instance)
    if not form.is_valid():
        row.form_errors(form, extra={'month': 'date'})   # месяц договора берётся из даты
    result.form = form      # и при ошибках: загрузка книги может сохранить договор с пометкой

    if fee is not None and not result.errors:
        monthly = fee - min(fee, fee * Decimal(discount or 0) / 100 + (discount_fixed or 0)) + (food or 0)
        result.summary = [number, row.raw('student_full_name'), school.name if school else '',
                          _("%(amount)s сум в месяц") % {'amount': format_money(monthly)}]
    if result.errors:
        return

    def prepare(obj):
        obj.user = ctx.user
        if ctx.user.role != 'OWNER':
            obj.school = ctx.user.school
        if cancelled:
            obj.is_cancelled = True
            obj.cancelled_at = cancelled_at
    result.form = form
    result.prepare = prepare


def check_payment(values, ctx, result):
    row = RowChecker('payments', values, result)
    number = row.get('contract', required=True)
    contract = ctx.contracts.get(number) if number else None
    if number and contract is None:
        row.error('contract', _("договор «%(number)s» не найден") % {'number': number})
    payment_date = row.date('payment_date', required=True)
    for_month = row.month('for_month')
    if for_month is None and payment_date and not row.raw('for_month'):
        for_month = payment_date.replace(day=1)
    amount = row.amount('amount', required=True)
    method = row.choice('method', Payment.METHOD_CHOICES) or 'CASH'
    if result.errors:
        return

    result.summary = [f"{contract.number} · {contract.student_full_name}", payment_date.strftime('%d.%m.%Y'),
                      format_russian_month(for_month), money(amount), dict(Payment.METHOD_CHOICES)[method]]
    if Payment.active.filter(contract=contract, kind=Payment.KIND_PAYMENT, for_month=for_month, amount=amount,
                             payment_date=payment_date).exists():
        result.status = RowResult.EXISTS
        result.notes.append(_("Такая оплата уже есть — строка пропущена, чтобы не задвоить."))
        return
    if not contract.is_billable_in(for_month):
        result.notes.append(_("За этот месяц по договору нет начисления — проверьте месяц."))
    form = PaymentForm(data={
        'contract': contract.pk, 'for_month': for_month.isoformat(), 'amount': amount, 'method': method,
        'payment_date': payment_date.isoformat(), 'notes': row.raw('notes'),
    }, contract_queryset=ctx.contract_queryset)
    if not form.is_valid():
        row.form_errors(form)
        return

    def prepare(obj):
        obj.created_by = ctx.user
    result.form = form
    result.prepare = prepare


def find_employee(row, ctx):
    text = row.get('employee', required=True)
    if not text:
        return None
    phone = normalize_phone(text)
    if is_valid_uz_phone(phone):
        found = [e for e in ctx.employees if phone in e.phones]
    else:
        found = find_by_name(ctx.employees, text, 'full_name')
        if len(found) > 1:
            row.error('employee', _("несколько сотрудников с именем «%(name)s» — укажите телефон") % {'name': text})
            return None
    if not found:
        row.error('employee', _("сотрудник «%(name)s» не найден") % {'name': text})
        return None
    return found[0]


def check_payout(values, ctx, result):
    row = RowChecker('payouts', values, result)
    if parse_bool(row.raw('reversed')):
        result.status = RowResult.SKIPPED
        result.notes.append(_("Отмечена как сторно — не загружается."))
        return
    employee = find_employee(row, ctx)
    school = row.school(ctx, required=False)
    if ctx.user.role == 'OWNER' and school is None and employee and not row.raw('school'):
        school = employee.school
        if school is None:
            row.error('school', _("у сотрудника не указана школа — заполните колонку"))
    for_month = row.month('for_month', required=True)
    amount = row.amount('amount', required=True)
    payout_date = row.date('payout_date', required=True)
    account = find_account(row, school, 'account')
    if result.errors:
        return

    result.summary = [employee.full_name, school.name if school else '', format_russian_month(for_month),
                      money(amount), payout_date.strftime('%d.%m.%Y')]
    if EmployeePayout.active.filter(employee=employee, for_month=for_month, amount=amount, payout_date=payout_date).exists():
        result.status = RowResult.EXISTS
        result.notes.append(_("Такая выплата уже есть — строка пропущена, чтобы не задвоить."))
        return
    data = {'employee': employee.pk, 'for_month': for_month.isoformat(), 'amount': amount,
            'payout_date': payout_date.isoformat(), 'notes': row.raw('notes'), 'account': account.pk if account else ''}
    if ctx.user.role == 'OWNER':
        data['school'] = school.pk
    form = EmployeePayoutForm(data=data, user=ctx.user)
    if not form.is_valid():
        row.form_errors(form)
        return

    def prepare(obj):
        obj.created_by = ctx.user
        if ctx.user.role != 'OWNER':
            obj.school = ctx.user.school
    result.form = form
    result.prepare = prepare


def check_transaction(values, ctx, result):
    row = RowChecker('transactions', values, result)
    if parse_bool(row.raw('reversed')):
        result.status = RowResult.SKIPPED
        result.notes.append(_("Отмечена как сторно — не загружается."))
        return
    tr_date = row.date('date', required=True)
    school = row.school(ctx, required=True)
    tr_type = row.choice('type', Transaction.TYPE_CHOICES)
    amount = row.amount('amount', required=True)
    category = None
    if row.raw('category'):
        category = Category.find(row.raw('category'), tr_type)
        if category is None:
            row.error('category', _("статьи «%(name)s» нет в справочнике — добавьте её или выберите другую")
                      % {'name': row.raw('category')})
    elif tr_type is None and 'type' not in row.bad_keys:
        row.error('type', _("укажите тип или статью"))
    elif tr_type:
        category = Category.other(tr_type)
    account = find_account(row, school, 'account')
    counterparty = None
    if row.raw('counterparty'):
        found = find_by_name(ctx.counterparties, row.raw('counterparty'), 'display_name') or \
            [c for c in ctx.counterparties if normalize_header(row.raw('counterparty')) in map(normalize_header, c.all_names())]
        if found:
            counterparty = found[0]
        else:
            row.error('counterparty', _("контрагента «%(name)s» нет в справочнике") % {'name': row.raw('counterparty')})
    if result.errors:
        return

    result.summary = [tr_date.strftime('%d.%m.%Y'), school.name, category.display_name, money(amount),
                      account.display_name if account else '']
    if Transaction.active.filter(school=school, category=category, date=tr_date, amount=amount).exists():
        result.status = RowResult.EXISTS
        result.notes.append(_("Такая транзакция уже есть — строка пропущена, чтобы не задвоить."))
        return
    data = {'category': category.pk, 'amount': amount, 'date': tr_date.isoformat(), 'notes': row.raw('notes'),
            'account': account.pk if account else '', 'counterparty': counterparty.pk if counterparty else ''}
    if ctx.user.role == 'OWNER':
        data['school'] = school.pk
    form = TransactionForm(data=data, user=ctx.user)
    if not form.is_valid():
        row.form_errors(form)
        return

    def prepare(obj):
        obj.created_by = ctx.user
        if ctx.user.role != 'OWNER':
            obj.school = ctx.user.school
    result.form = form
    result.prepare = prepare


CHECKERS = {
    'contracts': check_contract,
    'payments': check_payment,
    'payouts': check_payout,
    'transactions': check_transaction,
}


def money(amount):
    return _("%(amount)s сум") % {'amount': format_money(amount)}


def check_rows(kind, user, rows):
    """Проверяет все строки, ничего не сохраняя. rows — [(номер строки, {ключ: значение})]."""
    ctx = Context(user)
    results = []
    for number, values in rows:
        result = RowResult(number)
        CHECKERS[kind](values, ctx, result)
        if result.errors:
            result.status = RowResult.ERROR
            result.form = None
        if not result.summary:
            # Чтобы строку с ошибкой можно было узнать: первые заполненные значения как в файле
            result.summary = [v for v in values.values() if v][:3]
        results.append(result)
    return results


def save_rows(kind, user, results, filename):
    """Сохраняет правильные строки одной транзакцией. Возвращает число сохранённых."""
    saved = 0
    with db_transaction.atomic():
        for result in results:
            if not result.is_ok:
                continue
            obj = result.form.save(commit=False)
            result.prepare(obj)
            obj.save()
            AuditLog.record(user, 'CREATE', obj, {'source': 'excel', 'file': filename[:100], 'row': result.number})
            saved += 1
    return saved


# --- шаблон для заполнения

def build_template(kind, user):
    """Пустая таблица .xlsx с нужными колонками, подсказками и списками значений."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter, quote_sheetname
    from openpyxl.worksheet.datavalidation import DataValidation

    columns = columns_for(kind, user)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = str(KINDS[kind]['title'])[:31]
    lists = workbook.create_sheet(_("Списки")[:31])
    help_sheet = workbook.create_sheet(_("Как заполнять")[:31])

    header_fill = PatternFill('solid', fgColor='DDE7FF')
    required_fill = PatternFill('solid', fgColor='FFE3B3')
    for index, column in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=str(column.label) + (' *' if column.is_required(user) else ''))
        cell.font = Font(bold=True)
        cell.fill = required_fill if column.is_required(user) else header_fill
        cell.alignment = Alignment(wrap_text=True, vertical='center')
        letter = get_column_letter(index)
        sheet.column_dimensions[letter].width = column.width
        if column.text:
            for row in range(2, 502):
                sheet.cell(row=row, column=index).number_format = '@'
    sheet.freeze_panes = 'A2'
    sheet.row_dimensions[1].height = 30

    choice_lists = {
        'school': lambda: [s.name for s in School.objects.order_by('name')],
        'specialization': lambda: [str(label) for _code, label in Contract.SPECIALIZATION_CHOICES],
        'method': lambda: [str(label) for _code, label in Payment.METHOD_CHOICES],
        'type': lambda: [str(label) for _code, label in Transaction.TYPE_CHOICES],
        'academic_year': lambda: [y.name for y in AcademicYear.objects.all()],
        'category': lambda: [c.display_name for c in Category.objects.filter(is_active=True)],
        'account': lambda: [str(label) for kind, label in Account.KIND_CHOICES if kind != Account.KIND_OTHER],
        'counterparty': lambda: [c.display_name for c in Counterparty.objects.all()],
        'employee': lambda: [f"{e.full_name} — {format_phone(e.phones[0])}" if e.phones else e.full_name
                             for e in Staff.objects.filter(is_active=True, **({} if user.role == 'OWNER' else {'school': user.school}))],
        'cancelled': lambda: [_('да')],
        'reversed': lambda: [_('да')],
    }
    list_column = 0
    for index, column in enumerate(columns, start=1):
        values = choice_lists[column.key]() if column.key in choice_lists else None
        if not values:
            continue
        list_column += 1
        letter = get_column_letter(list_column)
        lists.cell(row=1, column=list_column, value=str(column.label)).font = Font(bold=True)
        for row, value in enumerate(values, start=2):
            lists.cell(row=row, column=list_column, value=value)
        lists.column_dimensions[letter].width = 30
        source = f"{quote_sheetname(lists.title)}!${letter}$2:${letter}${len(values) + 1}"
        # Предупреждение, а не запрет: можно вписать и свой вариант (например, «рус»)
        validation = DataValidation(type='list', formula1=source, allow_blank=True, errorStyle='warning')
        validation.add(f"{get_column_letter(index)}2:{get_column_letter(index)}{MAX_ROWS + 1}")
        sheet.add_data_validation(validation)

    help_sheet.append([_("Колонка"), _("Обязательная"), _("Как заполнять")])
    for cell in help_sheet[1]:
        cell.font = Font(bold=True)
    for column in columns:
        help_sheet.append([str(column.label), _('да') if column.is_required(user) else '', str(column.hint)])
    help_sheet.append([])
    help_sheet.append([_("Заполняйте лист «%(sheet)s», начиная со второй строки. Обязательные колонки отмечены звёздочкой.") % {'sheet': sheet.title}])
    help_sheet.append([_("Перед сохранением сайт покажет, что будет загружено и где ошибки.")])
    help_sheet.column_dimensions['A'].width = 22
    help_sheet.column_dimensions['B'].width = 14
    help_sheet.column_dimensions['C'].width = 90

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
