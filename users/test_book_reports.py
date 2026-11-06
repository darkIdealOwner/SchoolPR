"""Отчёты в раскладке листов книги школы — на заранее посчитанном примере из test_reports.ReportBase.

Пример (школа «Школа Отчёт», финансовый год июль 2026 – июнь 2027):
касса — оплата 1000 (авг), вложение «Учредитель Два» 700, возврат 100, аренда 300, основные средства 2000,
зарплата 400 одной суммой + выплата 600, перевод 200 в банк;
банк — оплата 500 (окт), вложение «Учредитель Один» 5000, выплата ему 1000, нераспознанное 70, перевод 200 из кассы.
"""
from datetime import date
from decimal import Decimal

from django.urls import reverse

from users import reports
from users.models import Category, Contract, Transaction
from users.test_reports import ReportBase

D = Decimal


class MovementTests(ReportBase):
    def moves(self):
        months = reports.financial_months(self.year)
        return reports.movements(months[0], reports.shift_month(months[-1], 1), self.school)

    def test_totals_by_direction(self):
        moves = self.moves()
        self.assertEqual(sum(m.amount for m in moves if m.direction == 'in'), D(7470))
        self.assertEqual(sum(m.amount for m in moves if m.direction == 'out'), D(4600))

    def test_transfer_is_two_moves(self):
        transfer = [m for m in self.moves() if m.transfer]
        self.assertEqual(sorted((m.kind, m.direction) for m in transfer), [('BANK', 'in'), ('CASH', 'out')])

    def test_founder_moves_are_labelled_by_name(self):
        labels = {m.label for m in self.moves()}
        self.assertIn('Учредитель Один', labels)
        self.assertNotIn('Вложения учредителей', labels)

    def test_other_school_not_included(self):
        self.assertNotIn(D(99999), [m.amount for m in self.moves()])

    def test_whole_network(self):
        months = reports.financial_months(self.year)
        moves = reports.movements(months[0], reports.shift_month(months[-1], 1))
        self.assertIn(D(99999), [m.amount for m in moves])

    def test_reversed_records_are_ignored(self):
        Transaction.objects.filter(category__name='Аренда', school=self.school).update(is_reversed=True)
        self.assertEqual(sum(m.amount for m in self.moves() if m.direction == 'out'), D(4300))


