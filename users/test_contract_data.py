"""Договор: два телефона, скидка процентом и суммой с причиной, неполные данные, братья и сёстры."""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from users.excel_import import split_phones
from users.forms import ContractForm
from users.models import Contract, School, User
from users.test_excel_import import CONTRACT_HEADERS, contract_row, xlsx

PASSWORD = 'Strong-pass-2026'


def form_data(**overrides):
    data = {
        'number': 'K-1', 'date': '2026-06-01', 'month': 'Июнь', 'student_full_name': 'Ученик', 'student_birth_year': 2015,
        'student_class': '5', 'duration_months': 10, 'parents_full_name': 'Родитель', 'passport_data': 'AA1234567',
        'inps': '12345678901234', 'address': 'Заамин', 'phone_number': '901234567', 'phone_number_2': '',
        'specialization': 'Узбекский язык', 'tuition_fee': '1936000', 'discount_percentage': 0, 'discount_fixed': '',
        'discount_reason': '', 'discount_amount': 0, 'food_cost': '616000', 'total_amount': 0, 'notes': '',
    }
    data.update(overrides)
    return data


class DataBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name='Школа Д')
        cls.other = School.objects.create(name='Школа Е')
        cls.owner = User.objects.create_user(username='cd_owner', phone_number='+998930000301', password=PASSWORD,
                                             role='OWNER', first_name='Владелец')
        cls.director = User.objects.create_user(username='cd_dir', phone_number='+998930000302', password=PASSWORD,
                                                role='DIRECTOR', school=cls.school, first_name='Директор')
        cls.investor = User.objects.create_user(username='cd_inv', phone_number='+998930000303', password=PASSWORD,
                                                role='INVESTOR', first_name='Инвестор')

    def make(self, number, **kwargs):
        defaults = dict(number=number, school=self.school, date=date(2026, 6, 1), duration_months=10,
                        student_full_name=f'Ученик {number}', tuition_fee=Decimal(1936000), food_cost=Decimal(616000),
                        passport_data='AA1234567', inps='12345678901234', address='Заамин', phone_number='+998901234567')
        defaults.update(kwargs)
        return Contract.objects.create(**defaults)


class DiscountTests(DataBase):
    def test_percent_and_fixed_sum_add_up(self):
        contract = self.make('K-1', discount_percentage=10, discount_fixed=Decimal(100000))
        self.assertEqual(contract.discount_amount, Decimal(293600))
        self.assertEqual(contract.total_amount, Decimal(1936000 - 293600 + 616000))

    def test_fixed_sum_only(self):
        contract = self.make('K-2', discount_fixed=Decimal(968000), discount_reason='STAFF_CHILD')
        self.assertEqual(contract.total_amount, Decimal(968000 + 616000))

    def test_discount_never_exceeds_tuition(self):
        contract = self.make('K-3', discount_percentage=100, discount_fixed=Decimal(500000))
        self.assertEqual(contract.discount_amount, Decimal(1936000))
        self.assertEqual(contract.total_amount, Decimal(616000))   # остаётся только питание


class ContractFormTests(DataBase):
    def form(self, **overrides):
        return ContractForm(data=form_data(**overrides), user=self.director)

    def test_phones_are_normalized(self):
        form = self.form(phone_number='90 123 45 67', phone_number_2='+998 (90) 765-43-21')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.cleaned_data['phone_number'], form.cleaned_data['phone_number_2']),
                         ('+998901234567', '+998907654321'))

    def test_bad_second_phone(self):
        self.assertIn('phone_number_2', self.form(phone_number_2='12345').errors)

    def test_passport_inps_address_are_optional(self):
        self.assertTrue(self.form(passport_data='', inps='', address='').is_valid())

    def test_passport_and_inps_cleanup(self):
        form = self.form(passport_data='ab 1111111', inps='301 0199 0000 011')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.cleaned_data['passport_data'], form.cleaned_data['inps']), ('AB1111111', '30101990000011'))
        self.assertIn('inps', self.form(inps='32508X').errors)

    def test_class_range(self):
        self.assertIn('student_class', self.form(student_class='2013').errors)
        self.assertTrue(self.form(student_class='5-А').is_valid())
        self.assertTrue(self.form(student_class='0').is_valid())

    def test_discount_limits(self):
        self.assertIn('discount_percentage', self.form(discount_percentage=150).errors)
        self.assertIn('discount_fixed', self.form(discount_fixed='-5').errors)

    def test_create_through_page(self):
        self.client.force_login(self.director)
        self.client.post(reverse('contract_add'), form_data(number='K-9', discount_fixed='100000', discount_reason='SOCIAL',
                                                            phone_number_2='331112233'))
        contract = Contract.objects.get(number='K-9')
        self.assertEqual((contract.discount_fixed, contract.discount_reason, contract.phone_number_2),
                         (Decimal(100000), 'SOCIAL', '+998331112233'))


