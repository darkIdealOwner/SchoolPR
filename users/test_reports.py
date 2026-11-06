"""Отчёты: прибыль и убытки, движение денег, контроль оплаты, расчёты с учредителями — на заранее посчитанном примере."""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from users import reports
from users.models import (
    AcademicYear, Account, Category, Contract, Counterparty, EmployeePayout, Payment, School, Staff, Transaction, Transfer,
    UnidentifiedReceipt, User,
)

PASSWORD = 'Strong-pass-2026'
D = Decimal


class ReportBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.get(name='2026–2027')
        cls.school = School.objects.create(name='Школа Отчёт')
        cls.other = School.objects.create(name='Школа Другая')
        make = lambda username, phone, role, school=None: User.objects.create_user(
            username=username, phone_number=phone, password=PASSWORD, role=role, school=school, first_name=username)
        cls.owner = make('rp_owner', '+998930000701', 'OWNER')
        cls.director = make('rp_dir', '+998930000702', 'DIRECTOR', cls.school)
        cls.employee = make('rp_emp', '+998930000703', 'EMPLOYEE', cls.school)
        cls.investor = make('rp_inv', '+998930000704', 'INVESTOR')
        cls.cash = Account.objects.get(school=cls.school, kind='CASH')
        cls.bank = Account.objects.get(school=cls.school, kind='BANK')

        cls.c1 = Contract.objects.create(number='10', school=cls.school, date=date(2026, 6, 1), duration_months=10,
                                         student_full_name='Ученик Десятый', tuition_fee=D(1000))
        cls.c2 = Contract.objects.create(number='9', school=cls.school, date=date(2026, 6, 1), duration_months=10,
                                         student_full_name='Ученик Девятый', tuition_fee=D(1000), is_cancelled=True)
        pay = lambda month, amount, day, method='CASH', kind='PAYMENT': Payment.objects.create(
            contract=cls.c1, for_month=month, amount=D(amount), payment_date=day, method=method, kind=kind)
        pay(date(2026, 9, 1), 1000, date(2026, 8, 20))                       # предоплата в августе
        pay(date(2026, 10, 1), 500, date(2026, 10, 5), method='BANK')
        pay(date(2026, 10, 1), 100, date(2026, 10, 20), kind='REFUND')       # возврат
        cat = lambda name: Category.objects.get(name=name)
        cls.founder = Counterparty.objects.create(name='Учредитель Один', kind='FOUNDER', investor=cls.investor)
        cls.founder2 = Counterparty.objects.create(name='Учредитель Два', kind='FOUNDER')
        tr = lambda category, amount, day, **kw: Transaction.objects.create(school=cls.school, category=cat(category),
                                                                          amount=D(amount), date=day, **kw)
        tr('Аренда', 300, date(2026, 10, 10))
        tr('Вложения учредителей', 5000, date(2026, 9, 2), account=cls.bank, counterparty=cls.founder)
        tr('Выплаты учредителям', 1000, date(2026, 10, 12), account=cls.bank, counterparty=cls.founder)
        tr('Вложения учредителей', 700, date(2026, 9, 3), counterparty=cls.founder2)
        tr('Основные средства', 2000, date(2026, 10, 11))
        tr('Зарплата', 400, date(2026, 10, 25))
        staff = Staff.objects.create(school=cls.school, full_name='Повар')
        EmployeePayout.objects.create(employee=staff, school=cls.school, for_month=date(2026, 10, 1), amount=D(600),
                                      payout_date=date(2026, 10, 30))
        UnidentifiedReceipt.objects.create(school=cls.school, account=cls.bank, amount=D(70), date=date(2026, 10, 15))
        Transfer.objects.create(school=cls.school, from_account=cls.cash, to_account=cls.bank, amount=D(200), date=date(2026, 10, 16))
        # Другая школа — не должна попадать в отчёт по первой
        Transaction.objects.create(school=cls.other, category=cat('Аренда'), amount=D(99999), date=date(2026, 10, 10))

    def row(self, data, label):
        return next(r for r in data['rows'] if r.label == label)