class DashboardTests(ReportBase):
    def data(self, school='own'):
        return reports.dashboard(self.year, self.school if school == 'own' else school)

    def test_kpis(self):
        k = self.data()['kpi']
        self.assertEqual((k['income'], k['operating'], k['operating_profit'], k['tax'], k['net']),
                         (D(1400), D(1300), D(100), D(0), D(100)))
        self.assertAlmostEqual(float(k['margin']), 100 / 14, places=3)

    def test_cash_flow_kpis_add_up(self):
        k = self.data()['kpi']
        self.assertEqual(k['operating_cf'] + k['investing_cf'] + k['financing_cf'], k['net_cf'])
        self.assertEqual(k['investing_cf'], D(-2000))
        self.assertEqual(k['financing_cf'], D(4700))     # 5000 + 700 − 1000

    def test_tax_interest_other_are_separate(self):
        cat = lambda name: Category.objects.get(name=name)
        for name, amount in (('Налоги', 50), ('Проценты по кредиту', 20), ('Прочие расходы', 10)):
            Transaction.objects.create(school=self.school, category=cat(name), amount=D(amount), date=date(2026, 10, 3))
        k = self.data()['kpi']
        self.assertEqual((k['operating'], k['tax'], k['net']), (D(1300), D(50), D(20)))
        pl = {r['label']: r['amount'] for r in self.data()['pl']}
        self.assertEqual((pl['Проценты по кредиту'], pl['Прочие расходы'], pl['EBITDA']), (D(20), D(10), D(100)))

    def test_pl_percent_of_income(self):
        pl = {r['label']: r for r in self.data()['pl']}
        self.assertEqual(pl['Доход']['pct'], 100)
        self.assertAlmostEqual(float(pl['Операционные расходы']['pct']), 1300 / 14, places=3)

    def test_monthly_table(self):
        data = self.data()
        by_month = {m['month']: m for m in data['monthly']}
        self.assertEqual(len(data['monthly']), 12)
        self.assertEqual((by_month[date(2026, 8, 1)]['income'], by_month[date(2026, 8, 1)]['inflow']), (D(1000), D(1000)))
        october = by_month[date(2026, 10, 1)]
        self.assertEqual((october['income'], october['expense'], october['profit']), (D(400), D(1300), D(-900)))
        self.assertEqual(october['inflow'], D(570))     # 500 + 70, без перевода
        self.assertEqual(data['monthly_total']['inflow'], D(7270))

    def test_sources_and_articles(self):
        data = self.data()
        sources = {r['label']: r['amount'] for r in data['sources']}
        self.assertEqual(sources, {'Оплата за обучение': D(1500), 'Нераспознанные поступления': D(70),
                                   'Учредитель Один': D(5000), 'Учредитель Два': D(700)})
        self.assertEqual(data['sources_total'], D(7270))
        articles = {r['label']: r['amount'] for r in data['articles']}
        self.assertEqual(articles, {'Возвраты родителям': D(100), 'Зарплата': D(1000), 'Аренда': D(300),
                                    'Основные средства': D(2000), 'Учредитель Один': D(1000)})
        self.assertEqual(data['articles_total'], D(4400))

    def test_tuition_goes_first(self):
        self.assertEqual(self.data()['sources'][0]['label'], 'Оплата за обучение')

    def test_bank_and_cash(self):
        rows = {r['label']: r for r in self.data()['bank_cash']['rows']}
        self.assertEqual((rows['Расчётный счёт']['in'], rows['Расчётный счёт']['out'], rows['Расчётный счёт']['closing']),
                         (D(5770), D(1000), D(4770)))
        self.assertEqual((rows['Касса']['in'], rows['Касса']['out'], rows['Касса']['closing']), (D(1700), D(3600), D(-1900)))
        self.assertNotIn('Другое', rows)
        self.assertEqual(self.data()['bank_cash']['total']['closing'], D(2870))

    def test_founders_block(self):
        self.assertEqual(self.data()['founders']['totals']['balance'], D(4700))

    def test_investor_sees_only_own_founder(self):
        data = reports.dashboard(self.year, None, self.investor)
        self.assertEqual([r['counterparty'].name for r in data['founders']['rows']], ['Учредитель Один'])

    def test_page(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('report_dashboard') + f'?school={self.school.pk}')
        for text in ('Прибыль и убытки', 'Доходы по источникам', 'Расходы по статьям', 'Движение денег по разделам',
                     'Банк и касса', 'Расчёты с учредителями', 'EBITDA'):
            self.assertContains(response, text)

    def test_director_page_has_no_founders(self):
        self.client.force_login(self.director)
        self.assertNotContains(self.client.get(reverse('report_dashboard')), 'Расчёты с учредителями</h2>')

    def test_empty_school(self):
        data = self.data(self.other)
        self.assertEqual(data['kpi']['income'], D(0))
        self.assertIsNone(data['kpi']['margin'])


