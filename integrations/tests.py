import json
import os
import tempfile
from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from users.models import AuditLog, Contract, EmployeePayout, Payment, School, Transaction, User

from .importers import REVERSAL_REASON, run_import
from .models import SyncRun
from .onec import fetch_records

TOKEN = 'test-token-123'


class BaseData(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="школа №1", external_id="SCH-1")
        self.owner = User.objects.create_user(username="i_owner", phone_number="+998910000001", password="x", role="OWNER")
        self.director = User.objects.create_user(username="i_dir", phone_number="+998910000002", password="x",
                                                 role="DIRECTOR", school=self.school)
        self.employee = User.objects.create_user(username="i_emp", phone_number="+998910000003", password="x",
                                                 role="EMPLOYEE", school=self.school)
        self.contract = Contract.objects.create(number="30", user=self.owner, school=self.school, date=date(2026, 9, 1),
                                                tuition_fee=Decimal(1000), external_id="CTR-30")

    def payment_record(self, **overrides):
        record = {'external_id': 'PAY-1', 'contract_external_id': 'CTR-30', 'payment_date': '2026-10-05',
                  'for_month': '2026-10', 'amount': '1000.00', 'method': 'CLICK'}
        record.update(overrides)
        return record


class ImporterTestCase(BaseData):
    def test_create_is_idempotent_and_marks_source(self):
        run = run_import('payments', [self.payment_record()])
        self.assertEqual((run.status, run.created), ('SUCCESS', 1))
        payment = Payment.objects.get(external_id='PAY-1')
        self.assertEqual(payment.source, '1C')
        self.assertEqual(payment.for_month, date(2026, 10, 1))
        self.assertIsNotNone(payment.synced_at)

        run = run_import('payments', [self.payment_record()])
        self.assertEqual((run.created, run.updated, run.skipped), (0, 0, 1))
        self.assertEqual(Payment.objects.count(), 1)

    def test_update_reverse_and_restore(self):
        run_import('payments', [self.payment_record()])
        run = run_import('payments', [self.payment_record(amount='900')])
        self.assertEqual(run.updated, 1)
        self.assertEqual(Payment.objects.get().amount, Decimal(900))
        self.assertTrue(AuditLog.objects.filter(action='UPDATE', changes__amount__isnull=False).exists())

        run = run_import('payments', [self.payment_record(deleted=True)])
        self.assertEqual(run.reversed, 1)
        payment = Payment.objects.get()
        self.assertTrue(payment.is_reversed)
        self.assertEqual(payment.reversal_reason, REVERSAL_REASON)

        run = run_import('payments', [self.payment_record(posted=True, amount='900')])
        self.assertEqual(run.updated, 1)
        self.assertFalse(Payment.objects.get().is_reversed)

    def test_contract_found_by_number_and_errors_reported(self):
        records = [
            self.payment_record(external_id='PAY-2', contract_external_id=None, contract_number='30'),
            self.payment_record(external_id='PAY-3', contract_external_id='UNKNOWN'),
            self.payment_record(external_id='PAY-4', amount='-5'),
            self.payment_record(external_id=''),
        ]
        run = run_import('payments', records)
        self.assertEqual(run.status, 'PARTIAL')
        self.assertEqual((run.created, run.failed), (1, 3))
        self.assertEqual([e['row'] for e in run.errors], [2, 3, 4])
        self.assertIn('договор не найден', run.errors[0]['error'])

    def test_dry_run_saves_nothing_but_logs_run(self):
        run = run_import('payments', [self.payment_record()], dry_run=True)
        self.assertEqual(run.created, 1)
        self.assertFalse(Payment.objects.exists())
        self.assertTrue(SyncRun.objects.filter(pk=run.pk, dry_run=True).exists())

    def test_payouts_and_transactions(self):
        self.employee.external_id = 'EMP-1'
        self.employee.save()
        run = run_import('payouts', [{'external_id': 'PO-1', 'employee_external_id': 'EMP-1', 'payout_date': '10.10.2026',
                                      'for_month': '2026-09', 'amount': '3 500 000'}])
        self.assertEqual(run.created, 1, run.errors)
        payout = EmployeePayout.objects.get()
        self.assertEqual((payout.school, payout.amount), (self.school, Decimal(3500000)))

        run = run_import('transactions', [
            {'external_id': 'TR-1', 'school_external_id': 'SCH-1', 'type': 'expense', 'category': 'Свет',
             'date': '2026-10-12T00:00:00', 'amount': '850000,50'},
            {'external_id': 'TR-2', 'school_name': 'нет такой', 'type': 'EXPENSE', 'date': '2026-10-12', 'amount': 1},
        ])
        self.assertEqual((run.created, run.failed), (1, 1))
        self.assertEqual(Transaction.objects.get().amount, Decimal('850000.50'))

    def test_links(self):
        other = School.objects.create(name="Школа №2")
        run = run_import('schools', [{'external_id': 'SCH-2', 'name': 'школа №2'}, {'external_id': 'SCH-1', 'name': 'Школа №2'}])
        self.assertEqual((run.updated, run.failed), (1, 1))
        other.refresh_from_db()
        self.assertEqual(other.external_id, 'SCH-2')
        self.assertIn('уже привязан', run.errors[0]['error'])

        run = run_import('employees', [{'external_id': 'EMP-9', 'phone': '+998910000003'}])
        self.assertEqual(run.updated, 1)
        run = run_import('contracts', [{'external_id': 'CTR-NEW', 'number': '30'}])
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.external_id, 'CTR-NEW')

    def test_unknown_entity(self):
        with self.assertRaises(ValueError):
            run_import('nonsense', [])


