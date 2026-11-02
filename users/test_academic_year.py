"""Учебный год, первый месяц обучения и отмена договора без даты."""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from users.finance import billing_months, get_debt_details
from users.models import AcademicYear, Contract, Payment, School, User

PASSWORD = 'Strong-pass-2026'


class AcademicYearBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Учебный год 2026–2027 создаёт миграция
        cls.year = AcademicYear.objects.get(name='2026–2027')
        cls.school = School.objects.create(name='Школа Г')
        cls.owner = User.objects.create_user(username='ay_owner', phone_number='+998930000201', password=PASSWORD,
                                             role='OWNER', first_name='Владелец')
        cls.director = User.objects.create_user(username='ay_dir', phone_number='+998930000202', password=PASSWORD,
                                                role='DIRECTOR', school=cls.school, first_name='Директор')

    def make(self, number, signed, **kwargs):
        defaults = dict(number=number, school=self.school, date=signed, duration_months=10,
                        student_full_name=f'Ученик {number}', tuition_fee=Decimal(1000))
        defaults.update(kwargs)
        return Contract.objects.create(**defaults)

    def pay(self, contract, month, amount=1000, reversed_=False):
        payment = Payment.objects.create(contract=contract, for_month=month, amount=Decimal(amount), payment_date=month)
        if reversed_:
            payment.reverse(None, 'ошибка')
        return payment

    def fresh(self, contract):
        return Contract.objects.get(pk=contract.pk)


class AcademicYearRulesTests(AcademicYearBase):
    def test_migration_created_current_year(self):
        self.assertEqual((self.year.start_month, self.year.end_month, self.year.months_count),
                         (date(2026, 9, 1), date(2027, 6, 1), 10))

    def test_year_for_contract_date(self):
        cases = {
            date(2026, 6, 1): self.year,     # подписан летом — на следующий учебный год
            date(2026, 10, 16): self.year,
            date(2027, 5, 20): self.year,
            date(2026, 1, 10): None,         # слишком рано
            date(2027, 7, 1): None,          # год уже закончился
        }
        for signed, expected in cases.items():
            self.assertEqual(AcademicYear.for_date(signed), expected, signed)

    def test_summer_contract_starts_in_september_and_ends_in_june(self):
        contract = self.make('S-1', date(2026, 6, 1))
        self.assertEqual(contract.academic_year, self.year)
        months = billing_months(contract)
        self.assertEqual((months[0], months[-1], len(months)), (date(2026, 9, 1), date(2027, 6, 1), 10))

    def test_late_contract_starts_in_its_own_month(self):
        contract = self.make('S-2', date(2026, 10, 16), duration_months=9)
        months = billing_months(contract)
        self.assertEqual((months[0], months[-1]), (date(2026, 10, 1), date(2027, 6, 1)))

    def test_contract_outside_any_year_keeps_old_rule(self):
        contract = self.make('S-3', date(2025, 3, 15), duration_months=3)
        self.assertIsNone(contract.academic_year)
        self.assertEqual(contract.first_billing_month, date(2025, 3, 1))

    def test_explicit_first_month_is_kept_and_normalized(self):
        contract = self.make('S-4', date(2026, 6, 1), billing_start=date(2026, 11, 20))
        self.assertEqual(self.fresh(contract).billing_start, date(2026, 11, 1))

    def test_no_false_debt_for_summer(self):
        contract = self.make('S-5', date(2026, 6, 1))
        debt, months = get_debt_details(self.fresh(contract), date(2026, 8, 1))
        self.assertEqual(debt, 0)
        debt, months = get_debt_details(self.fresh(contract), date(2026, 11, 1))
        self.assertEqual((debt, len(months)), (Decimal(3000), 3))


class CancellationWithoutDateTests(AcademicYearBase):
    def test_billing_ends_with_last_paid_month(self):
        contract = self.make('C-1', date(2026, 6, 1), is_cancelled=True)
        self.pay(contract, date(2026, 9, 1))
        self.pay(contract, date(2026, 10, 1))
        contract = self.fresh(contract)
        self.assertIsNone(contract.cancelled_at)
        self.assertTrue(contract.cancellation_date_unknown)
        self.assertEqual(contract.last_billing_month, date(2026, 10, 1))
        self.assertEqual(get_debt_details(contract, date(2027, 3, 1))[0], 0)

    def test_partial_last_month_stays_as_debt(self):
        contract = self.make('C-2', date(2026, 6, 1), is_cancelled=True)
        self.pay(contract, date(2026, 9, 1))
        self.pay(contract, date(2026, 10, 1), amount=400)
        self.assertEqual(get_debt_details(self.fresh(contract), date(2027, 3, 1))[0], Decimal(600))

    def test_without_payments_nothing_is_charged(self):
        contract = self.make('C-3', date(2026, 6, 1), is_cancelled=True)
        self.assertIsNone(contract.last_billing_month)
        self.assertEqual(get_debt_details(self.fresh(contract), date(2027, 3, 1)), (0, []))

    def test_reversed_payment_does_not_extend_billing(self):
        contract = self.make('C-4', date(2026, 6, 1), is_cancelled=True)
        self.pay(contract, date(2026, 9, 1))
        self.pay(contract, date(2026, 12, 1), reversed_=True)
        self.assertEqual(self.fresh(contract).last_billing_month, date(2026, 9, 1))

    def test_known_date_still_wins(self):
        contract = self.make('C-5', date(2026, 6, 1), is_cancelled=True, cancelled_at=date(2026, 12, 5))
        self.pay(contract, date(2026, 9, 1))
        self.assertEqual(self.fresh(contract).last_billing_month, date(2026, 11, 1))

    def test_status(self):
        active = self.make('C-6', date(2026, 6, 1))
        cancelled = self.make('C-7', date(2026, 6, 1), is_cancelled=True)
        finished = self.make('C-8', date(2025, 1, 1), duration_months=2)
        self.assertEqual(active.status(date(2026, 11, 2))[0], 'active')
        self.assertEqual(cancelled.status(date(2026, 11, 2))[0], 'cancelled')
        self.assertEqual(finished.status(date(2026, 11, 2))[0], 'finished')


