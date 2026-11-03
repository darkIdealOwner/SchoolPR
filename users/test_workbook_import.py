"""Загрузка книги школы целиком: на выдуманной книге той же структуры, что у школ (все данные вымышленные)."""
import io
from datetime import date, datetime
from decimal import Decimal

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from users.models import (
    Account, AcademicYear, Category, Contract, Counterparty, Payment, School, Transaction, Transfer, UnidentifiedReceipt, User,
)
from users.workbook_import import book_phones, discount_parts

PASSWORD = 'Strong-pass-2026'
D = Decimal

CONTRACT_HEADERS = ['№ договора', 'Дата', 'Месяц', 'ФИО ученика', 'Год рождения', 'Класс', 'Срок, мес', 'ФИО родителей',
                    'Серия и номер паспорта', 'ИНПС', 'Адрес', '№ телефона', 'Направление', 'Стоимость учебы', 'Скидка, %',
                    'Скидка, сум', 'Стоимость питание', 'Сумма', 'Отметка об отмены договора', 'Примечание']
INCOME_HEADERS = ['Дата', 'Месяц', 'Вид поступление', '№ договора', 'Оплата за период', 'Сумма', 'Назначение платежа', 'Краткое назначение']
EXPENSE_HEADERS = ['Дата', 'Месяц', 'Вид расходы', 'Сумма', 'Назначение платежа', 'Краткое назначение', 'Примечание']


def contract(number, **kw):
    row = {'№ договора': number, 'Дата': datetime(2026, 6, 1), 'ФИО ученика': f'Тест Ученик {number}', 'Год рождения': 2015,
           'Класс': 4, 'Срок, мес': 10, 'ФИО родителей': 'Тест Родитель', 'Серия и номер паспорта': f'AA{1000000 + number}',
           'ИНПС': 30101990000000 + number, 'Адрес': 'Тестовая МФЙ', '№ телефона': 901234567, 'Направление': 'rus tili',
           'Стоимость учебы': 1936000, 'Стоимость питание': 616000, 'Сумма': 2552000, 'Отметка об отмены договора': 'Актуальный'}
    row.update(kw)
    return row


def book(contracts, income, expense, title_rows=(2, 3, 3)):
    wb = openpyxl.Workbook()
    wb.active.title = 'DASHBOARD'
    for name, headers, rows, skip in (('Договоры', CONTRACT_HEADERS, contracts, title_rows[0]),
                                      ('Поступление ДС', INCOME_HEADERS, income, title_rows[1]),
                                      ('Расходы ДС', EXPENSE_HEADERS, expense, title_rows[2])):
        ws = wb.create_sheet(name)
        for _ in range(skip):
            ws.append([None, name])
        ws.append([None] + headers)
        for row in rows:
            ws.append([None] + [row.get(h) for h in headers])
    buffer = io.BytesIO()
    wb.save(buffer)
    return SimpleUploadedFile('AsiaSchool_Test_2026-2027.xlsx', buffer.getvalue())