class FakeODataClient:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def fetch(self, entity, filter_expr=None, select=None):
        self.calls.append((entity, filter_expr))
        return self.data.get(entity, [])


class ODataMappingTestCase(BaseData):
    def test_payments_mapping(self):
        client = FakeODataClient({
            'Document_ПриходныйКассовыйОрдер': [
                {'Ref_Key': 'PKO-1', 'Number': '00125 ', 'Date': '2026-10-05T10:00:00', 'Posted': True, 'DeletionMark': False,
                 'СуммаДокумента': 1000, 'ДоговорКонтрагента_Key': 'CTR-30', 'Комментарий': 'октябрь'},
                {'Ref_Key': 'PKO-2', 'Date': '2026-10-06T10:00:00', 'СуммаДокумента': 5,
                 'ДоговорКонтрагента_Key': '00000000-0000-0000-0000-000000000000'},
            ],
        })
        records = fetch_records('payments', since=date(2026, 10, 1), client=client)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['method'], 'CASH')
        self.assertIn("datetime'2026-10-01T00:00:00'", client.calls[0][1])
        run = run_import('payments', records, channel='odata')
        self.assertEqual(run.created, 1, run.errors)
        self.assertEqual(Payment.objects.get().notes, '00125 октябрь')

    def test_expense_category_names(self):
        client = FakeODataClient({
            'Catalog_СтатьиДвиженияДенежныхСредств': [{'Ref_Key': 'CAT-1', 'Description': 'Аренда'}],
            'Document_СписаниеСРасчетногоСчета': [
                {'Ref_Key': 'SP-1', 'Date': '2026-10-07T00:00:00', 'СуммаДокумента': 500, 'Подразделение_Key': 'SCH-1',
                 'СтатьяДвиженияДенежныхСредств_Key': 'CAT-1'},
            ],
        })
        records = fetch_records('transactions', client=client)
        run = run_import('transactions', records, channel='odata')
        self.assertEqual(run.created, 1, run.errors)
        self.assertEqual(Transaction.objects.get().category.name, 'Аренда')