class ContractPagesTests(AcademicYearBase):
    def test_cancelled_contract_cannot_be_deleted(self):
        cancelled = self.make('D-1', date(2026, 6, 1), is_cancelled=True)
        active = self.make('D-2', date(2026, 6, 1))
        self.client.force_login(self.director)
        response = self.client.post(reverse('contract_delete', args=[cancelled.pk]), follow=True)
        self.assertContains(response, 'не удаляется')
        self.assertTrue(Contract.objects.filter(pk=cancelled.pk).exists())
        self.client.post(reverse('contract_delete', args=[active.pk]))
        self.assertFalse(Contract.objects.filter(pk=active.pk).exists())

    def test_list_filters_and_marks(self):
        self.make('F-1', date(2026, 6, 1))
        no_date = self.make('F-2', date(2026, 6, 1), is_cancelled=True)
        self.pay(no_date, date(2026, 9, 1))
        self.make('F-3', date(2026, 6, 1), is_cancelled=True, cancelled_at=date(2026, 10, 1))
        self.make('F-4', date(2025, 2, 1))
        self.client.force_login(self.owner)
        url = reverse('contract_list')

        def numbers(query):
            return sorted(c.number for c in self.client.get(url + query).context['contracts_list'])

        self.assertEqual(numbers('?status=no_cancel_date'), ['F-2'])
        self.assertEqual(numbers('?status=cancelled'), ['F-2', 'F-3'])
        self.assertEqual(numbers('?status=active'), ['F-1', 'F-4'])
        self.assertEqual(numbers(f'?year={self.year.pk}'), ['F-1', 'F-2', 'F-3'])
        body = self.client.get(url + '?status=no_cancel_date').content.decode()
        self.assertIn('дата не указана', body)
        self.assertIn('начисления по 09.2026', body)

    def test_detail_shows_unknown_cancellation(self):
        contract = self.make('F-5', date(2026, 6, 1), is_cancelled=True)
        self.client.force_login(self.owner)
        body = self.client.get(reverse('contract_detail', args=[contract.pk])).content.decode()
        self.assertIn('дата отмены не указана', body)
        self.assertIn('обучение с 09.2026', body)

    def test_export_has_billing_columns(self):
        contract = self.make('F-6', date(2026, 6, 1), is_cancelled=True)
        self.pay(contract, date(2026, 9, 1))
        self.client.force_login(self.owner)
        body = self.client.get(reverse('contract_list') + '?export=csv').content.decode('utf-8-sig')
        header, row = body.splitlines()[:2]
        self.assertIn('Первый месяц обучения', header)
        self.assertIn('Начисления по', header)
        self.assertIn('09.2026', row)
        self.assertIn('не указана', row)

    def test_form_first_month_and_default_year(self):
        self.client.force_login(self.director)
        form = self.client.get(reverse('contract_add')).context['form']
        self.assertIn('billing_start', form.fields)
        data = {
            'number': 'F-7', 'date': '2026-06-01', 'month': 'Июнь', 'academic_year': self.year.pk,
            'billing_start': '2026-10', 'student_full_name': 'Ученик', 'student_birth_year': 2015, 'student_class': '5',
            'duration_months': 9, 'parents_full_name': 'Родитель', 'passport_data': 'AA1', 'inps': '1', 'address': 'Т',
            'phone_number': '+998901112233', 'specialization': 'Русский язык', 'tuition_fee': '1000',
            'discount_percentage': 0, 'discount_amount': 0, 'food_cost': 0, 'total_amount': 0, 'notes': '',
        }
        self.client.post(reverse('contract_add'), data)
        self.assertEqual(Contract.objects.get(number='F-7').first_billing_month, date(2026, 10, 1))
        data.update(number='F-8', billing_start='')
        self.client.post(reverse('contract_add'), data)
        self.assertEqual(Contract.objects.get(number='F-8').first_billing_month, date(2026, 9, 1))


class AcademicYearPagesTests(AcademicYearBase):
    def test_only_owner(self):
        self.client.force_login(self.director)
        for name in ('academic_year_list', 'academic_year_add'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('academic_year_list')), '2026–2027')

    def test_create_and_validation(self):
        self.client.force_login(self.owner)
        url = reverse('academic_year_add')
        overlap = self.client.post(url, {'name': 'X', 'start_month': '2027-01', 'end_month': '2027-08'})
        self.assertContains(overlap, '2026–2027')
        backwards = self.client.post(url, {'name': 'Y', 'start_month': '2028-06', 'end_month': '2027-09'})
        self.assertFormError(backwards.context['form'], 'end_month', 'Последний месяц раньше первого.')
        self.client.post(url, {'name': '2027–2028', 'start_month': '2027-09', 'end_month': '2028-06'})
        year = AcademicYear.objects.get(name='2027–2028')
        self.assertEqual((year.start_month, year.end_month), (date(2027, 9, 1), date(2028, 6, 1)))
        self.assertEqual(AcademicYear.for_date(date(2027, 7, 1)), year)