INCOME = [
    {'Дата': datetime(2026, 7, 8), 'Вид поступление': 'Перечисление', '№ договора': 1, 'Оплата за период': 'сент.26', 'Сумма': 2552000, 'Назначение платежа': 'оплата', 'Краткое назначение': 'Таълим пули'},
    {'Дата': datetime(2026, 10, 2), 'Вид поступление': 'Наличный расчет', '№ договора': 1, 'Оплата за период': 'окт.26', 'Сумма': 1000000, 'Краткое назначение': 'Таълим пули'},
    {'Дата': datetime(2026, 10, 3), 'Вид поступление': 'Наличный расчет', '№ договора': 1, 'Оплата за период': 'окт.26', 'Сумма': 1000000},
    {'Дата': datetime(2026, 9, 1), 'Вид поступление': 'Перечисление', '№ договора': 2, 'Оплата за период': 'сент.26', 'Сумма': 616000, 'Краткое назначение': 'Таълим пули'},
    {'Дата': datetime(2026, 9, 5), 'Вид поступление': 'Перечисление', 'Сумма': 500000, 'Назначение платежа': 'без номера', 'Краткое назначение': 'Таълим пули'},
    {'Дата': datetime(2026, 8, 1), 'Вид поступление': 'Перечисление', 'Сумма': 100000000, 'Краткое назначение': 'ООО Тест Инвест'},
    {'Дата': datetime(2026, 9, 10), 'Вид поступление': 'Наличный расчет', 'Сумма': 30000000, 'Краткое назначение': 'Банкдан олинган пул'},
    {'Дата': datetime(2026, 9, 11), 'Вид поступление': 'Наличный расчет', 'Сумма': 5000, 'Краткое назначение': 'Бошқалар'},
    {'Дата': datetime(2023, 9, 25), 'Вид поступление': 'Наличный расчет', '№ договора': 1, 'Оплата за период': 'сент.26', 'Сумма': 1},
    {'Дата': datetime(2026, 9, 25), 'Вид поступление': 'Наличный расчет', '№ договора': 99, 'Оплата за период': 'сент.26', 'Сумма': 1},
]
EXPENSE = [
    {'Дата': datetime(2026, 8, 4), 'Вид расходы': 'Наличный расчет', 'Сумма': 1000000, 'Назначение платежа': 'аванс', 'Краткое назначение': 'Иш ҳақи'},
    {'Дата': datetime(2026, 8, 5), 'Вид расходы': 'Перечисление', 'Сумма': 2000000, 'Краткое назначение': 'ООО Тест Инвест'},
    {'Дата': datetime(2026, 9, 9), 'Вид расходы': 'Перечисление', 'Сумма': 30000000, 'Краткое назначение': 'Банкдан олинган нақд пул'},
    {'Дата': datetime(2026, 9, 12), 'Вид расходы': 'Наличный расчет', 'Сумма': 4000000, 'Краткое назначение': 'Ҳисоб рақамни тўлдириш'},
    {'Дата': datetime(2026, 9, 13), 'Вид расходы': 'Наличный расчет', 'Сумма': 700000, 'Краткое назначение': 'Таълим пули'},
    {'Дата': datetime(2026, 9, 14), 'Вид расходы': 'Перечисление', 'Сумма': 12345.67, 'Краткое назначение': 'Банк хизмати'},
    {'Дата': datetime(2006, 9, 19), 'Вид расходы': 'Наличный расчет', 'Сумма': 1, 'Краткое назначение': 'Бошқалар'},
]
CONTRACTS = [
    contract(1, **{'№ телефона': '901234567\n907654321'}),
    contract(2, **{'Скидка, %': 1, 'Сумма': 616000, 'Примечание': 'ijtimoiy reestr'}),
    contract(3, **{'Скидка, %': 0.1, 'Сумма': 2358400, 'Примечание': '2-farzand'}),
    contract(4, **{'Скидка, %': 1, 'Скидка, сум': 968000, 'Сумма': 1584000, 'Примечание': 'hodim'}),
    contract(5, **{'Отметка об отмены договора': 'Отменено'}),
    contract(6, **{'Класс': 2013}),
]


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.get(name='2026–2027')
        cls.school = School.objects.create(name='Тест Заамин')
        cls.other = School.objects.create(name='Тест Другая')
        make = lambda u, phone, role, school=None: User.objects.create_user(username=u, phone_number=phone, password=PASSWORD,
                                                                              role=role, school=school, first_name=u)
        cls.owner = make('wb_owner', '+998930000801', 'OWNER')
        cls.director = make('wb_dir', '+998930000802', 'DIRECTOR', cls.school)
        cls.employee = make('wb_emp', '+998930000803', 'EMPLOYEE', cls.school)
        cls.investor = make('wb_inv', '+998930000804', 'INVESTOR')

    def upload(self, user, file, **fields):
        self.client.force_login(user)
        data = {'file': file, 'academic_year': self.year.pk, 'prefix': fields.pop('prefix', '')}
        if user.role == 'OWNER':
            data['school'] = fields.pop('school', self.school).pk
        return self.client.post(reverse('workbook_import'), data)

    def confirm(self, response):
        return self.client.post(reverse('workbook_import'), {'action': 'confirm', 'token': response.context['token']})