class SummaryTests(ReportBase):
    def test_count_and_sum_tables(self):
        data = reports.contract_summary(self.year, self.school)
        count = {r.label: r.total for r in data['count']}
        self.assertEqual((count['Актуальные'], count['Отменённые'], count['Итого']), (1, 1, 2))
        self.assertEqual(data['sum'][-1].total, D(2000))
        self.assertEqual(data['columns'], [date(2026, 6, 1)])

    def test_classes_inside_status(self):
        Contract.objects.filter(pk=self.c1.pk).update(student_class='5')
        Contract.objects.create(number='11', school=self.school, academic_year=self.year, date=date(2026, 7, 1),
                                student_class='10', tuition_fee=D(1), duration_months=10)
        Contract.objects.create(number='12', school=self.school, academic_year=self.year, date=date(2026, 7, 1),
                                student_class='5', tuition_fee=D(1), duration_months=10)
        data = reports.contract_summary(self.year, self.school)
        labels = [r.label for r in data['count']]
        self.assertEqual(labels[:3], ['Актуальные', '5 класс', '10 класс'])    # 10 после 5, а не раньше
        five = next(r for r in data['count'] if r.label == '5 класс')
        self.assertEqual((five.by_month[date(2026, 6, 1)], five.by_month[date(2026, 7, 1)], five.total), (1, 1, 2))
        self.assertEqual(len(data['columns']), 2)

    def test_month_block(self):
        data = reports.contract_summary(self.year, self.school, '2026-10', today=date(2026, 11, 6))
        self.assertEqual((data['paid'], data['debts'], data['active_debts'], data['overpaid'], data['net']),
                         (D(400), D(600), D(600), D(0), D(-600)))
        september = reports.contract_summary(self.year, self.school, '2026-09', today=date(2026, 11, 6))
        self.assertEqual((september['paid'], september['debts']), (D(1000), D(0)))

    def test_whole_year(self):
        data = reports.contract_summary(self.year, self.school, '', today=date(2026, 11, 6))
        self.assertEqual((data['month'], data['paid'], data['debts']), ('', D(1400), D(1600)))   # сент–нояб: 3000 − 1400

    def test_bad_month_means_whole_year(self):
        self.assertEqual(reports.contract_summary(self.year, self.school, '2020-01')['month'], '')

    def test_page(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('report_summary') + f'?school={self.school.pk}&month=2026-10')
        self.assertContains(response, 'Количество составленных договоров')
        self.assertContains(response, '<option value="2026-10" selected>')
        self.assertContains(response, 'Весь год')


class CashByMethodTests(ReportBase):
    def data(self):
        return reports.cash_by_method(self.year, self.school)

    def rows(self, section):
        rows, current = {}, None
        for r in self.data()['rows']:
            if r.style == 'section':
                current = r.label
            elif current == section:
                rows[r.label] = r
        return rows

    def test_inflows(self):
        rows = self.rows('Поступления')
        self.assertEqual((rows['Расчётный счёт'].total, rows['Касса'].total, rows['Итого'].total), (D(5770), D(1700), D(7470)))
        self.assertEqual(rows['Касса'].by_month[date(2026, 8, 1)], D(1000))

    def test_outflows(self):
        rows = self.rows('Расходы')
        self.assertEqual((rows['Расчётный счёт'].total, rows['Касса'].total, rows['Итого'].total), (D(1000), D(3600), D(4600)))

    def test_running_balance(self):
        rows = self.rows('Остаток на конец месяца')
        cash = rows['Касса']
        self.assertEqual((cash.by_month[date(2026, 8, 1)], cash.by_month[date(2026, 9, 1)], cash.by_month[date(2026, 10, 1)]),
                         (D(1000), D(1700), D(-1900)))
        self.assertEqual(cash.by_month[date(2027, 6, 1)], D(-1900))
        self.assertEqual(cash.total, D(-1900))       # «Всего» — остаток на конец, а не сумма месяцев
        self.assertEqual(rows['Итого'].total, D(2870))

    def test_balance_matches_dashboard(self):
        dash = reports.dashboard(self.year, self.school)
        self.assertEqual(self.rows('Остаток на конец месяца')['Итого'].total, dash['bank_cash']['total']['closing'])

    def test_terminal_row_always_shown(self):
        self.assertIn('Терминал', self.rows('Поступления'))

    def test_page(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse('report_cash_methods'))
        self.assertContains(response, 'Остаток на конец месяца')


