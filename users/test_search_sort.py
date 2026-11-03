"""Поиск по любому полю и сортировка (номера по возрастанию/убыванию и др.) в договорах, оплатах и долгах.

Все данные вымышленные.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from users.models import AcademicYear, Contract, Payment, School, User

PASSWORD = 'Strong-pass-2026'
D = Decimal


class SearchSortBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name='Тест Поиск')
        cls.other = School.objects.create(name='Тест Соседняя')
        make = lambda u, phone, role, school=None: User.objects.create_user(username=u, phone_number=phone, password=PASSWORD,
                                                                              role=role, school=school)
        cls.owner = make('ss_owner', '+998930001001', 'OWNER')
        cls.director = make('ss_dir', '+998930001002', 'DIRECTOR', cls.school)
        cls.investor = make('ss_inv', '+998930001003', 'INVESTOR')

        def c(number, name, **kw):
            data = dict(number=number, school=cls.school, date=date(2026, 9, 1), tuition_fee=D(1000000),
                        student_full_name=name, duration_months=10, student_class='4', student_birth_year=2015,
                        parents_full_name='Тест Родитель', phone_number='+998901110000')
            data.update(kw)
            return Contract.objects.create(**data)

        cls.c9 = c('9', 'Тест Алиев Бобур', phone_number='+998901234567', passport_data='AB1234567', student_class='7')
        cls.c10 = c('10', 'Тест Каримова Дилноза', tuition_fee=D(2552000), inps='30101990000011', date=date(2026, 8, 15))
        cls.c94 = c('94', 'Тест Юсупов Жасур', address='Тестовая МФЙ, дом 5', notes='особый случай', student_birth_year=2012)
        cls.c155 = c('155', 'Тест Ахмедова Зарина', parents_full_name='Тест Ахмедов Рустам', phone_number_2='+998977654321')
        cls.c1001 = c('1001', 'Тест Бекова Лола', data_problem='строка 5: «Класс»: ошибка', specialization='Узбекский язык')
        cls.foreign = c('9001', 'Тест Чужой Алиев', school=cls.other)

    def numbers(self, response, key='contracts_list'):
        return [c.number for c in response.context[key]]

    def get(self, name, user=None, **params):
        self.client.force_login(user or self.owner)
        return self.client.get(reverse(name), params)


class ContractSearchTests(SearchSortBase):
    def search(self, q, user=None):
        return set(self.numbers(self.get('contract_list', user, q=q, per_page=100)))

    def test_by_number(self):
        self.assertEqual(self.search('155'), {'155'})

    def test_by_part_of_name(self):
        self.assertEqual(self.search('алиев'), {'9', '9001'})

    def test_several_words_all_must_match(self):
        self.assertEqual(self.search('Алиев Бобур'), {'9'})
        self.assertEqual(self.search('Алиев Каримова'), set())

    def test_by_phone_digits_in_any_format(self):
        for q in ('901234567', '+998 (90)-123-45-67', '90 123 45 67', '1234567'):
            self.assertEqual(self.search(q), {'9'}, q)

    def test_by_second_phone(self):
        self.assertEqual(self.search('977654321'), {'155'})

    def test_by_passport_and_inps(self):
        self.assertEqual(self.search('ab1234567'), {'9'})
        self.assertEqual(self.search('30101990000011'), {'10'})

    def test_by_parent_and_address_and_notes(self):
        self.assertEqual(self.search('Рустам'), {'155'})
        self.assertEqual(self.search('МФЙ'), {'94'})
        self.assertEqual(self.search('особый'), {'94'})

    def test_by_amount_with_and_without_spaces(self):
        self.assertEqual(self.search('2552000'), {'10'})
        self.assertEqual(self.search('2 552 000'), {'10'})

    def test_by_birth_year(self):
        self.assertEqual(self.search('2012'), {'94'})

    def test_by_date(self):
        self.assertEqual(self.search('15.08.2026'), {'10'})

    def test_by_class_specialization_school_year_problem(self):
        self.assertIn('9', self.search('7'))
        self.assertEqual(self.search('Узбекский'), {'1001'})
        self.assertEqual(self.search('Соседняя'), {'9001'})
        self.assertEqual(self.search('ошибка'), {'1001'})

    def test_by_academic_year(self):
        year = AcademicYear.objects.get(name='2026–2027')
        Contract.objects.filter(number='94').update(academic_year=year)
        Contract.objects.exclude(number='94').update(academic_year=None)
        self.assertEqual(self.search('2026–2027'), {'94'})

    def test_empty_query_returns_all(self):
        self.assertEqual(len(self.search('   ')), 6)

    def test_nothing_found(self):
        self.assertEqual(self.search('нетакого'), set())
        self.assertContains(self.get('contract_list', q='нетакого'), 'Договоры не найдены')

    def test_investor_cannot_find_by_personal_data(self):
        for q in ('901234567', 'AB1234567', 'Рустам', 'МФЙ', '30101990000011'):
            self.assertEqual(self.search(q, self.investor), set(), q)
        self.assertEqual(self.search('Бобур', self.investor), {'9'})

    def test_director_searches_only_own_school(self):
        self.assertEqual(self.search('Алиев', self.director), {'9'})

    def test_search_keeps_status_filter(self):
        Contract.objects.filter(number='9').update(is_cancelled=True)
        response = self.get('contract_list', q='Тест', status='cancelled')
        self.assertEqual(self.numbers(response), ['9'])

    def test_search_form_keeps_other_filters(self):
        html = self.get('contract_list', status='active', sort='number', per_page=25).content.decode()
        form = html[html.index('class="search-form"'):]
        form = form[:form.index('</form>')]
        for part in ('name="status" value="active"', 'name="sort" value="number"', 'name="per_page" value="25"'):
            self.assertIn(part, form)

    def test_csv_export_follows_search(self):
        response = self.get('contract_list', q='Алиев', export='csv')
        self.assertEqual(len(response.content.decode('utf-8-sig').strip().splitlines()), 3)

    def test_special_characters_do_not_break(self):
        for q in ('%', '_', "'", '"', '\\', '((', '+', '.', '01.13.2026'):
            self.assertEqual(self.get('contract_list', q=q).status_code, 200, q)


class ContractSortTests(SearchSortBase):
    def sorted_numbers(self, sort, user=None):
        return self.numbers(self.get('contract_list', user, sort=sort, per_page=100))

    def test_numbers_ascending_like_people_count(self):
        self.assertEqual(self.sorted_numbers('number'), ['9', '10', '94', '155', '1001', '9001'])

    def test_numbers_descending(self):
        self.assertEqual(self.sorted_numbers('-number'), ['9001', '1001', '155', '94', '10', '9'])

    def test_by_name(self):
        names = [c.student_full_name for c in self.get('contract_list', sort='student', per_page=100).context['contracts_list']]
        self.assertEqual(names, sorted(names))
        names_desc = [c.student_full_name for c in self.get('contract_list', sort='-student', per_page=100).context['contracts_list']]
        self.assertEqual(names_desc, sorted(names, reverse=True))

    def test_by_date(self):
        self.assertEqual(self.sorted_numbers('date')[0], '10')
        self.assertEqual(self.sorted_numbers('-date')[-1], '10')

    def test_by_total(self):
        self.assertEqual(self.sorted_numbers('-total')[0], '10')
        self.assertEqual(self.sorted_numbers('total')[-1], '10')

    def test_default_is_newest_first(self):
        self.assertEqual(self.sorted_numbers('')[0], '9001')

    def test_unknown_sort_is_default(self):
        self.assertEqual(self.sorted_numbers('drop table'), self.sorted_numbers(''))

    def test_sort_kept_across_pages(self):
        first = self.numbers(self.get('contract_list', sort='number', per_page=10))
        self.assertEqual(first[:3], ['9', '10', '94'])
        self.assertContains(self.get('contract_list', sort='number', q='Тест'), 'sort=number')

    def test_sort_select_shows_choice(self):
        response = self.get('contract_list', sort='-number')
        self.assertContains(response, '<option value="-number" selected>')

    def test_sort_and_search_together(self):
        self.assertEqual(self.numbers(self.get('contract_list', sort='-number', q='Алиев')), ['9001', '9'])

    def test_prefixed_numbers(self):
        for n in ('ZAM-100', 'ZAM-20', 'ZAM-3'):
            Contract.objects.create(number=n, school=self.school, date=date(2026, 9, 1), tuition_fee=D(1))
        ordered = [n for n in self.sorted_numbers('number') if n.startswith('ZAM')]
        self.assertEqual(ordered, ['ZAM-3', 'ZAM-20', 'ZAM-100'])


class PaymentListSearchSortTests(SearchSortBase):
    def page(self, **params):
        return self.get('payment_list', month='2026-09', per_page=100, **params)

    def test_search(self):
        self.assertEqual(self.numbers(self.page(q='Каримова'), 'contracts'), ['10'])

    def test_search_by_phone(self):
        self.assertEqual(self.numbers(self.page(q='90 123 45 67'), 'contracts'), ['9'])

    def test_sort_by_number(self):
        self.assertEqual(self.numbers(self.page(sort='number'), 'contracts'), ['9', '10', '94', '155', '1001', '9001'])
        self.assertEqual(self.numbers(self.page(sort='-number'), 'contracts')[0], '9001')

    def test_sort_by_balance(self):
        Payment.objects.create(contract=self.c94, for_month=date(2026, 9, 1), amount=D(1000000), payment_date=date(2026, 9, 3))
        rows = self.page(sort='-balance').context['contracts']
        self.assertEqual(rows[0].number, '10')
        self.assertEqual(rows[-1].number, '94')

    def test_unknown_sort_is_default(self):
        self.assertEqual(self.page(sort='xyz').context['sort'], '')

    def test_month_kept_in_search_form(self):
        self.assertContains(self.page(), 'name="month" value="2026-09"')

    def test_csv_follows_search(self):
        response = self.get('payment_list', month='2026-09', q='Каримова', export='csv')
        self.assertEqual(len(response.content.decode('utf-8-sig').strip().splitlines()), 2)

    def test_reset_link(self):
        self.assertContains(self.page(q='Каримова'), 'Сбросить поиск')


class DebtListSearchSortTests(SearchSortBase):
    def page(self, **params):
        return self.get('debt_list', mode='monthly', month='2026-10', per_page=100, **params)

    def test_default_biggest_debt_first(self):
        rows = self.page().context['debtors']
        self.assertEqual(rows[0].number, '10')
        self.assertEqual([c.total_debt for c in rows], sorted((c.total_debt for c in rows), reverse=True))

    def test_smallest_first(self):
        rows = self.page(sort='debt').context['debtors']
        self.assertEqual([c.total_debt for c in rows], sorted(c.total_debt for c in rows))

    def test_sort_by_number(self):
        self.assertEqual(self.numbers(self.page(sort='number'), 'debtors'), ['9', '10', '94', '155', '1001', '9001'])
        self.assertEqual(self.numbers(self.page(sort='-number'), 'debtors'), ['9001', '1001', '155', '94', '10', '9'])

    def test_sort_by_months_overdue(self):
        Payment.objects.create(contract=self.c94, for_month=date(2026, 9, 1), amount=D(1000000), payment_date=date(2026, 9, 3))
        rows = self.page(sort='-months').context['debtors']
        self.assertEqual(rows[-1].number, '94')
        self.assertEqual(rows[-1].unpaid_count, 1)

    def test_search(self):
        self.assertEqual(self.numbers(self.page(q='Юсупов'), 'debtors'), ['94'])

    def test_search_changes_total(self):
        response = self.page(q='Юсупов')
        self.assertEqual(response.context['debt_total'], response.context['debtors'][0].total_debt)

    def test_mode_kept_in_form(self):
        html = self.page().content.decode()
        form = html[html.index('class="list-tools"'):]
        self.assertIn('name="mode" value="monthly"', form[:form.index('</form>')])

    def test_csv_follows_search_and_sort(self):
        response = self.get('debt_list', mode='monthly', month='2026-10', q='Тест', sort='number', export='csv')
        lines = response.content.decode('utf-8-sig').strip().splitlines()
        self.assertEqual([line.split(';')[0] for line in lines[1:]], ['9', '10', '94', '155', '1001', '9001'])

    def test_director_only_own_school(self):
        response = self.get('debt_list', self.director, mode='monthly', month='2026-10')
        self.assertNotIn('9001', self.numbers(response, 'debtors'))
