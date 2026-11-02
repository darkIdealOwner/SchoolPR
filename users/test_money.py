"""Деньги: счета и остатки, статьи, учредители, переводы между счетами."""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from integrations.importers import run_import
from users.finance import account_balances
from users.models import Account, Category, Contract, Counterparty, EmployeePayout, Payment, School, Transaction, Transfer, User
from users.templatetags.money import money
from users.test_excel_import import xlsx

PASSWORD = 'Strong-pass-2026'


class MoneyBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name='Школа М')
        cls.other = School.objects.create(name='Школа Н')
        cls.owner = User.objects.create_user(username='m_owner', phone_number='+998930000401', password=PASSWORD, role='OWNER')
        cls.director = User.objects.create_user(username='m_dir', phone_number='+998930000402', password=PASSWORD,
                                                role='DIRECTOR', school=cls.school, first_name='Директор')
        cls.employee = User.objects.create_user(username='m_emp', phone_number='+998930000403', password=PASSWORD,
                                                role='EMPLOYEE', school=cls.school, first_name='Учитель')
        cls.investor = User.objects.create_user(username='m_inv', phone_number='+998930000404', password=PASSWORD, role='INVESTOR')
        cls.contract = Contract.objects.create(number='MN-1', school=cls.school, date=date(2026, 9, 1), duration_months=10,
                                               student_full_name='Ученик', tuition_fee=Decimal(1000))
        cls.cash = Account.objects.get(school=cls.school, kind='CASH')
        cls.bank = Account.objects.get(school=cls.school, kind='BANK')
        cls.card = Account.objects.get(school=cls.school, kind='CARD')
        cls.rent = Category.objects.get(name='Аренда')
        cls.founders_in = Category.objects.get(name='Вложения учредителей')


class AccountsAndCategoriesTests(MoneyBase):
    def test_new_school_gets_cash_bank_terminal(self):
        kinds = sorted(Account.objects.filter(school=self.other).values_list('kind', flat=True))
        self.assertEqual(kinds, ['BANK', 'CARD', 'CASH'])
        with translation.override('ru'):
            self.assertEqual(self.cash.display_name, 'Касса')
        with translation.override('en'):
            self.assertEqual(self.cash.display_name, 'Cash desk')

    def test_payment_goes_to_account_by_method(self):
        for method, account in (('CASH', self.cash), ('CLICK', self.bank), ('BANK', self.bank), ('CARD', self.card)):
            payment = Payment.objects.create(contract=self.contract, for_month=date(2026, 9, 1), amount=Decimal(1), method=method)
            self.assertEqual(payment.account, account, method)

    def test_transaction_type_comes_from_category(self):
        tr = Transaction.objects.create(school=self.school, type='INCOME', category=self.rent, amount=Decimal(5))
        self.assertEqual((tr.type, tr.account), ('EXPENSE', self.cash))
        plain = Transaction.objects.create(school=self.school, type='INCOME', amount=Decimal(5))
        self.assertEqual(plain.category.name, 'Прочие поступления')

    def test_category_found_in_any_language(self):
        for text in ('Аренда', 'аренда ', 'Ijara', 'Rent', '임대료'):
            self.assertEqual(Category.find(text), self.rent, text)
        self.assertIsNone(Category.find('Аренда', 'INCOME'))
        self.assertIsNone(Category.find('Космос'))

    def test_seeded_sections(self):
        self.assertFalse(self.founders_in.in_pl)
        self.assertEqual(self.founders_in.section, 'FINANCING')
        self.assertTrue(Category.objects.get(name='Зарплата').is_salary)
        self.assertEqual(Category.objects.get(name='Основные средства').section, 'INVESTING')


class BalanceTests(MoneyBase):
    def test_balance_formula(self):
        self.cash.opening_balance, self.cash.opening_date = Decimal(1000), date(2026, 9, 1)
        self.cash.save()
        Payment.objects.create(contract=self.contract, for_month=date(2026, 9, 1), amount=Decimal(500),
                               payment_date=date(2026, 9, 5), method='CASH')
        Payment.objects.create(contract=self.contract, for_month=date(2026, 9, 1), amount=Decimal(999),
                               payment_date=date(2026, 8, 5), method='CASH')   # раньше остатка на начало
        reversed_payment = Payment.objects.create(contract=self.contract, for_month=date(2026, 9, 1), amount=Decimal(777),
                                                  payment_date=date(2026, 9, 6), method='CASH')
        reversed_payment.reverse(None, 'ошибка')
        EmployeePayout.objects.create(employee=self.employee.staff_profile, school=self.school, for_month=date(2026, 9, 1),
                                      amount=Decimal(200), payout_date=date(2026, 9, 10))
        Transaction.objects.create(school=self.school, category=self.rent, amount=Decimal(100), date=date(2026, 9, 11))
        Transaction.objects.create(school=self.school, category=self.founders_in, amount=Decimal(50), date=date(2026, 9, 12))
        Transfer.objects.create(school=self.school, from_account=self.cash, to_account=self.bank, amount=Decimal(300),
                                date=date(2026, 9, 13))
        balances = account_balances([self.cash, self.bank], until=date(2026, 9, 30))
        self.assertEqual(balances[self.cash.pk], Decimal(1000 + 500 - 200 - 100 + 50 - 300))
        self.assertEqual(balances[self.bank.pk], Decimal(300))
        early = account_balances([self.cash], until=date(2026, 9, 7))
        self.assertEqual(early[self.cash.pk], Decimal(1500))