class WarningsAndSiblingsTests(DataBase):
    def test_warnings(self):
        contract = self.make('W-1', passport_data='', inps='1234567890123', address='')
        warnings = ' | '.join(contract.data_warnings)
        self.assertIn('паспорт', warnings)
        self.assertIn('14 цифр', warnings)
        self.assertIn('адрес', warnings)
        self.assertEqual(self.make('W-2').data_warnings, [])
        self.assertIn('AA1234567', ' '.join(self.make('W-3', passport_data='AB12345678').data_warnings))

    def test_siblings_by_passport_or_inps(self):
        first = self.make('S-1', passport_data='AB2222222', inps='30202990000022')
        second = self.make('S-2', passport_data='AB2222222', inps='')
        third = self.make('S-3', passport_data='', inps='30202990000022')
        stranger = self.make('S-4', passport_data='AB0000001', inps='11111111111111')
        self.assertEqual(sorted(c.number for c in first.siblings()), ['S-2', 'S-3'])
        self.assertEqual(list(stranger.siblings()), [])
        self.assertEqual(list(self.make('S-5', passport_data='', inps='').siblings()), [])
        self.assertNotIn(third, Contract.objects.filter(pk=second.pk))

    def test_sibling_discount_without_siblings_is_flagged(self):
        alone = self.make('S-6', passport_data='AC1112223', inps='22222222222222', discount_percentage=10, discount_reason='SIBLING_2')
        self.assertTrue(any('брата' in w for w in alone.data_warnings))
        self.make('S-7', passport_data='AC1112223', inps='22222222222222')
        self.assertEqual(Contract.objects.get(pk=alone.pk).data_warnings, [])

    def test_detail_shows_siblings_of_own_school_only(self):
        child = self.make('S-8', passport_data='AE5555555', inps='')
        self.make('S-9', passport_data='AE5555555', inps='', student_full_name='Брат из этой школы')
        self.make('S-10', passport_data='AE5555555', inps='', school=self.other, student_full_name='Сестра из другой школы')
        self.client.force_login(self.director)
        body = self.client.get(reverse('contract_detail', args=[child.pk])).content.decode()
        self.assertIn('Брат из этой школы', body)
        self.assertNotIn('Сестра из другой школы', body)
        self.client.force_login(self.owner)
        self.assertIn('Сестра из другой школы', self.client.get(reverse('contract_detail', args=[child.pk])).content.decode())

    def test_incomplete_filter_and_investor(self):
        self.make('I-1')
        self.make('I-2', inps='')
        self.make('I-3', passport_data='ab123')
        self.client.force_login(self.owner)
        response = self.client.get(reverse('contract_list') + '?status=incomplete')
        self.assertEqual(sorted(c.number for c in response.context['contracts_list']), ['I-2', 'I-3'])
        self.client.force_login(self.investor)
        filters = [v for v, _ in self.client.get(reverse('contract_list')).context['status_filters']]
        self.assertNotIn('incomplete', filters)

    def test_export_columns(self):
        self.make('X-1', phone_number_2='+998331112233', discount_fixed=Decimal(5000), discount_reason='PRIVILEGE')
        self.client.force_login(self.owner)
        header, row = self.client.get(reverse('contract_list') + '?export=csv').content.decode('utf-8-sig').splitlines()[:2]
        for name in ('Доп. телефон', 'Скидка суммой', 'Причина скидки'):
            self.assertIn(name, header)
        self.assertIn('+998331112233', row)
        self.assertIn('Льгота', row)


class ImportTests(DataBase):
    def test_split_phones(self):
        self.assertEqual(split_phones('901234567\n907654321'), ('+998901234567', '+998907654321'))
        self.assertEqual(split_phones('911112233, 331112233'), ('+998911112233', '+998331112233'))
        self.assertEqual(split_phones('+998 90 111 22 33'), ('+998901112233', ''))

    def test_workbook_like_rows(self):
        headers = CONTRACT_HEADERS + ['Скидка суммой', 'Причина скидки']
        self.client.force_login(self.director)
        rows = [
            contract_row('M-1', **{'Телефон': '901234567\n907654321', 'Паспорт': '', 'ИНПС': '', 'Причина скидки': 'второй ребёнок'}),
            contract_row('M-2', **{'Скидка %': '', 'Скидка суммой': 968000, 'Причина скидки': 'Ребёнок сотрудника'}),
            contract_row('M-3', **{'Класс': 2013}),
        ]
        response = self.client.post(reverse('excel_import', args=['contracts']), {'file': xlsx(headers, rows)})
        statuses = [r.status for r in response.context['results']]
        self.assertEqual(statuses, ['ok', 'ok', 'error'], [r.errors for r in response.context['results']])
        self.client.post(reverse('excel_import', args=['contracts']), {'action': 'confirm', 'token': response.context['token']})
        first = Contract.objects.get(number='M-1')
        self.assertEqual((first.phone_number, first.phone_number_2, first.discount_reason), ('+998901234567', '+998907654321', 'SIBLING_2'))
        self.assertTrue(first.data_warnings)
        second = Contract.objects.get(number='M-2')
        self.assertEqual((second.discount_fixed, second.discount_reason), (Decimal(968000), 'STAFF_CHILD'))
