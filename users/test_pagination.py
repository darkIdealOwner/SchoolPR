"""Списки по страницам: договоры, оплаты, долги и остальные таблицы; выбор «показывать по», фильтры не теряются.

Все данные вымышленные.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from users.models import Contract, Payment, School, Transaction, User

PASSWORD = 'Strong-pass-2026'
D = Decimal


class PaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name='Тест Страницы')
        cls.other = School.objects.create(name='Тест Другая')
        cls.owner = User.objects.create_user(username='pg_owner', phone_number='+998930000901', password=PASSWORD, role='OWNER')
        cls.director = User.objects.create_user(username='pg_dir', phone_number='+998930000902', password=PASSWORD,
                                                role='DIRECTOR', school=cls.school)
        for n in range(1, 31):
            Contract.objects.create(number=f'P-{n}', school=cls.school, date=date(2026, 9, 1), tuition_fee=D(1000) * n,
                                    student_full_name=f'Тест Ученик {n:02d}', duration_months=10)
        Contract.objects.create(number='X-1', school=cls.other, date=date(2026, 9, 1), tuition_fee=D(1),
                                student_full_name='Тест Чужой', duration_months=10)

    def get(self, name, user=None, **params):
        self.client.force_login(user or self.owner)
        return self.client.get(reverse(name), params)

    # договоры
    def test_contracts_first_page_has_ten(self):
        response = self.get('contract_list')
        self.assertEqual(len(response.context['contracts_list']), 10)
        self.assertContains(response, 'Показаны 1–10 из 31')

    def test_contracts_page_numbers_are_links(self):
        response = self.get('contract_list')
        self.assertContains(response, 'class="pager-link current"')
        self.assertContains(response, '?page=2')
        self.assertContains(response, '?page=4')

    def test_contracts_last_page(self):
        response = self.get('contract_list', page=4)
        self.assertEqual(len(response.context['contracts_list']), 1)
        self.assertContains(response, 'Показаны 31–31 из 31')

    def test_page_out_of_range_shows_last_page(self):
        response = self.get('contract_list', page=99)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].number, 4)

    def test_garbage_page_shows_first(self):
        response = self.get('contract_list', page='abc')
        self.assertEqual((response.status_code, response.context['page_obj'].number), (200, 1))

    def test_per_page_choice(self):
        for size, expected in ((25, 25), (50, 31), (100, 31)):
            response = self.get('contract_list', per_page=size)
            self.assertEqual(len(response.context['contracts_list']), expected, size)

    def test_unknown_per_page_falls_back_to_ten(self):
        for value in ('7', '1000', 'abc', '-5'):
            self.assertEqual(len(self.get('contract_list', per_page=value).context['contracts_list']), 10, value)

    def test_per_page_is_selected_in_dropdown(self):
        self.assertContains(self.get('contract_list', per_page=25), '<option value="25" selected>')

    def test_page_links_keep_filters(self):
        response = self.get('contract_list', q='Тест', status='active', per_page=10)
        link = [l for l in response.content.decode().split('href="') if l.startswith('?') and 'page=2' in l][0]
        for part in ('q=', 'status=active', 'per_page=10'):
            self.assertIn(part, link.split('"')[0])

    def test_per_page_form_keeps_filters_but_not_page(self):
        html = self.get('contract_list', status='active', page=2).content.decode()
        form = html[html.index('class="pager-size"'):]
        form = form[:form.index('</form>')]
        self.assertIn('name="status" value="active"', form)
        self.assertNotIn('name="page"', form)

    def test_no_pager_when_few_rows(self):
        response = self.get('contract_list', q='Чужой')
        self.assertNotContains(response, 'class="pager"')

    def test_director_paginates_only_own_school(self):
        response = self.get('contract_list', user=self.director)
        self.assertContains(response, 'из 30')

    def test_csv_export_is_not_paginated(self):
        response = self.get('contract_list', export='csv')
        self.assertEqual(len(response.content.decode('utf-8-sig').strip().splitlines()), 32)

    # оплаты за месяц
    def test_payments_month_is_paginated(self):
        response = self.get('payment_list', month='2026-09')
        self.assertEqual(len(response.context['contracts']), 10)
        self.assertContains(response, 'Показаны 1–10 из 31')

    def test_payments_page_two_and_month_kept(self):
        response = self.get('payment_list', month='2026-09', page=2)
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertContains(response, 'month=2026-09')

    def test_payments_paid_sums_on_later_pages(self):
        c = Contract.objects.get(number='P-1')
        Payment.objects.create(contract=c, for_month=date(2026, 9, 1), amount=D(400), payment_date=date(2026, 9, 2))
        response = self.get('payment_list', month='2026-09', per_page=100)
        row = next(x for x in response.context['contracts'] if x.number == 'P-1')
        self.assertEqual((row.this_month_paid, row.this_month_balance), (D(400), D(600)))

    def test_payments_csv_has_all_rows(self):
        response = self.get('payment_list', month='2026-09', export='csv')
        self.assertEqual(len(response.content.decode('utf-8-sig').strip().splitlines()), 32)

    # долги
    def test_debts_are_paginated(self):
        response = self.get('debt_list', mode='monthly', month='2026-10')
        self.assertEqual(len(response.context['debtors_page']), 10)
        self.assertEqual(len(response.context['debtors']), 31)
        self.assertContains(response, 'Показаны 1–10 из 31')

    def test_debts_total_counts_all_pages(self):
        response = self.get('debt_list', mode='monthly', month='2026-09')
        self.assertEqual(response.context['debt_total'], sum(c.total_debt for c in response.context['debtors']))
        self.assertContains(response, 'Должников: 31')

    def test_debts_keep_mode_in_links(self):
        response = self.get('debt_list', mode='monthly', month='2026-10')
        self.assertContains(response, 'mode=monthly')

    def test_debts_csv_has_all_rows(self):
        response = self.get('debt_list', mode='monthly', month='2026-10', export='csv')
        self.assertEqual(len(response.content.decode('utf-8-sig').strip().splitlines()), 32)

    def test_debts_biggest_first_across_pages(self):
        first = self.get('debt_list', mode='monthly', month='2026-09').context['debtors_page'][0]
        last = self.get('debt_list', mode='monthly', month='2026-09', page=4).context['debtors_page'][0]
        self.assertGreater(first.total_debt, last.total_debt)

    # остальные таблицы
    def test_transactions_paginated(self):
        for n in range(12):
            Transaction.objects.create(school=self.school, type='INCOME', amount=D(n + 1), date=date(2026, 9, 1))
        response = self.get('transaction_list')
        self.assertContains(response, 'Показаны 1–10 из 12')

    def test_users_paginated(self):
        for n in range(12):
            User.objects.create_user(username=f'pg_u{n}', phone_number=f'+9989300010{n:02d}', password=PASSWORD,
                                     role='EMPLOYEE', school=self.school)
        response = self.get('user_list', per_page=25)
        self.assertEqual(response.context['page_obj'].paginator.per_page, 25)