class DebtsMonthTests(ReportBase):
    def test_october(self):
        data = reports.debts_for_month(self.year, self.school, '2026-10')
        self.assertEqual([r['contract'].number for r in data['rows']], ['10'])
        r = data['rows'][0]
        self.assertEqual((r['accrued'], r['paid'], r['debt']), (D(2000), D(1400), D(600)))
        self.assertEqual(data['total']['debt'], D(600))

    def test_september_no_debt(self):
        self.assertEqual(reports.debts_for_month(self.year, self.school, '2026-09')['rows'], [])

    def test_default_month_is_current(self):
        data = reports.debts_for_month(self.year, self.school, '', today=date(2026, 11, 6))
        self.assertEqual(data['month'], '2026-11')

    def test_page_hides_personal_data_from_investor(self):
        Contract.objects.filter(pk=self.c1.pk).update(parents_full_name='Родитель Секретный')
        self.client.force_login(self.investor)
        response = self.client.get(reverse('report_debts') + f'?school={self.school.pk}&month=2026-10')
        self.assertContains(response, 'Ученик Десятый')
        self.assertNotContains(response, 'Родитель Секретный')
        self.client.force_login(self.director)
        self.assertContains(self.client.get(reverse('report_debts') + '?month=2026-10'), 'Родитель Секретный')

    def test_csv(self):
        self.client.force_login(self.director)
        text = self.client.get(reverse('report_debts') + '?month=2026-10&export=csv').content.decode('utf-8-sig')
        self.assertIn('10;Ученик Десятый', text)


class TariffTests(ReportBase):
    def test_groups(self):
        for n, fee in (('21', 2000), ('22', 2000), ('23', 3000)):
            Contract.objects.create(number=n, school=self.school, academic_year=self.year, date=date(2026, 9, 1),
                                    tuition_fee=D(fee), duration_months=10)
        data = reports.active_by_tariff(self.year, self.school)
        self.assertEqual([(r['amount'], r['count'], r['sum']) for r in data['rows']],
                         [(D(1000), 1, D(1000)), (D(2000), 2, D(4000)), (D(3000), 1, D(3000))])
        self.assertEqual((data['count'], data['sum']), (4, D(8000)))

    def test_cancelled_excluded(self):
        self.assertEqual(reports.active_by_tariff(self.year, self.school)['count'], 1)

    def test_page_for_employee(self):
        self.client.force_login(self.employee)
        self.assertContains(self.client.get(reverse('report_tariffs')), 'Сумма в месяц')


class PivotTests(ReportBase):
    def test_income_by_method_and_label(self):
        data = reports.money_pivots(self.year, self.school)
        income = {r.label: r.total for r in data['income']}
        self.assertEqual((income['Расчётный счёт'], income['Касса'], income['Итого']), (D(5770), D(1700), D(7470)))
        self.assertEqual(income['Учредитель Один'], D(5000))
        self.assertIn('Перевод: Касса → Расчётный счёт', income)

    def test_expense_rows_under_method(self):
        data = reports.money_pivots(self.year, self.school)
        labels = [r.label for r in data['expense']]
        self.assertLess(labels.index('Касса'), labels.index('Аренда'))
        self.assertEqual(data['expense'][-1].total, D(4600))

    def test_empty_methods_hidden(self):
        labels = [r.label for r in reports.money_pivots(self.year, self.school)['income']]
        self.assertNotIn('Терминал', labels)

    def test_page(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('report_pivots')), 'Способ и статья')


class PaymentControlLayoutTests(ReportBase):
    def test_total_running_balance(self):
        data = reports.payment_control(self.year, self.school)
        cells = data['total']['cells']
        self.assertEqual((cells[0]['running'], cells[1]['running']), (D(0), D(-600)))
        self.assertEqual(len(data['groups']), len(data['labels']) + 1)

    def test_page_has_triplets_and_personal_columns(self):
        Contract.objects.filter(pk=self.c1.pk).update(parents_full_name='Родитель Видимый')
        self.client.force_login(self.director)
        response = self.client.get(reverse('report_payment_control'))
        body = response.content.decode()
        self.assertGreaterEqual(body.count('>Стоимость<'), len(response.context['data']['groups']))
        self.assertContains(response, 'Родитель Видимый')
        self.client.force_login(self.investor)
        self.assertNotContains(self.client.get(reverse('report_payment_control')), 'Родитель Видимый')