class ProfitLossTests(ReportBase):
    def test_numbers(self):
        months = reports.financial_months(self.year)
        self.assertEqual((months[0], months[-1], len(months)), (date(2026, 7, 1), date(2027, 6, 1), 12))
        data = reports.profit_and_loss(months, self.school)
        tuition = self.row(data, 'Оплата за обучение')
        self.assertEqual((tuition.by_month[date(2026, 8, 1)], tuition.by_month[date(2026, 10, 1)], tuition.total),
                         (D(1000), D(400), D(1400)))
        self.assertEqual(self.row(data, 'Зарплата').total, D(1000))         # выплаты 600 + одной суммой 400
        self.assertEqual(self.row(data, 'Аренда').total, D(300))
        labels = [r.label for r in data['rows']]
        for excluded in ('Вложения учредителей', 'Основные средства', 'Выплаты учредителям'):
            self.assertNotIn(excluded, labels)
        self.assertEqual((data['income'], data['expense'], data['profit']), (D(1400), D(1300), D(100)))
        self.assertAlmostEqual(float(data['margin']), 100 / 14, places=3)
        self.assertEqual(self.row(data, 'Начислено за обучение (справочно)').total, D(10000))   # 10 месяцев × 1000

    def test_network_includes_other_school(self):
        data = reports.profit_and_loss(reports.financial_months(self.year))
        self.assertEqual(self.row(data, 'Аренда').total, D(300 + 99999))


class CashFlowTests(ReportBase):
    def test_sections_and_balances(self):
        data = reports.cash_flow(reports.financial_months(self.year), self.school)
        nets = {r.label: r.total for r in data['rows'] if r.style == 'subtotal'}
        self.assertEqual(nets['Итого: операционная деятельность'], D(1000 + 500 + 70 - 100 - 600 - 300 - 400))
        self.assertEqual(nets['Итого: инвестиционная деятельность'], D(-2000))
        self.assertEqual(nets['Итого: финансовая деятельность'], D(5000 + 700 - 1000))
        total = next(r for r in data['rows'] if r.style == 'total')
        self.assertEqual(total.total, D(2870))
        self.assertEqual((data['opening'], data['closing']), (D(0), D(2870)))   # перевод не меняет общий остаток
        bank = next(a for a in data['accounts'] if a['account'] == self.bank)
        self.assertEqual(bank['closing'], D(500 + 5000 - 1000 + 70 + 200))
        balance = next(r for r in data['rows'] if r.label == 'Остаток денег на конец месяца')
        self.assertEqual((balance.by_month[date(2026, 8, 1)], balance.by_month[date(2027, 6, 1)]), (D(1000), D(2870)))


class PaymentControlTests(ReportBase):
    def test_matrix(self):
        data = reports.payment_control(self.year, self.school, today=date(2026, 11, 2))
        self.assertEqual([r['contract'].number for r in data['rows']], ['9', '10'])   # по номеру как по числу
        row = data['rows'][1]
        sep, oct_, nov = row['cells'][0], row['cells'][1], row['cells'][2]
        self.assertEqual((sep['accrued'], sep['paid'], sep['balance']), (D(1000), D(1000), D(0)))
        self.assertEqual((oct_['accrued'], oct_['paid'], oct_['balance']), (D(1000), D(400), D(-600)))
        self.assertEqual(nov['balance'], D(-1600))
        self.assertEqual((row['accrued'], row['paid'], row['debt_now']), (D(10000), D(1400), D(1600)))
        cancelled = data['rows'][0]
        self.assertEqual((cancelled['accrued'], cancelled['debt_now']), (D(0), D(0)))   # отменён без оплат — без начислений
        self.assertEqual(data['total']['debt_now'], D(1600))

    def test_status_filter(self):
        only_cancelled = reports.payment_control(self.year, self.school, 'cancelled')
        self.assertEqual([r['contract'].number for r in only_cancelled['rows']], ['9'])


class FounderTests(ReportBase):
    def test_settlements(self):
        data = reports.founder_settlements()
        by_name = {r['counterparty'].name: r for r in data['rows']}
        self.assertEqual((by_name['Учредитель Один']['invested'], by_name['Учредитель Один']['paid'],
                          by_name['Учредитель Один']['balance']), (D(5000), D(1000), D(4000)))
        self.assertEqual(data['totals']['balance'], D(4700))
        own = reports.founder_settlements(investor=self.investor)
        self.assertEqual([r['counterparty'].name for r in own['rows']], ['Учредитель Один'])