class TransferTests(MoneyBase):
    def test_create_and_reverse(self):
        self.client.force_login(self.director)
        response = self.client.post(reverse('transfer_create'), {
            'from_account': self.cash.pk, 'to_account': self.bank.pk, 'amount': '400', 'date': '2026-10-01', 'notes': ''})
        self.assertRedirects(response, reverse('transfer_list'))
        transfer = Transfer.objects.get()
        self.assertEqual((transfer.school, transfer.created_by), (self.school, self.director))
        self.client.post(reverse('transfer_reverse', args=[transfer.pk]), {'reason': 'ошибка'})
        self.assertEqual(account_balances([self.bank], until=date(2026, 10, 31))[self.bank.pk], 0)

    def test_validation(self):
        self.client.force_login(self.owner)
        url = reverse('transfer_create')
        same = self.client.post(url, {'school': self.school.pk, 'from_account': self.cash.pk, 'to_account': self.cash.pk,
                                      'amount': '1', 'date': '2026-10-01'})
        self.assertIn('to_account', same.context['form'].errors)
        foreign = Account.objects.get(school=self.other, kind='CASH')
        cross = self.client.post(url, {'school': self.school.pk, 'from_account': self.cash.pk, 'to_account': foreign.pk,
                                       'amount': '1', 'date': '2026-10-01'})
        self.assertIn('to_account', cross.context['form'].errors)
        zero = self.client.post(url, {'school': self.school.pk, 'from_account': self.cash.pk, 'to_account': self.bank.pk,
                                      'amount': '0', 'date': '2026-10-01'})
        self.assertIn('amount', zero.context['form'].errors)
        self.assertFalse(Transfer.objects.exists())

    def test_director_cannot_use_other_school_accounts(self):
        self.client.force_login(self.director)
        foreign = Account.objects.get(school=self.other, kind='CASH')
        response = self.client.post(reverse('transfer_create'), {
            'from_account': foreign.pk, 'to_account': self.bank.pk, 'amount': '1', 'date': '2026-10-01'})
        self.assertIn('from_account', response.context['form'].errors)


class PagesAccessTests(MoneyBase):
    def test_access_by_role(self):
        pages = {
            'account_list': {'OWNER': 200, 'DIRECTOR': 200, 'EMPLOYEE': 403, 'INVESTOR': 403},
            'transfer_list': {'OWNER': 200, 'DIRECTOR': 200, 'EMPLOYEE': 403, 'INVESTOR': 403},
            'category_list': {'OWNER': 200, 'DIRECTOR': 403, 'EMPLOYEE': 403, 'INVESTOR': 403},
            'counterparty_list': {'OWNER': 200, 'DIRECTOR': 403, 'EMPLOYEE': 403, 'INVESTOR': 403},
        }
        users = {'OWNER': self.owner, 'DIRECTOR': self.director, 'EMPLOYEE': self.employee, 'INVESTOR': self.investor}
        for name, codes in pages.items():
            for role, code in codes.items():
                self.client.force_login(users[role])
                self.assertEqual(self.client.get(reverse(name)).status_code, code, f'{name} {role}')

    def test_director_sees_and_edits_only_own_accounts(self):
        self.client.force_login(self.director)
        accounts = self.client.get(reverse('account_list')).context['accounts']
        self.assertEqual({a.school_id for a in accounts}, {self.school.pk})
        foreign = Account.objects.get(school=self.other, kind='CASH')
        self.assertEqual(self.client.get(reverse('account_edit', args=[foreign.pk])).status_code, 404)
        self.client.post(reverse('account_edit', args=[self.cash.pk]), {
            'kind': 'CASH', 'name': 'Касса №1', 'opening_balance': '250000', 'opening_date': '2026-09-01', 'is_active': 'on'})
        self.cash.refresh_from_db()
        self.assertEqual((self.cash.name, self.cash.opening_balance), ('Касса №1', Decimal(250000)))
        self.assertContains(self.client.get(reverse('account_list')), money(Decimal(250000)))

    def test_owner_manages_categories_and_counterparties(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('category_add'), {'name': 'Обучение учителей', 'name_en': 'Teacher training',
                                                   'direction': 'EXPENSE', 'section': 'OPERATING', 'in_pl': 'on',
                                                   'sort_order': 50, 'is_active': 'on'})
        self.assertTrue(Category.objects.filter(name='Обучение учителей', in_pl=True).exists())
        self.client.post(reverse('counterparty_add'), {'name': 'ООО Учредитель', 'kind': 'FOUNDER', 'investor': self.investor.pk})
        self.assertEqual(Counterparty.objects.get().investor, self.investor)


