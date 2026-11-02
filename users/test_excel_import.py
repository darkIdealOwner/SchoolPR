"""Загрузка из Excel: разбор файла, проверка строк, права ролей, защита от задвоения."""
import csv
import io
from datetime import date
from decimal import Decimal

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from users import excel_import
from users.excel_import import RowError, parse_amount, parse_choice, parse_date, parse_month
from users.models import AuditLog, Contract, EmployeePayout, Payment, School, Transaction, User

PASSWORD = 'Strong-pass-2026'

CONTRACT_HEADERS = ['№ договора', 'Дата', 'ФИО ученика', 'Год рождения', 'Класс', 'Срок, мес', 'Направление',
                    'ФИО родителей', 'Паспорт', 'ИНПС', 'Адрес', 'Телефон', 'Стоимость', 'Скидка %', 'Питание']


def contract_row(number, **overrides):
    values = {
        '№ договора': number, 'Дата': '01.09.2026', 'ФИО ученика': f'Ученик {number}', 'Год рождения': 2015,
        'Класс': '5-А', 'Срок, мес': 9, 'Направление': 'рус', 'ФИО родителей': 'Родитель', 'Паспорт': 'AA1234567',
        'ИНПС': '12345678901234', 'Адрес': 'Ташкент', 'Телефон': '+998 90 111 22 33', 'Стоимость': '1 000 000',
        'Скидка %': 10, 'Питание': 200000,
    }
    values.update(overrides)
    return values