class ReportPagesTests(ReportBase):
    def test_access(self):
        money = {'OWNER': 200, 'DIRECTOR': 200, 'EMPLOYEE': 403, 'INVESTOR': 200}
        everyone = {'OWNER': 200, 'DIRECTOR': 200, 'EMPLOYEE': 200, 'INVESTOR': 200}
        expected = {
            'report_dashboard': money, 'report_summary': money, 'report_cash_methods': money, 'report_pivots': money,
            'report_payment_control': everyone, 'report_debts': everyone, 'report_tariffs': everyone,
            'report_founders': {'OWNER': 200, 'DIRECTOR': 403, 'EMPLOYEE': 403, 'INVESTOR': 200},
        }
        users = {'OWNER': self.owner, 'DIRECTOR': self.director, 'EMPLOYEE': self.employee, 'INVESTOR': self.investor}
        for name, codes in expected.items():
            for role, code in codes.items():
                self.client.force_login(users[role])
                self.assertEqual(self.client.get(reverse(name)).status_code, code, f'{name} {role}')
        self.client.force_login(self.employee)
        self.assertRedirects(self.client.get(reverse('reports')), reverse('report_payment_control'))
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get(reverse('reports')), reverse('report_dashboard'))

    def test_old_addresses_lead_to_dashboard(self):
        self.client.force_login(self.owner)
        for name in ('report_pl', 'report_cashflow'):
            response = self.client.get(reverse(name) + f'?year={self.year.pk}')
            self.assertRedirects(response, reverse('report_dashboard') + f'?year={self.year.pk}')

    def test_director_cannot_switch_school(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse('report_dashboard') + f'?school={self.other.pk}')
        self.assertEqual(response.context['school'], self.school)
        self.assertEqual(response.context['data']['kpi']['operating'], D(1300))

    def test_owner_school_filter_and_pages_render(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('report_dashboard') + f'?school={self.other.pk}&year={self.year.pk}')
        self.assertEqual(response.context['data']['kpi']['operating'], D(99999))
        self.assertContains(self.client.get(reverse('report_dashboard')), 'Банк и касса')
        body = self.client.get(reverse('report_payment_control')).content.decode()
        self.assertIn('Ученик Десятый', body)
        self.assertContains(self.client.get(reverse('report_founders')), 'Учредитель Два')

    def test_nav_has_all_tabs_and_keeps_filters(self):
        self.client.force_login(self.owner)
        body = self.client.get(reverse('report_summary') + f'?year={self.year.pk}&school={self.school.pk}&month=2026-10').content.decode()
        for name in ('report_dashboard', 'report_summary', 'report_cash_methods', 'report_payment_control', 'report_debts',
                     'report_tariffs', 'report_pivots', 'report_founders'):
            self.assertIn(f'{reverse(name)}?year={self.year.pk}&amp;school={self.school.pk}"', body, name)
        self.client.force_login(self.employee)
        body = self.client.get(reverse('report_debts')).content.decode()
        self.assertNotIn(reverse('report_dashboard'), body)
        self.assertIn(reverse('report_tariffs'), body)

    def test_exports(self):
        self.client.force_login(self.owner)
        for name in ('report_dashboard', 'report_summary', 'report_cash_methods', 'report_debts', 'report_tariffs',
                     'report_pivots', 'report_payment_control', 'report_founders'):
            response = self.client.get(reverse(name) + f'?year={self.year.pk}&export=csv')
            self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8', name)
        dash = self.client.get(reverse('report_dashboard') + f'?school={self.school.pk}&export=csv').content.decode('utf-8-sig')
        self.assertIn('Чистая прибыль;100', dash)
        control = self.client.get(reverse('report_payment_control') + '?export=csv').content.decode('utf-8-sig').splitlines()
        self.assertIn('начислено', control[0])
        self.assertTrue(any(line.startswith('10;') for line in control))
        founders = self.client.get(reverse('report_founders') + '?export=csv').content.decode('utf-8-sig')
        self.assertIn('Учредитель Один', founders)

    def test_menu_has_reports(self):
        for user in (self.owner, self.employee, self.investor):
            self.client.force_login(user)
            self.assertContains(self.client.get(reverse('home')), reverse('reports'))