class TransactionFormTests(MoneyBase):
    def test_owner_creates_with_category_account_counterparty(self):
        founder = Counterparty.objects.create(name='Учредитель 1', kind='FOUNDER')
        self.client.force_login(self.owner)
        self.client.post(reverse('transaction_create'), {
            'school': self.school.pk, 'category': self.founders_in.pk, 'account': self.bank.pk, 'counterparty': founder.pk,
            'amount': '1000000', 'date': '2026-10-01', 'notes': 'Вклад'})
        tr = Transaction.objects.get()
        self.assertEqual((tr.type, tr.account, tr.counterparty), ('INCOME', self.bank, founder))
        body = self.client.get(reverse('transaction_list')).content.decode()
        self.assertIn('Вложения учредителей', body)
        self.assertIn('Учредитель 1', body)

    def test_validation(self):
        self.client.force_login(self.owner)
        url = reverse('transaction_create')
        foreign = Account.objects.get(school=self.other, kind='CASH')
        cross = self.client.post(url, {'school': self.school.pk, 'category': self.rent.pk, 'account': foreign.pk,
                                       'amount': '1', 'date': '2026-10-01'})
        self.assertIn('account', cross.context['form'].errors)
        no_category = self.client.post(url, {'school': self.school.pk, 'amount': '1', 'date': '2026-10-01'})
        self.assertIn('category', no_category.context['form'].errors)
        zero = self.client.post(url, {'school': self.school.pk, 'category': self.rent.pk, 'amount': '0', 'date': '2026-10-01'})
        self.assertIn('amount', zero.context['form'].errors)
        self.assertFalse(Transaction.objects.exists())

    def test_export_columns(self):
        Transaction.objects.create(school=self.school, category=self.rent, amount=Decimal(5), account=self.bank)
        self.client.force_login(self.owner)
        header, row = self.client.get(reverse('transaction_list') + '?export=csv').content.decode('utf-8-sig').splitlines()[:2]
        for name in ('Статья', 'Счёт', 'Контрагент'):
            self.assertIn(name, header)
        self.assertIn('Аренда', row)
        self.assertIn('Расчётный счёт', row)


class ImportTests(MoneyBase):
    HEADERS = ['Дата', 'Краткое назначение', 'Вид расходы', 'Контрагент', 'Сумма', 'Назначение платежа']

    def test_workbook_style_rows(self):
        Counterparty.objects.create(name='Учредитель 2', kind='FOUNDER')
        self.client.force_login(self.director)
        rows = [
            {'Дата': '04.08.2026', 'Краткое назначение': 'Ijara', 'Вид расходы': 'Перечисление', 'Сумма': 1000000, 'Назначение платежа': 'аренда за август'},
            {'Дата': '05.08.2026', 'Краткое назначение': 'Выплаты учредителям', 'Вид расходы': 'Наличный расчет', 'Контрагент': 'Учредитель 2', 'Сумма': 500},
            {'Дата': '05.08.2026', 'Краткое назначение': 'Космос', 'Сумма': 1},
            {'Дата': '05.08.2026', 'Сумма': 1},
            {'Дата': '05.08.2026', 'Краткое назначение': 'Аренда', 'Вид расходы': 'Сейф', 'Сумма': 1},
        ]
        response = self.client.post(reverse('excel_import', args=['transactions']), {'file': xlsx(self.HEADERS, rows)})
        results = response.context['results']
        self.assertEqual([r.status for r in results], ['ok', 'ok', 'error', 'error', 'error'], [r.errors for r in results])
        self.assertIn('Космос', results[2].errors[0])
        self.client.post(reverse('excel_import', args=['transactions']), {'action': 'confirm', 'token': response.context['token']})
        rent = Transaction.objects.get(category=self.rent)
        self.assertEqual((rent.account, rent.notes), (self.bank, 'аренда за август'))
        payout = Transaction.objects.get(category__name='Выплаты учредителям')
        self.assertEqual((payout.account, payout.counterparty.name, payout.type), (self.cash, 'Учредитель 2', 'EXPENSE'))

    def test_onec_unknown_category_goes_to_other_with_note(self):
        self.school.external_id = 'SCH-M'
        self.school.save()
        run = run_import('transactions', [{'external_id': 'T-1', 'school_external_id': 'SCH-M', 'type': 'EXPENSE',
                                           'category': 'Канцтовары', 'date': '2026-10-01', 'amount': 10, 'notes': 'бумага'}])
        self.assertEqual(run.created, 1, run.errors)
        tr = Transaction.objects.get()
        self.assertEqual((tr.category.name, tr.notes), ('Прочие расходы', '[Канцтовары] бумага'))
