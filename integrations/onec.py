"""Чтение документов из 1С через стандартный интерфейс OData.

Включается в 1С: «Администрирование → Обслуживание → Настройка стандартного интерфейса OData»
(нужно опубликовать базу на веб-сервере и отметить нужные документы).

Адрес и доступ задаются в .env:
    ONEC_ODATA_URL=http://server/base/odata/standard.odata
    ONEC_USER=...
    ONEC_PASSWORD=...

Имена документов и реквизитов зависят от конфигурации 1С. Ниже — настройки для типовой
«Бухгалтерии предприятия». Если в вашей базе они называются иначе, правьте только MAPPINGS.
"""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings


class OneCError(Exception):
    pass


class ODataClient:
    page_size = 500

    def __init__(self, base_url=None, user=None, password=None, timeout=None):
        self.base_url = (base_url or settings.ONEC_ODATA_URL or '').rstrip('/')
        self.user = user if user is not None else settings.ONEC_USER
        self.password = password if password is not None else settings.ONEC_PASSWORD
        self.timeout = timeout or settings.ONEC_TIMEOUT
        if not self.base_url:
            raise OneCError("Не задан ONEC_ODATA_URL — укажите адрес OData-интерфейса 1С в .env")

    def _get(self, url):
        request = urllib.request.Request(url, headers={'Accept': 'application/json'})
        if self.user:
            token = base64.b64encode(f"{self.user}:{self.password}".encode('utf-8')).decode('ascii')
            request.add_header('Authorization', f'Basic {token}')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')[:500]
            raise OneCError(f"1С ответила {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise OneCError(f"Нет связи с 1С: {exc.reason}") from exc

    def fetch(self, entity, filter_expr=None, select=None):
        """Все записи набора (постранично)."""
        rows = []
        skip = 0
        while True:
            params = {'$format': 'json', '$top': self.page_size, '$skip': skip}
            if filter_expr:
                params['$filter'] = filter_expr
            if select:
                params['$select'] = ','.join(select)
            url = f"{self.base_url}/{urllib.parse.quote(entity)}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"
            page = self._get(url).get('value', [])
            rows.extend(page)
            if len(page) < self.page_size:
                return rows
            skip += self.page_size


def _common(row):
    return {
        'external_id': row.get('Ref_Key'),
        'date': row.get('Date'),
        'deleted': bool(row.get('DeletionMark')),
        'posted': row.get('Posted', True),
        'amount': row.get('СуммаДокумента'),
        'notes': ' '.join(filter(None, [row.get('Number', '').strip(), row.get('Комментарий', '')])).strip(),
    }


def map_payment(row, method):
    if not row.get('ДоговорКонтрагента_Key') or row.get('ДоговорКонтрагента_Key') == EMPTY_REF:
        return None  # не оплата по договору с родителями
    record = _common(row)
    record.update({
        'payment_date': row.get('Date'),
        'contract_external_id': row.get('ДоговорКонтрагента_Key'),
        'method': method,
    })
    return record


def map_expense(row, categories):
    record = _common(row)
    record.update({
        'type': 'EXPENSE',
        'school_external_id': row.get('Подразделение_Key') or row.get('ПодразделениеОрганизации_Key'),
        'category': categories.get(row.get('СтатьяДвиженияДенежныхСредств_Key'), ''),
    })
    return record


# entity платформы -> список (набор OData, функция преобразования строки в запись импорта)
MAPPINGS = {
    'payments': [
        ('Document_ПриходныйКассовыйОрдер', lambda row: map_payment(row, 'CASH')),
        ('Document_ПоступлениеНаРасчетныйСчет', lambda row: map_payment(row, 'BANK')),
    ],
    'transactions': [
        ('Document_РасходныйКассовыйОрдер', map_expense),
        ('Document_СписаниеСРасчетногоСчета', map_expense),
    ],
}

EMPTY_REF = '00000000-0000-0000-0000-000000000000'
CATEGORIES_CATALOG = 'Catalog_СтатьиДвиженияДенежныхСредств'


def fetch_records(entity, since=None, client=None):
    """Документы 1С, приведённые к формату импорта. since — дата, с которой брать документы."""
    if entity not in MAPPINGS:
        raise OneCError(f"Для «{entity}» загрузка через OData не настроена — используйте файл или API")
    client = client or ODataClient()
    filter_expr = f"Date ge datetime'{since:%Y-%m-%dT00:00:00}'" if since else None
    extra = ()
    if entity == 'transactions':
        # Названия статей движения денежных средств вместо GUID
        categories = {row['Ref_Key']: row.get('Description', '')
                      for row in client.fetch(CATEGORIES_CATALOG, select=['Ref_Key', 'Description'])}
        extra = (categories,)
    records = []
    for odata_entity, mapper in MAPPINGS[entity]:
        for row in client.fetch(odata_entity, filter_expr=filter_expr):
            record = mapper(row, *extra)
            if record is not None:
                records.append(record)
    return records