@override_settings(ONEC_API_TOKEN=TOKEN)
class APITestCase(BaseData):
    def api(self, token=TOKEN):
        return Client(HTTP_AUTHORIZATION=f'Token {token}')

    def test_auth(self):
        self.assertIn(Client().get('/api/1c/ping/').status_code, (401, 403))
        self.assertEqual(self.api('wrong').get('/api/1c/ping/').status_code, 401)
        self.assertEqual(self.api().get('/api/1c/ping/').status_code, 200)
        # Вход на сайт не даёт доступа к API
        client = Client()
        client.force_login(self.owner)
        self.assertIn(client.get('/api/1c/ping/').status_code, (401, 403))
        with self.settings(ONEC_API_TOKEN=''):
            self.assertEqual(self.api().get('/api/1c/ping/').status_code, 401)

    def test_contracts_and_link(self):
        unlinked = Contract.objects.create(number="31", user=self.owner, school=self.school, date=date(2026, 9, 1))
        data = self.api().get('/api/1c/contracts/?unlinked=1').json()
        self.assertEqual([c['number'] for c in data['results']], ['31'])
        self.assertEqual(data['results'][0]['school_external_id'], 'SCH-1')

        response = self.api().post(f'/api/1c/contracts/{unlinked.pk}/link/', {'external_id': 'CTR-31'}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        unlinked.refresh_from_db()
        self.assertEqual(unlinked.external_id, 'CTR-31')
        response = self.api().post(f'/api/1c/contracts/{unlinked.pk}/link/', {'external_id': 'CTR-30'}, content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_platform_payments_for_1c(self):
        manual = Payment.objects.create(contract=self.contract, for_month=date(2026, 10, 1), amount=Decimal(1000), method='CASH')
        run_import('payments', [self.payment_record()])
        data = self.api().get('/api/1c/payments/?unlinked=1').json()
        self.assertEqual([p['id'] for p in data['results']], [manual.pk])
        self.assertEqual(data['results'][0]['contract_external_id'], 'CTR-30')
        self.assertEqual(data['results'][0]['amount'], '1000.00')

        response = self.api().post(f'/api/1c/payments/{manual.pk}/link/', {'external_id': 'PKO-77'}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.api().get('/api/1c/payments/?unlinked=1').json()['count'], 0)

    def test_import_endpoint(self):
        response = self.api().post('/api/1c/import/payments/', [self.payment_record()], content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['created'], 1)
        self.assertEqual(SyncRun.objects.get().channel, 'api')

        response = self.api().post('/api/1c/import/payments/', {'records': [self.payment_record(external_id='X', contract_external_id='?')], 'dry_run': True},
                                   content_type='application/json')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.api().post('/api/1c/import/unknown/', [], content_type='application/json').status_code, 404)


class ReadOnlyAndPageTestCase(BaseData):
    def login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_1c_records_cannot_be_edited_or_reversed_on_platform(self):
        run_import('transactions', [{'external_id': 'TR-1', 'school_external_id': 'SCH-1', 'type': 'EXPENSE', 'date': '2026-10-01', 'amount': 5}])
        run_import('payments', [self.payment_record()])
        tr = Transaction.objects.get()
        payment = Payment.objects.get()
        client = self.login(self.director)
        self.assertEqual(client.get(reverse('transaction_edit', args=[tr.pk])).status_code, 404)
        self.assertEqual(client.post(reverse('transaction_reverse', args=[tr.pk]), {'reason': 'x'}).status_code, 404)
        self.assertEqual(client.post(reverse('payment_reverse', args=[payment.pk]), {'reason': 'x'}).status_code, 404)
        self.assertContains(client.get(reverse('transaction_list')), 'Документ из 1С')
        self.assertContains(client.get(reverse('contract_detail', args=[self.contract.pk])), 'Документ из 1С')

    def test_integration_page_owner_only_and_upload(self):
        self.assertEqual(self.login(self.director).get(reverse('integration')).status_code, 403)
        client = self.login(self.owner)
        self.assertContains(client.get(reverse('integration')), 'Журнал обменов')

        upload = SimpleUploadedFile('p.json', json.dumps([self.payment_record()]).encode('utf-8'), content_type='application/json')
        response = client.post(reverse('integration'), {'entity': 'payments', 'file': upload, 'dry_run': 'on'}, follow=True)
        self.assertContains(response, 'Пробный запуск')
        self.assertFalse(Payment.objects.exists())

        upload = SimpleUploadedFile('p.json', json.dumps({'records': [self.payment_record()]}).encode('utf-8'))
        client.post(reverse('integration'), {'entity': 'payments', 'file': upload})
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(SyncRun.objects.filter(dry_run=False).get().triggered_by, self.owner)

        upload = SimpleUploadedFile('bad.json', b'not json')
        response = client.post(reverse('integration'), {'entity': 'payments', 'file': upload})
        self.assertContains(response, 'Это не JSON')

    def test_management_command(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as fh:
            json.dump([self.payment_record()], fh)
        try:
            out = StringIO()
            call_command('sync_1c', 'payments', '--file', fh.name, stdout=out)
            self.assertIn('создано 1', out.getvalue())
            self.assertEqual(Payment.objects.count(), 1)
        finally:
            os.unlink(fh.name)


class SampleFilesTestCase(TestCase):
    """Пробные файлы из samples/1c загружаются с ожидаемым результатом, а cleanup_1c_demo всё убирает."""
    ENTITIES = {'01': 'schools', '02': 'employees', '03': 'contracts', '04': 'payments',
                '05': 'payouts', '06': 'transactions', '07': 'payments'}

    def setUp(self):
        self.school = School.objects.create(name="школа №1")
        owner = User.objects.create_user(username="s_owner", phone_number="+998500022131", password="x", role="OWNER")
        User.objects.create_user(username="s_emp", phone_number="+998500000001", password="x", role="EMPLOYEE", school=self.school)
        self.contract = Contract.objects.create(number="30", user=owner, school=self.school, date=date(2025, 8, 4),
                                                duration_months=10, tuition_fee=Decimal(1450200), food_cost=Decimal(576800))
        Contract.objects.create(number="1", user=owner, school=self.school, date=date(2026, 6, 27))

    def load(self, name):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'samples', '1c', name)
        with open(path, encoding='utf-8') as fh:
            return run_import(self.ENTITIES[name[:2]], json.load(fh))

    def test_samples_in_order_and_cleanup(self):
        expected = {
            '01_schools.json': (0, 1, 0, 0), '02_employees.json': (0, 1, 0, 0), '03_contracts.json': (0, 2, 0, 0),
            '04_payments.json': (3, 0, 0, 2), '05_payouts.json': (2, 0, 0, 0), '06_transactions.json': (3, 0, 0, 1),
            '07_payments_second_exchange.json': (0, 1, 1, 0),
        }
        for name, counts in expected.items():
            run = self.load(name)
            self.assertEqual((run.created, run.updated, run.reversed, run.failed), counts, name)

        payment = Payment.objects.get(external_id='DEMO-PAY-003')
        self.assertEqual(payment.amount, Decimal(2027000))
        self.assertTrue(Payment.objects.get(external_id='DEMO-PAY-002').is_reversed)

        out = StringIO()
        call_command('cleanup_1c_demo', stdout=out)
        self.assertIn('Ничего не изменено', out.getvalue())
        self.assertEqual(Payment.objects.count(), 3)

        call_command('cleanup_1c_demo', '--yes', stdout=StringIO())
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(EmployeePayout.objects.exists())
        self.assertFalse(Transaction.objects.exists())
        self.school.refresh_from_db()
        self.contract.refresh_from_db()
        self.assertIsNone(self.school.external_id)
        self.assertIsNone(self.contract.external_id)
        self.assertEqual(SyncRun.objects.count(), 7)  # журнал обменов остаётся как история
