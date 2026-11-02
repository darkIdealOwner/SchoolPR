"""Загрузка данных из 1С в платформу.

Все каналы (файл, OData, API) приводят данные к одному формату — списку словарей —
и передают их сюда. Импорт идемпотентный: запись ищется по external_id (GUID документа 1С),
повторная загрузка тех же данных ничего не дублирует.

Форматы записей описаны в docs/1c-integration.md.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone

from users.models import AuditLog, Category, Contract, Staff, EmployeePayout, Payment, School, Transaction, User, ExternalSyncMixin

from .models import SyncRun

REVERSAL_REASON = "Документ помечен на удаление или распроведён в 1С"


class RecordError(Exception):
    pass


# --- разбор значений

def parse_date(value, field):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        raise RecordError(f"не указано поле {field}")
    text = str(value).strip()
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%d.%m.%Y', '%Y-%m'):
        try:
            return datetime.strptime(text[:19] if fmt.startswith('%Y-%m-%dT') else text, fmt).date()
        except ValueError:
            continue
    raise RecordError(f"не удалось разобрать дату в поле {field}: {value!r}")


def parse_amount(value):
    try:
        amount = Decimal(str(value).replace(' ', '').replace(' ', '').replace(',', '.'))
    except (InvalidOperation, TypeError):
        raise RecordError(f"некорректная сумма: {value!r}")
    if amount <= 0:
        raise RecordError(f"сумма должна быть больше нуля: {value!r}")
    return amount


def is_deleted(record):
    return bool(record.get('deleted')) or record.get('posted') is False


def require_id(record):
    external_id = str(record.get('external_id') or '').strip()
    if not external_id:
        raise RecordError("нет external_id (GUID документа 1С)")
    return external_id


# --- поиск связанных объектов

def find_contract(record):
    if record.get('contract_external_id'):
        contract = Contract.objects.filter(external_id=record['contract_external_id']).first()
        if contract:
            return contract
    if record.get('contract_number'):
        contract = Contract.objects.filter(number=str(record['contract_number']).strip()).first()
        if contract:
            return contract
    raise RecordError(
        f"договор не найден (contract_external_id={record.get('contract_external_id')!r}, "
        f"contract_number={record.get('contract_number')!r}) — свяжите договор с 1С"
    )


def school_by_name(name):
    # Сравнение без учёта регистра в Python: SQLite не умеет iexact для кириллицы
    wanted = ' '.join(str(name).split()).casefold()
    for school in School.objects.all():
        if ' '.join(school.name.split()).casefold() == wanted:
            return school
    return None


def find_school(record):
    if record.get('school_external_id'):
        school = School.objects.filter(external_id=record['school_external_id']).first()
        if school:
            return school
    if record.get('school_name'):
        school = school_by_name(record['school_name'])
        if school:
            return school
    raise RecordError(
        f"школа не найдена (school_external_id={record.get('school_external_id')!r}, "
        f"school_name={record.get('school_name')!r}) — свяжите школу с подразделением 1С"
    )


def staff_by_phone(phone):
    phone = str(phone).strip()
    return Staff.objects.filter(Q(phone=phone) | Q(user__phone_number=phone)).first()


def find_employee(record):
    """Сотрудник из справочника сотрудников: по GUID физлица 1С, иначе по телефону."""
    if record.get('employee_external_id'):
        guid = record['employee_external_id']
        # GUID мог быть проставлен раньше пользователю платформы — ищем и через него
        employee = Staff.objects.filter(Q(external_id=guid) | Q(user__external_id=guid)).first()
        if employee:
            return employee
    if record.get('employee_phone'):
        employee = staff_by_phone(record['employee_phone'])
        if employee:
            return employee
    raise RecordError(
        f"сотрудник не найден (employee_external_id={record.get('employee_external_id')!r}, "
        f"employee_phone={record.get('employee_phone')!r}) — свяжите сотрудника с физлицом 1С"
    )


# --- построение полей по типу данных

def payment_fields(record):
    payment_date = parse_date(record.get('payment_date') or record.get('date'), 'payment_date')
    for_month = parse_date(record.get('for_month'), 'for_month') if record.get('for_month') else payment_date
    method = record.get('method') or 'BANK'
    if method not in dict(Payment.METHOD_CHOICES):
        raise RecordError(f"неизвестный способ оплаты: {method!r}")
    return {
        'contract': find_contract(record),
        'payment_date': payment_date,
        'for_month': for_month.replace(day=1),
        'amount': parse_amount(record.get('amount')),
        'method': method,
        'notes': record.get('notes') or '',
    }


def payout_fields(record):
    employee = find_employee(record)
    payout_date = parse_date(record.get('payout_date') or record.get('date'), 'payout_date')
    for_month = parse_date(record.get('for_month'), 'for_month') if record.get('for_month') else payout_date
    school = find_school(record) if (record.get('school_external_id') or record.get('school_name')) else employee.school
    if school is None:
        raise RecordError("у сотрудника не указана школа, передайте school_external_id")
    return {
        'employee': employee,
        'school': school,
        'payout_date': payout_date,
        'for_month': for_month.replace(day=1),
        'amount': parse_amount(record.get('amount')),
        'notes': record.get('notes') or '',
    }


def transaction_fields(record):
    tr_type = str(record.get('type') or '').upper()
    if tr_type not in dict(Transaction.TYPE_CHOICES):
        raise RecordError(f"type должен быть INCOME или EXPENSE, получено {record.get('type')!r}")
    # Статья 1С ищется в справочнике по названию; если такой нет — «Прочие», а название статьи 1С — в примечании
    category_name = str(record.get('category') or '').strip()
    category = Category.find(category_name, tr_type) if category_name else None
    notes = record.get('notes') or ''
    if category is None:
        category = Category.other(tr_type)
        if category_name:
            notes = f"[{category_name}] {notes}".strip()
    return {
        'school': find_school(record),
        'type': tr_type,
        'category': category,
        'date': parse_date(record.get('date'), 'date'),
        'amount': parse_amount(record.get('amount')),
        'notes': notes,
    }


DOCUMENTS = {
    'payments': (Payment, payment_fields),
    'payouts': (EmployeePayout, payout_fields),
    'transactions': (Transaction, transaction_fields),
}


def _apply_document(model, build_fields, record, now):
    """Создаёт/обновляет/сторнирует одну запись. Возвращает 'created' | 'updated' | 'reversed' | 'skipped'."""
    external_id = require_id(record)
    existing = model.objects.filter(external_id=external_id).first()

    if is_deleted(record):
        if existing and not existing.is_reversed:
            existing.reverse(None, REVERSAL_REASON)
            AuditLog.record(None, 'REVERSE', existing, {'reason': REVERSAL_REASON, 'source': '1C'})
            return 'reversed'
        return 'skipped'

    fields = build_fields(record)

    if existing is None:
        obj = model.objects.create(external_id=external_id, source=ExternalSyncMixin.SOURCE_1C, synced_at=now, **fields)
        AuditLog.record(None, 'CREATE', obj, {'source': '1C', 'external_id': external_id})
        return 'created'

    changes = {}
    for name, value in fields.items():
        old = getattr(existing, name)
        if old != value:
            changes[name] = [str(old), str(value)]
            setattr(existing, name, value)
    restored = existing.is_reversed and existing.reversal_reason == REVERSAL_REASON
    if restored:
        # Документ снова проведён в 1С — возвращаем запись в расчёты
        existing.is_reversed = False
        existing.reversed_at = None
        existing.reversal_reason = ''
        changes['is_reversed'] = ['True', 'False']
    existing.source = ExternalSyncMixin.SOURCE_1C
    existing.synced_at = now
    existing.save()
    if changes:
        AuditLog.record(None, 'UPDATE', existing, {'source': '1C', **changes})
        return 'updated'
    return 'skipped'


LINKS = {
    # entity: (модель, поиск записи платформы по значению, ключ в записи)
    'schools': (School, school_by_name, 'name'),
    'employees': (Staff, staff_by_phone, 'phone'),
    'contracts': (Contract, lambda v: Contract.objects.filter(number=v).first(), 'number'),
}


def _apply_link(entity, record, now):
    """Проставляет GUID 1С существующей записи платформы (школе, сотруднику, договору)."""
    model, lookup, key = LINKS[entity]
    external_id = require_id(record)
    value = str(record.get(key) or '').strip()
    if not value:
        raise RecordError(f"нет поля {key}")
    obj = lookup(value)
    if obj is None:
        raise RecordError(f"на платформе не найдено: {key}={value!r}")
    taken = model.objects.filter(external_id=external_id).exclude(pk=obj.pk).first()
    if taken:
        raise RecordError(f"GUID {external_id} уже привязан к «{taken}»")
    if obj.external_id == external_id:
        return 'skipped'
    old = obj.external_id
    obj.external_id = external_id
    obj.synced_at = now
    obj.save(update_fields=['external_id', 'synced_at'])
    AuditLog.record(None, 'UPDATE', obj, {'external_id': [old, external_id], 'source': '1C'})
    return 'updated'


class _DryRunRollback(Exception):
    pass


def run_import(entity, records, channel='file', dry_run=False, user=None):
    """Загружает записи и пишет итог в журнал обменов. Возвращает SyncRun."""
    if entity not in DOCUMENTS and entity not in LINKS:
        raise ValueError(f"Неизвестный тип данных: {entity}")
    if not isinstance(records, list):
        raise ValueError("Ожидается список записей")

    run = SyncRun.objects.create(entity=entity, channel=channel, dry_run=dry_run,
                                 triggered_by=user if user and user.is_authenticated else None)
    counters = {'created': 0, 'updated': 0, 'reversed': 0, 'skipped': 0}
    errors = []
    now = timezone.now()

    try:
        with db_transaction.atomic():
            for index, record in enumerate(records, start=1):
                if not isinstance(record, dict):
                    errors.append({'row': index, 'error': 'запись должна быть объектом'})
                    continue
                try:
                    with db_transaction.atomic():
                        if entity in DOCUMENTS:
                            model, build_fields = DOCUMENTS[entity]
                            outcome = _apply_document(model, build_fields, record, now)
                        else:
                            outcome = _apply_link(entity, record, now)
                    counters[outcome] += 1
                except RecordError as exc:
                    errors.append({'row': index, 'external_id': record.get('external_id'), 'error': str(exc)})
            if dry_run:
                raise _DryRunRollback()
    except _DryRunRollback:
        pass

    run.total = len(records)
    run.created = counters['created']
    run.updated = counters['updated']
    run.reversed = counters['reversed']
    run.skipped = counters['skipped']
    run.failed = len(errors)
    run.errors = errors[:500]
    run.status = 'SUCCESS' if not errors else ('FAILED' if len(errors) == len(records) else 'PARTIAL')
    run.finished_at = timezone.now()
    run.save()
    return run