def xlsx(headers, rows, title_rows=0):
    """Файл .xlsx: строка заголовков и строки-словари (ключи — заголовки)."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for _ in range(title_rows):
        sheet.append(['Отчёт школы за сентябрь'])
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(h) for h in headers])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return SimpleUploadedFile('table.xlsx', buffer.getvalue())


def csv_file(headers, rows, encoding='utf-8-sig', delimiter=';'):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row.get(h, '') for h in headers])
    return SimpleUploadedFile('table.csv', buffer.getvalue().encode(encoding))


class ParsingTests(TestCase):
    def test_amounts(self):
        cases = {
            '1 500 000': Decimal(1500000), '1500000,50': Decimal('1500000.50'), '1.500.000': Decimal(1500000),
            '1,500': Decimal(1500), '1 500 000,00 сум': Decimal(1500000), '1,500,000.25': Decimal('1500000.25'),
            '2.5': Decimal('2.5'), '1\xa0200\xa0000': Decimal(1200000), '750000': Decimal(750000),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_amount(text), expected, text)
        for text in ('abc', '', '1.2.3,4,5'):
            with self.assertRaises(RowError, msg=text):
                parse_amount(text)

    def test_dates(self):
        for text in ('01.09.2026', '2026-09-01', '01/09/2026', '1.9.2026', '2026-09-01 00:00:00'):
            self.assertEqual(parse_date(text), date(2026, 9, 1), text)
        for text in ('32.13.2026', 'вчера', '2026/31/31'):
            with self.assertRaises(RowError):
                parse_date(text)

    def test_months_in_every_language(self):
        for text in ('09.2026', '2026-09', '9/2026', 'Сентябрь 2026', 'сентября 2026', 'September 2026',
                     'Sep 2026', '2026년 9월', '2026-09-15', '15.09.2026'):
            self.assertEqual(parse_month(text), date(2026, 9, 1), text)
        for text in ('2026', 'Сентябрь', '13.2026'):
            with self.assertRaises(RowError, msg=text):
                parse_month(text)

    def test_choices(self):
        self.assertEqual(parse_choice('рус', Contract.SPECIALIZATION_CHOICES), 'Русский язык')
        self.assertEqual(parse_choice('УЗБЕКСКИЙ ЯЗЫК', Contract.SPECIALIZATION_CHOICES), 'Узбекский язык')
        self.assertEqual(parse_choice('Click', Payment.METHOD_CHOICES), 'CLICK')
        self.assertEqual(parse_choice('cash', Payment.METHOD_CHOICES), 'CASH')
        self.assertEqual(parse_choice('Наличные', Payment.METHOD_CHOICES), 'CASH')
        self.assertEqual(parse_choice('Приход', Transaction.TYPE_CHOICES), 'INCOME')
        self.assertEqual(parse_choice('расход', Transaction.TYPE_CHOICES), 'EXPENSE')
        with translation.override('en'):
            english = str(dict(Payment.METHOD_CHOICES)['BANK'])
        self.assertEqual(parse_choice(english, Payment.METHOD_CHOICES), 'BANK')
        for text in ('карта банка', 'кр'):
            with self.assertRaises(RowError, msg=text):
                parse_choice(text, Payment.METHOD_CHOICES if text != 'кр' else Contract.SPECIALIZATION_CHOICES)


class ImportTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school_a = School.objects.create(name='Школа А')
        cls.school_b = School.objects.create(name='Школа Б')

        def make(username, phone, role, school=None, name=None):
            return User.objects.create_user(username=username, phone_number=phone, password=PASSWORD,
                                            role=role, school=school, first_name=name or username)

        cls.owner = make('x_owner', '+998930000101', 'OWNER', name='Владелец')
        cls.director_a = make('x_dir_a', '+998930000102', 'DIRECTOR', cls.school_a, 'Директор А')
        cls.employee_a = make('x_emp_a', '+998930000103', 'EMPLOYEE', cls.school_a, 'Камола')
        cls.employee_a2 = make('x_emp_a2', '+998930000104', 'EMPLOYEE', cls.school_a, 'Дилшод')
        cls.employee_b = make('x_emp_b', '+998930000105', 'EMPLOYEE', cls.school_b, 'Камола')
        cls.investor = make('x_inv', '+998930000106', 'INVESTOR', name='Инвестор')
        cls.director_no_school = make('x_dir_none', '+998930000107', 'DIRECTOR', None, 'Без школы')

        cls.contract_a = Contract.objects.create(
            number='A-1', school=cls.school_a, date=date(2026, 9, 1), duration_months=9,
            student_full_name='Ученик Альфа', tuition_fee=Decimal(1000000))
        cls.contract_b = Contract.objects.create(
            number='B-1', school=cls.school_b, date=date(2026, 9, 1), duration_months=9,
            student_full_name='Ученик Бета', tuition_fee=Decimal(2000000))

    def login(self, user):
        self.client.force_login(user)

    def upload(self, user, kind, file):
        self.login(user)
        return self.client.post(reverse('excel_import', args=[kind]), {'file': file})

    def confirm(self, response, kind):
        return self.client.post(reverse('excel_import', args=[kind]),
                                {'action': 'confirm', 'token': response.context['token']}, follow=True)

    def statuses(self, response):
        return [r.status for r in response.context['results']]

    def errors_text(self, response):
        return ' | '.join(e for r in response.context['results'] for e in r.errors)


class AccessTests(ImportTestBase):
    def test_pages_by_role(self):
        allowed = {
            'contracts': [self.owner, self.director_a, self.employee_a],
            'payments': [self.owner, self.director_a, self.employee_a],
            'payouts': [self.owner, self.director_a],
            'transactions': [self.owner, self.director_a],
        }
        everyone = [self.owner, self.director_a, self.employee_a, self.investor, self.director_no_school]
        for kind, users in allowed.items():
            for user in everyone:
                self.login(user)
                for name in ('excel_import', 'excel_template'):
                    code = self.client.get(reverse(name, args=[kind])).status_code
                    self.assertEqual(code, 200 if user in users else 403, f'{kind} {name} {user.username}')

    def test_unknown_kind_and_anonymous(self):
        self.login(self.owner)
        self.assertEqual(self.client.get('/import/users/').status_code, 404)
        self.client.logout()
        response = self.client.get(reverse('excel_import', args=['contracts']))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_investor_cannot_post(self):
        response = self.upload(self.investor, 'contracts', xlsx(CONTRACT_HEADERS, [contract_row('X-1')]))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Contract.objects.filter(number='X-1').exists())

    def test_list_pages_show_import_button(self):
        for user, url, expected in [
            (self.director_a, reverse('contract_list'), True), (self.employee_a, reverse('payment_list'), True),
            (self.director_a, reverse('payout_list'), True), (self.investor, reverse('contract_list'), False),
            (self.investor, reverse('transaction_list'), False),
        ]:
            self.login(user)
            body = self.client.get(url).content.decode()
            self.assertEqual('/import/' in body, expected, f'{user.username} {url}')


class TemplateTests(ImportTestBase):
    def load(self, user, kind):
        self.login(user)
        response = self.client.get(reverse('excel_template', args=[kind]))
        self.assertEqual(response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        return openpyxl.load_workbook(io.BytesIO(response.content))

    def test_owner_template_has_school_column_and_lists(self):
        workbook = self.load(self.owner, 'contracts')
        headers = [c.value for c in workbook.worksheets[0][1]]
        self.assertIn('№ договора *', headers)
        self.assertIn('Школа *', headers)
        self.assertIn('Примечание', headers)
        lists = workbook.worksheets[1]
        values = {c.value for col in lists.iter_cols() for c in col}
        self.assertIn('Школа Б', values)
        self.assertIn('Узбекский язык', values)

    def test_director_template_has_no_school_column(self):
        workbook = self.load(self.director_a, 'transactions')
        headers = [c.value for c in workbook.worksheets[0][1]]
        self.assertFalse(any(h.startswith('Школа') for h in headers))
        self.assertIn('Тип *', headers)

    def test_payout_template_lists_only_own_employees(self):
        workbook = self.load(self.director_a, 'payouts')
        values = ' '.join(str(c.value) for col in workbook.worksheets[1].iter_cols() for c in col)
        self.assertIn('Дилшод', values)
        self.assertNotIn('+998 (93)-000-01-05', values)  # сотрудник другой школы

    def test_template_in_english(self):
        self.client.cookies['django_language'] = 'en'
        workbook = self.load(self.owner, 'payments')
        headers = ' '.join(str(c.value) for c in workbook.worksheets[0][1])
        self.assertNotRegex(headers, '[А-Яа-яЁё]')

    def test_filled_template_imports_back(self):
        workbook = self.load(self.director_a, 'contracts')
        sheet = workbook.worksheets[0]
        headers = [c.value.rstrip(' *') for c in sheet[1]]
        sheet.append([contract_row('T-1').get(h) for h in headers])
        buffer = io.BytesIO()
        workbook.save(buffer)
        response = self.upload(self.director_a, 'contracts', SimpleUploadedFile('t.xlsx', buffer.getvalue()))
        self.assertEqual(self.statuses(response), ['ok'], self.errors_text(response))


class ContractImportTests(ImportTestBase):
    def test_preview_saves_nothing_then_confirm_saves(self):
        response = self.upload(self.director_a, 'contracts',
                               xlsx(CONTRACT_HEADERS, [contract_row('N-1'), contract_row('N-2')]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.statuses(response), ['ok', 'ok'], self.errors_text(response))
        self.assertFalse(Contract.objects.filter(number__in=['N-1', 'N-2']).exists())

        result = self.confirm(response, 'contracts')
        self.assertRedirects(result, reverse('contract_list'))
        self.assertContains(result, 'загружено строк: 2')
        contract = Contract.objects.get(number='N-1')
        self.assertEqual(contract.school, self.school_a)
        self.assertEqual(contract.user, self.director_a)
        self.assertEqual(contract.specialization, 'Русский язык')
        self.assertEqual(contract.phone_number, '+998901112233')
        self.assertEqual(contract.total_amount, Decimal(1100000))   # 1 000 000 − 10% + 200 000 питание
        self.assertEqual(contract.month, 'Сентябрь')
        self.assertEqual(contract.source, 'MANUAL')
        log = AuditLog.objects.get(action='CREATE', object_id=str(contract.pk), model_name=Contract._meta.verbose_name)
        self.assertEqual(log.changes['source'], 'excel')
        self.assertEqual(log.user, self.director_a)

    def test_director_cannot_put_contract_into_other_school(self):
        headers = CONTRACT_HEADERS + ['Школа']
        response = self.upload(self.director_a, 'contracts', xlsx(headers, [contract_row('N-3', **{'Школа': 'Школа Б'})]))
        self.assertIn('Школа', response.context['ignored'])
        self.confirm(response, 'contracts')
        self.assertEqual(Contract.objects.get(number='N-3').school, self.school_a)

    def test_owner_must_name_existing_school(self):
        headers = CONTRACT_HEADERS + ['Школа']
        response = self.upload(self.owner, 'contracts', xlsx(headers, [
            contract_row('N-4', **{'Школа': '  школа   б '}), contract_row('N-5', **{'Школа': 'Школа Ы'}),
            contract_row('N-6', **{'Школа': ''}),
        ]))
        self.assertEqual(self.statuses(response), ['ok', 'error', 'error'])
        self.assertIn('Школа Ы', self.errors_text(response))
        self.confirm(response, 'contracts')
        self.assertEqual(Contract.objects.get(number='N-4').school, self.school_b)
        self.assertFalse(Contract.objects.filter(number__in=['N-5', 'N-6']).exists())

    def test_owner_file_without_school_column_is_rejected(self):
        response = self.upload(self.owner, 'contracts', xlsx(CONTRACT_HEADERS, [contract_row('N-7')]))
        self.assertIn('Школа', response.context['form'].errors['file'][0])
        self.assertNotIn('results', response.context)

    def test_row_errors_name_the_column(self):
        response = self.upload(self.director_a, 'contracts', xlsx(CONTRACT_HEADERS, [
            contract_row('E-1', **{'Дата': '31.02.2026'}),
            contract_row('E-2', **{'ФИО ученика': ''}),
            contract_row('E-3', **{'Скидка %': 150}),
            contract_row('E-4', **{'Стоимость': 'много'}),
            contract_row('E-5', **{'Направление': 'английский'}),
            contract_row('E-6', **{'Срок, мес': 0}),
            contract_row('E-7', **{'Год рождения': '2015.5'}),
        ]))
        self.assertEqual(self.statuses(response), ['error'] * 7)
        errors = [r.errors for r in response.context['results']]
        self.assertIn('«Дата»', errors[0][0])
        self.assertEqual(errors[1], ['«ФИО ученика»: не заполнено'])
        self.assertIn('«Скидка %»', errors[2][0])
        self.assertIn('много', errors[3][0])
        self.assertIn('английский', errors[4][0])
        self.assertIn('«Срок, мес»', errors[5][0])
        self.assertIn('«Год рождения»', errors[6][0])
        self.assertTrue(all(len(e) == 1 for e in errors), errors)   # одна понятная ошибка, без «Обязательное поле»

    def test_existing_and_repeated_numbers(self):
        response = self.upload(self.director_a, 'contracts', xlsx(CONTRACT_HEADERS, [
            contract_row('A-1'), contract_row('N-8'), contract_row('N-8'),
        ]))
        self.assertEqual(self.statuses(response), ['exists', 'ok', 'error'])
        self.assertIn('строке 3', self.errors_text(response))

    def test_same_file_twice_adds_nothing(self):
        rows = [contract_row('R-1'), contract_row('R-2')]
        self.confirm(self.upload(self.director_a, 'contracts', xlsx(CONTRACT_HEADERS, rows)), 'contracts')
        response = self.upload(self.director_a, 'contracts', xlsx(CONTRACT_HEADERS, rows))
        self.assertEqual(self.statuses(response), ['exists', 'exists'])
        self.assertContains(response, 'disabled')
        self.assertEqual(Contract.objects.filter(number__startswith='R-').count(), 2)

    def test_cancelled_contract(self):
        headers = CONTRACT_HEADERS + ['Отменён', 'Дата отмены']
        response = self.upload(self.director_a, 'contracts', xlsx(headers, [
            contract_row('C-1', **{'Отменён': 'да', 'Дата отмены': '15.12.2026'}),
            contract_row('C-2', **{'Отменён': 'да'}),
            contract_row('C-3', **{'Отменён': 'нет'}),
            contract_row('C-4', **{'Отменён': 'да', 'Дата отмены': '01.01.2026'}),
        ]))
        self.assertEqual(self.statuses(response), ['ok', 'ok', 'ok', 'error'])
        self.confirm(response, 'contracts')
        cancelled = Contract.objects.get(number='C-1')
        self.assertTrue(cancelled.is_cancelled)
        self.assertEqual(cancelled.cancelled_at, date(2026, 12, 15))
        # Дата отмены неизвестна — договор отменён, дата пустая, начислений без оплат нет
        no_date = Contract.objects.get(number='C-2')
        self.assertTrue(no_date.is_cancelled)
        self.assertIsNone(no_date.cancelled_at)
        self.assertIsNone(no_date.last_billing_month)
        self.assertFalse(Contract.objects.get(number='C-3').is_cancelled)

    def test_academic_year_and_first_month_columns(self):
        headers = CONTRACT_HEADERS + ['Учебный год', 'Первый месяц обучения']
        response = self.upload(self.director_a, 'contracts', xlsx(headers, [
            contract_row('Y-1', **{'Дата': '01.06.2026'}),
            contract_row('Y-2', **{'Дата': '01.06.2026', 'Учебный год': '2026-2027', 'Первый месяц обучения': 'окт 2026'}),
            contract_row('Y-3', **{'Учебный год': '2030–2031'}),
        ]))
        self.assertEqual(self.statuses(response), ['ok', 'ok', 'error'], self.errors_text(response))
        self.confirm(response, 'contracts')
        auto = Contract.objects.get(number='Y-1')
        self.assertEqual(auto.academic_year.name, '2026–2027')
        self.assertEqual(auto.first_billing_month, date(2026, 9, 1))   # договор от июня → с сентября
        self.assertEqual(Contract.objects.get(number='Y-2').first_billing_month, date(2026, 10, 1))

    def test_real_excel_cell_types(self):
        row = contract_row('Z-1', **{'Дата': date(2026, 9, 1), 'Стоимость': 1000000.0, 'Телефон': 998901112233,
                                     'ИНПС': 12345678901234, 'Питание': None, 'Скидка %': None})
        response = self.upload(self.director_a, 'contracts', xlsx(CONTRACT_HEADERS, [row]))
        self.assertEqual(self.statuses(response), ['ok'], self.errors_text(response))
        self.confirm(response, 'contracts')
        contract = Contract.objects.get(number='Z-1')
        self.assertEqual(contract.inps, '12345678901234')
        self.assertEqual(contract.phone_number, '+998901112233')
        self.assertEqual(contract.total_amount, Decimal(1000000))

    def test_title_rows_and_empty_rows_are_skipped(self):
        file = xlsx(CONTRACT_HEADERS, [contract_row('H-1'), {}, contract_row('H-2')], title_rows=2)
        response = self.upload(self.director_a, 'contracts', file)
        self.assertEqual([r.number for r in response.context['results']], [4, 6])
        self.assertEqual(self.statuses(response), ['ok', 'ok'])

    def test_headers_in_other_languages(self):
        for lang in ('en', 'uz', 'ko'):
            with translation.override(lang):
                columns = {c.key: str(c.label) for c in excel_import.KINDS['contracts']['columns']}
            russian = {str(c.label): c.key for c in excel_import.KINDS['contracts']['columns']}
            headers = [columns[russian[h]] for h in CONTRACT_HEADERS]
            row = {columns[russian[h]]: v for h, v in contract_row(f'L-{lang}').items()}
            response = self.upload(self.director_a, 'contracts', xlsx(headers, [row]))
            self.assertEqual(self.statuses(response), ['ok'], f'{lang}: {self.errors_text(response)}')

    def test_sloppy_headers(self):
        sloppy = {'№ договора': '№договора', 'ФИО ученика': '  фио   УЧЕНИКА ', 'Срок, мес': 'Срок мес.',
                  'Скидка %': 'Скидка, %', 'Телефон': 'Телефон:', 'Год рождения': 'Год рождения *'}
        headers = [sloppy.get(h, h) for h in CONTRACT_HEADERS]
        row = {sloppy.get(h, h): v for h, v in contract_row('S-1').items()}
        response = self.upload(self.director_a, 'contracts', xlsx(headers, [row]))
        self.assertEqual(self.statuses(response), ['ok'], self.errors_text(response))
        self.assertEqual(response.context['ignored'], [])

    def test_column_names_do_not_collide(self):
        for kind in excel_import.KINDS:
            seen = {}
            for column in excel_import.KINDS[kind]['columns']:
                for name in column.names():
                    self.assertIn(seen.setdefault(name, column.key), (column.key,), f'{kind}: «{name}»')

    def test_csv_in_cp1251_and_comma(self):
        response = self.upload(self.director_a, 'contracts',
                               csv_file(CONTRACT_HEADERS, [contract_row('V-1')], encoding='cp1251'))
        self.assertEqual(self.statuses(response), ['ok'], self.errors_text(response))
        rows = [contract_row('V-2', **{'Стоимость': '1000000'})]
        response = self.upload(self.director_a, 'contracts', csv_file(CONTRACT_HEADERS, rows, delimiter=','))
        self.assertEqual(self.statuses(response), ['ok'], self.errors_text(response))

    def test_export_can_be_imported_back(self):
        self.login(self.owner)
        exported = self.client.get(reverse('contract_list') + '?export=csv').content.decode('utf-8-sig')
        changed = exported.replace('A-1;', 'COPY-1;')
        response = self.upload(self.owner, 'contracts', SimpleUploadedFile('dogovory.csv', changed.encode('utf-8-sig')))
        by_status = {r.summary[0] if r.summary else '': r.status for r in response.context['results']}
        self.assertEqual(by_status.get('B-1'), 'exists')
        # У договора А-1 из тестовых данных не заполнены паспорт и др. — их покажут как ошибки
        copy = [r for r in response.context['results'] if r.status != 'exists']
        self.assertEqual(len(copy), 1)
        self.assertIn('не заполнено', ' '.join(copy[0].errors))

    def test_bad_files(self):
        cases = [
            (SimpleUploadedFile('old.xls', b'\xd0\xcf\x11\xe0'), '.xls'),
            (SimpleUploadedFile('broken.xlsx', b'not a zip'), '.xlsx'),
            (SimpleUploadedFile('photo.jpg', b'\xff\xd8'), 'CSV'),
            (xlsx(CONTRACT_HEADERS, []), 'нет строк'),
            (xlsx(['Колонка 1', 'Колонка 2'], [{'Колонка 1': 1}]), 'названиями колонок'),
            (xlsx(['№ договора', 'Дата'], [{'№ договора': 'Q', 'Дата': '01.09.2026'}]), 'ФИО ученика'),
        ]
        for file, expected in cases:
            response = self.upload(self.director_a, 'contracts', file)
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected, response.context['form'].errors['file'][0], file.name)

    def test_too_many_rows(self):
        old = excel_import.MAX_ROWS
        excel_import.MAX_ROWS = 3
        try:
            response = self.upload(self.director_a, 'contracts',
                                   xlsx(CONTRACT_HEADERS, [contract_row(f'M-{i}') for i in range(5)]))
        finally:
            excel_import.MAX_ROWS = old
        self.assertIn('3', response.context['form'].errors['file'][0])


class ConfirmTests(ImportTestBase):
    def test_wrong_token_and_double_confirm(self):
        response = self.upload(self.director_a, 'contracts', xlsx(CONTRACT_HEADERS, [contract_row('D-1')]))
        bad = self.client.post(reverse('excel_import', args=['contracts']), {'action': 'confirm', 'token': 'x'}, follow=True)
        self.assertContains(bad, 'устарела')
        self.assertFalse(Contract.objects.filter(number='D-1').exists())
        self.confirm(response, 'contracts')
        again = self.confirm(response, 'contracts')
        self.assertContains(again, 'устарела')
        self.assertEqual(Contract.objects.filter(number='D-1').count(), 1)

    def test_cancel_forgets_file(self):
        response = self.upload(self.director_a, 'contracts', xlsx(CONTRACT_HEADERS, [contract_row('D-2')]))
        self.client.post(reverse('excel_import', args=['contracts']), {'action': 'cancel'})
        self.assertNotIn('excel_import', self.client.session)
        result = self.confirm(response, 'contracts')
        self.assertContains(result, 'устарела')
        self.assertFalse(Contract.objects.filter(number='D-2').exists())

    def test_confirm_rechecks_data(self):
        response = self.upload(self.director_a, 'contracts',
                               xlsx(CONTRACT_HEADERS, [contract_row('D-3'), contract_row('D-4')]))
        Contract.objects.create(number='D-3', school=self.school_a, student_full_name='Вручную')
        self.confirm(response, 'contracts')
        self.assertEqual(Contract.objects.get(number='D-3').student_full_name, 'Вручную')
        self.assertTrue(Contract.objects.filter(number='D-4').exists())

    def test_session_of_other_kind_is_not_used(self):
        response = self.upload(self.director_a, 'contracts', xlsx(CONTRACT_HEADERS, [contract_row('D-5')]))
        result = self.client.post(reverse('excel_import', args=['payments']),
                                  {'action': 'confirm', 'token': response.context['token']}, follow=True)
        self.assertContains(result, 'устарела')
        self.assertFalse(Contract.objects.filter(number='D-5').exists())


PAYMENT_HEADERS = ['№ договора', 'Дата оплаты', 'За месяц', 'Сумма', 'Способ оплаты', 'Примечание']


class PaymentImportTests(ImportTestBase):
    def test_employee_imports_payments_of_own_school(self):
        response = self.upload(self.employee_a, 'payments', xlsx(PAYMENT_HEADERS, [
            {'№ договора': 'A-1', 'Дата оплаты': '05.09.2026', 'За месяц': '09.2026', 'Сумма': '1 000 000', 'Способ оплаты': 'Click'},
            {'№ договора': 'A-1', 'Дата оплаты': '03.10.2026', 'Сумма': 500000},
            {'№ договора': 'B-1', 'Дата оплаты': '05.09.2026', 'Сумма': 100},
            {'№ договора': 'A-1', 'Дата оплаты': '05.09.2026', 'Сумма': 0},
            {'№ договора': 'НЕТ', 'Дата оплаты': '05.09.2026', 'Сумма': 100},
        ]))
        self.assertEqual(self.statuses(response), ['ok', 'ok', 'error', 'error', 'error'])
        self.assertIn('«B-1» не найден', self.errors_text(response))
        self.assertNotIn('Бета', response.content.decode())   # чужой договор не раскрывается
        self.confirm(response, 'payments')
        first, second = Payment.objects.filter(contract=self.contract_a).order_by('payment_date')
        self.assertEqual((first.method, first.for_month, first.amount), ('CLICK', date(2026, 9, 1), Decimal(1000000)))
        self.assertEqual((second.method, second.for_month), ('CASH', date(2026, 10, 1)))
        self.assertEqual(first.created_by, self.employee_a)
        self.assertFalse(Payment.objects.filter(contract=self.contract_b).exists())

    def test_duplicate_payment_is_skipped(self):
        Payment.objects.create(contract=self.contract_a, for_month=date(2026, 9, 1), amount=Decimal(1000000),
                               payment_date=date(2026, 9, 5))
        row = {'№ договора': 'A-1', 'Дата оплаты': '05.09.2026', 'За месяц': 'Сентябрь 2026', 'Сумма': 1000000}
        response = self.upload(self.director_a, 'payments', xlsx(PAYMENT_HEADERS, [row]))
        self.assertEqual(self.statuses(response), ['exists'])

    def test_reversed_payment_does_not_block(self):
        payment = Payment.objects.create(contract=self.contract_a, for_month=date(2026, 9, 1), amount=Decimal(1000000),
                                         payment_date=date(2026, 9, 5))
        payment.reverse(self.director_a, 'ошибка')
        row = {'№ договора': 'A-1', 'Дата оплаты': '05.09.2026', 'Сумма': 1000000}
        response = self.upload(self.director_a, 'payments', xlsx(PAYMENT_HEADERS, [row]))
        self.assertEqual(self.statuses(response), ['ok'])

    def test_month_outside_contract_gives_warning(self):
        row = {'№ договора': 'A-1', 'Дата оплаты': '05.09.2027', 'Сумма': 1000000}
        response = self.upload(self.director_a, 'payments', xlsx(PAYMENT_HEADERS, [row]))
        result = response.context['results'][0]
        self.assertEqual(result.status, 'ok')
        self.assertTrue(result.notes)

    def test_owner_sees_every_school(self):
        row = {'№ договора': 'B-1', 'Дата оплаты': '05.09.2026', 'Сумма': 2000000, 'Способ оплаты': 'перечисление'}
        response = self.upload(self.owner, 'payments', xlsx(PAYMENT_HEADERS, [row]))
        self.assertEqual(self.statuses(response), ['ok'], self.errors_text(response))
        self.confirm(response, 'payments')
        self.assertEqual(Payment.objects.get(contract=self.contract_b).method, 'BANK')


PAYOUT_HEADERS = ['Сотрудник', 'За месяц', 'Сумма', 'Дата выплаты', 'Сторно']


class PayoutImportTests(ImportTestBase):
    def test_director_by_phone_and_name(self):
        response = self.upload(self.director_a, 'payouts', xlsx(PAYOUT_HEADERS, [
            {'Сотрудник': '+998 (93)-000-01-03', 'За месяц': '09.2026', 'Сумма': 3000000, 'Дата выплаты': '30.09.2026'},
            {'Сотрудник': 'дилшод', 'За месяц': 'Сентябрь 2026', 'Сумма': 2500000, 'Дата выплаты': '30.09.2026'},
            {'Сотрудник': 'Дилшод — +998 (93)-000-01-04', 'За месяц': '10.2026', 'Сумма': 2500000, 'Дата выплаты': '31.10.2026'},
            {'Сотрудник': '+998930000105', 'За месяц': '09.2026', 'Сумма': 1, 'Дата выплаты': '30.09.2026'},
            {'Сотрудник': 'Дилшод', 'За месяц': '09.2026', 'Сумма': 100, 'Дата выплаты': '30.09.2026', 'Сторно': 'да'},
        ]))
        self.assertEqual(self.statuses(response), ['ok', 'ok', 'ok', 'error', 'skipped'], self.errors_text(response))
        self.confirm(response, 'payouts')
        self.assertEqual(EmployeePayout.objects.filter(school=self.school_a).count(), 3)
        self.assertFalse(EmployeePayout.objects.filter(employee=self.employee_b).exists())
        self.assertEqual(EmployeePayout.objects.first().created_by, self.director_a)

    def test_owner_ambiguous_name_and_default_school(self):
        response = self.upload(self.owner, 'payouts', xlsx(PAYOUT_HEADERS + ['Школа'], [
            {'Сотрудник': 'Камола', 'За месяц': '09.2026', 'Сумма': 100, 'Дата выплаты': '30.09.2026'},
            {'Сотрудник': '+998930000105', 'За месяц': '09.2026', 'Сумма': 100, 'Дата выплаты': '30.09.2026'},
        ]))
        self.assertEqual(self.statuses(response), ['error', 'ok'])
        self.assertIn('укажите телефон', self.errors_text(response))
        self.confirm(response, 'payouts')
        self.assertEqual(EmployeePayout.objects.get().school, self.school_b)

    def test_export_of_payouts_round_trip(self):
        EmployeePayout.objects.create(employee=self.employee_a2, school=self.school_a, for_month=date(2026, 9, 1),
                                      amount=Decimal(2500000), payout_date=date(2026, 9, 30))
        self.login(self.director_a)
        exported = self.client.get(reverse('payout_list') + '?export=csv').content
        response = self.upload(self.director_a, 'payouts', SimpleUploadedFile('vyplaty.csv', exported))
        self.assertEqual(self.statuses(response), ['exists'], self.errors_text(response))


TRANSACTION_HEADERS = ['Дата', 'Тип', 'Категория', 'Сумма', 'Примечание']


class TransactionImportTests(ImportTestBase):
    def test_director_school_is_forced(self):
        response = self.upload(self.director_a, 'transactions', xlsx(TRANSACTION_HEADERS + ['Школа'], [
            {'Дата': '10.09.2026', 'Тип': 'Расход', 'Категория': 'Аренда', 'Сумма': '5 000 000', 'Школа': 'Школа Б'},
            {'Дата': '11.09.2026', 'Тип': 'приход', 'Сумма': 10000000},
            {'Дата': '11.09.2026', 'Тип': 'Подарок', 'Сумма': 10},
        ]))
        self.assertEqual(self.statuses(response), ['ok', 'ok', 'error'])
        self.confirm(response, 'transactions')
        self.assertEqual(Transaction.objects.filter(school=self.school_a).count(), 2)
        self.assertEqual(Transaction.objects.get(category='Аренда').type, 'EXPENSE')
        self.assertFalse(Transaction.objects.filter(school=self.school_b).exists())

    def test_owner_needs_school_and_duplicates_are_skipped(self):
        Transaction.objects.create(school=self.school_b, type='INCOME', category='Инвестиция',
                                   amount=Decimal(1000), date=date(2026, 9, 1))
        response = self.upload(self.owner, 'transactions', xlsx(TRANSACTION_HEADERS + ['Школа', 'Сторно'], [
            {'Дата': '01.09.2026', 'Тип': 'Приход', 'Категория': 'Инвестиция', 'Сумма': 1000, 'Школа': 'Школа Б'},
            {'Дата': '01.09.2026', 'Тип': 'Приход', 'Сумма': 1000, 'Школа': ''},
            {'Дата': '02.09.2026', 'Тип': 'Приход', 'Сумма': 1000, 'Школа': 'Школа А', 'Сторно': 'да'},
            {'Дата': '02.09.2026', 'Тип': 'Приход', 'Сумма': 1000, 'Школа': 'Школа А'},
        ]))
        self.assertEqual(self.statuses(response), ['exists', 'error', 'skipped', 'ok'])
        self.confirm(response, 'transactions')
        self.assertEqual(Transaction.objects.count(), 2)


class PageTranslationTests(ImportTestBase):
    def test_import_pages_have_no_russian_in_other_languages(self):
        for lang in ('uz', 'en', 'ko'):
            self.client.cookies['django_language'] = lang
            for kind in excel_import.KINDS:
                self.login(self.owner)
                body = self.client.get(reverse('excel_import', args=[kind])).content.decode()
                main = body.split('<main>', 1)[1].split('</main>', 1)[0]
                self.assertNotRegex(main, '[А-Яа-яЁё]', f'{lang} {kind}')

    def test_preview_has_no_russian_in_english(self):
        self.client.cookies['django_language'] = 'en'
        with translation.override('en'):
            headers = [str(c.label) for c in excel_import.KINDS['transactions']['columns'] if c.key in ('date', 'type', 'amount')]
        response = self.upload(self.director_a, 'transactions', xlsx(headers, [
            {headers[0]: '10.09.2026', headers[1]: 'Expense', headers[2]: 100},
            {headers[0]: 'bad', headers[1]: 'Expense', headers[2]: 100},
        ]))
        main = response.content.decode().split('<main>', 1)[1].split('</main>', 1)[0]
        self.assertNotRegex(main.replace(self.school_a.name, ''), '[А-Яа-яЁё]')   # кроме названия школы из данных