class WorkbookFlowTests(Base):
    def test_full_import_and_reconciliation(self):
        preview = self.upload(self.owner, book(CONTRACTS, INCOME, EXPENSE))
        counts = preview.context['plan'].counts()
        # Класс 2013 и даты 2023/2006 — ошибки книги, но строки загружаются с пометкой; договора № 99 нет — ошибка
        self.assertEqual(counts['contracts'], {'ok': 6, 'exists': 0, 'skipped': 0, 'error': 0, 'flagged': 1, 'fixed': 0})
        self.assertEqual(counts['income'], {'ok': 8, 'exists': 0, 'skipped': 1, 'error': 1, 'flagged': 1, 'fixed': 0})
        self.assertEqual(counts['expense'], {'ok': 7, 'exists': 0, 'skipped': 0, 'error': 0, 'flagged': 1, 'fixed': 0})
        self.assertFalse(Contract.objects.exists())   # предпросмотр ничего не сохраняет

        result = self.confirm(preview)
        created = result.context['created']
        self.assertEqual((created['contracts'], created['payment'], created['receipt'], created['transaction'], created['transfer']),
                         (6, 5, 1, 7, 2))
        self.assertEqual(created['flagged'], 3)
        recon = result.context['recon']
        self.assertTrue(recon['ok'], [c for c in recon['checks'] if not c['ok']])
        self.assertContains(result, 'совпадают')

        c1 = Contract.objects.get(number='1')
        self.assertEqual((c1.school, c1.academic_year, c1.first_billing_month), (self.school, self.year, date(2026, 9, 1)))
        self.assertEqual((c1.phone_number, c1.phone_number_2, c1.specialization), ('+998901234567', '+998907654321', 'Русский язык'))
        c2 = Contract.objects.get(number='2')
        self.assertEqual((c2.discount_percentage, c2.total_amount, c2.discount_reason), (100, D(616000), 'SOCIAL'))
        c3 = Contract.objects.get(number='3')
        self.assertEqual((c3.discount_percentage, c3.total_amount, c3.discount_reason), (10, D(2358400), 'SIBLING_2'))
        c4 = Contract.objects.get(number='4')   # в книге 100 %, но «Скидка, сум» и «Сумма» говорят о 50 %
        self.assertEqual((c4.discount_percentage, c4.discount_fixed, c4.total_amount), (0, D(968000), D(1584000)))
        c5 = Contract.objects.get(number='5')
        self.assertTrue(c5.is_cancelled)
        self.assertIsNone(c5.cancelled_at)
        c6 = Contract.objects.get(number='6')
        self.assertEqual(c6.student_class, '2013')
        self.assertIn('Класс', c6.data_problem)

        bank = Account.objects.get(school=self.school, kind='BANK')
        cash = Account.objects.get(school=self.school, kind='CASH')
        sept = Payment.objects.get(contract=c1, for_month=date(2026, 9, 1), data_problem='')
        self.assertEqual((sept.payment_date, sept.method, sept.account), (date(2026, 7, 8), 'BANK', bank))
        self.assertEqual(Payment.objects.filter(contract=c1, for_month=date(2026, 10, 1)).count(), 2)   # пустая статья + номер = оплата
        self.assertEqual(UnidentifiedReceipt.objects.get().amount, D(500000))
        founder = Counterparty.objects.get(name='ООО Тест Инвест')
        self.assertEqual(founder.kind, 'FOUNDER')
        self.assertEqual(Transaction.objects.get(counterparty=founder, type='INCOME').category.name, 'Вложения учредителей')
        self.assertEqual(Transaction.objects.get(counterparty=founder, type='EXPENSE').category.name, 'Выплаты учредителям')
        self.assertEqual(Transaction.objects.get(category__name='Зарплата').account, cash)
        self.assertEqual(Transaction.objects.get(category__name='Возвраты родителям').amount, D(700000))
        self.assertEqual(Transaction.objects.get(category__name='Услуги банка').amount, D('12345.67'))
        transfers = {(t.from_account.kind, t.to_account.kind, t.amount) for t in Transfer.objects.all()}
        self.assertEqual(transfers, {('BANK', 'CASH', D(30000000)), ('CASH', 'BANK', D(4000000))})
        self.assertFalse(Transaction.objects.filter(amount=D(30000000)).exists())   # снятие наличных не стало доходом

    def test_second_upload_adds_nothing(self):
        self.confirm(self.upload(self.owner, book(CONTRACTS, INCOME, EXPENSE)))
        before = (Contract.objects.count(), Payment.objects.count(), Transaction.objects.count(), Transfer.objects.count(),
                  UnidentifiedReceipt.objects.count())
        preview = self.upload(self.owner, book(CONTRACTS, INCOME, EXPENSE))
        counts = preview.context['plan'].counts()
        self.assertEqual((counts['contracts']['ok'], counts['income']['ok'], counts['expense']['ok']), (0, 0, 0))
        self.confirm(preview)
        after = (Contract.objects.count(), Payment.objects.count(), Transaction.objects.count(), Transfer.objects.count(),
                 UnidentifiedReceipt.objects.count())
        self.assertEqual(before, after)

    def test_fixed_rows_are_added_on_reupload(self):
        self.confirm(self.upload(self.owner, book(CONTRACTS, INCOME[:1], [])))
        fixed = [dict(r) for r in CONTRACTS]
        fixed[5]['Класс'] = 3
        response = self.confirm(self.upload(self.owner, book(fixed, INCOME[:1], [])))
        self.assertEqual(response.context['created']['contracts'], 0)
        self.assertEqual(response.context['created']['fixed'], 1)
        c6 = Contract.objects.get(number='6')
        self.assertEqual((c6.student_class, c6.data_problem), ('3', ''))

    def test_numbers_taken_by_other_school_need_prefix(self):
        Contract.objects.create(number='1', school=self.other, date=date(2026, 9, 1), tuition_fee=D(1))
        preview = self.upload(self.owner, book(CONTRACTS[:1], INCOME[:1], []))
        self.assertIn('префикс', ' '.join(preview.context['plan'].problems()[0].errors))
        preview = self.upload(self.owner, book(CONTRACTS[:1], INCOME[:1], []), prefix='ZAM-')
        self.confirm(preview)
        self.assertEqual(Contract.objects.get(number='ZAM-1').school, self.school)
        self.assertEqual(Payment.objects.get().contract.number, 'ZAM-1')

    def test_director_imports_into_own_school(self):
        self.client.force_login(self.director)
        self.assertNotIn('school', self.client.get(reverse('workbook_import')).context['form'].fields)
        self.confirm(self.upload(self.director, book(CONTRACTS[:1], [], [])))
        self.assertEqual(Contract.objects.get().school, self.school)

    def test_access_and_bad_files(self):
        for user in (self.employee, self.investor):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('workbook_import')).status_code, 403)
        wb = openpyxl.Workbook()
        wb.active.title = 'Договоры'
        buffer = io.BytesIO()
        wb.save(buffer)
        response = self.upload(self.owner, SimpleUploadedFile('x.xlsx', buffer.getvalue()))
        self.assertIn('Поступление ДС', response.context['form'].errors['file'][0])
        response = self.upload(self.owner, SimpleUploadedFile('x.csv', b'a;b'))
        self.assertIn('.xlsx', response.context['form'].errors['file'][0])

    def test_stale_token(self):
        preview = self.upload(self.owner, book(CONTRACTS[:1], [], []))
        self.client.post(reverse('workbook_import'), {'action': 'cancel'})
        response = self.client.post(reverse('workbook_import'), {'action': 'confirm', 'token': preview.context['token']}, follow=True)
        self.assertContains(response, 'устарела')
        self.assertFalse(Contract.objects.exists())

    def test_contract_list_has_link(self):
        self.client.force_login(self.director)
        self.assertContains(self.client.get(reverse('contract_list')), reverse('workbook_import'))
        self.client.force_login(self.employee)
        self.assertNotContains(self.client.get(reverse('contract_list')), reverse('workbook_import'))


class HelperTests(TestCase):
    def test_discount_parts(self):
        self.assertEqual(discount_parts(D(1936000), 0.1, D(2358400), D(616000)), (10, D(0)))
        self.assertEqual(discount_parts(D(1936000), 1, D(616000), D(616000)), (100, D(0)))
        self.assertEqual(discount_parts(D(1936000), None, D(2552000), D(616000)), (0, D(0)))
        self.assertEqual(discount_parts(D(1936000), 1, D(1584000), D(616000)), (0, D(968000)))
        self.assertEqual(discount_parts(D(1936000), 0.1, D(2258400), D(616000)), (10, D(100000)))   # процент + сумма
        self.assertEqual(discount_parts(D(1936000), 15, None, D(616000)), (15, D(0)))

    def test_book_phones(self):
        self.assertEqual(book_phones('901234567\n907654321'), ('+998901234567', '+998907654321', []))
        self.assertEqual(book_phones('901234567.765432'), ('+998901234567', '', ['765432']))
        self.assertEqual(book_phones('901234567,  90765432'), ('+998901234567', '', ['90765432']))
        self.assertEqual(book_phones('9012345678'), ('9012345678', '', ['9012345678']))
