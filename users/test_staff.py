"""Справочник сотрудников и зарплата: по сотрудникам и одной суммой."""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from integrations.importers import run_import
from users.models import Category, EmployeePayout, School, Staff, Transaction, User
from users.test_excel_import import xlsx

PASSWORD = 'Strong-pass-2026'


class StaffBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name='Школа Т')
        cls.other = School.objects.create(name='Школа У')
        make = lambda username, phone, role, school=None, name='': User.objects.create_user(
            username=username, phone_number=phone, password=PASSWORD, role=role, school=school, first_name=name or username)
        cls.owner = make('st_owner', '+998930000601', 'OWNER')
        cls.director = make('st_dir', '+998930000602', 'DIRECTOR', cls.school)
        cls.employee = make('st_emp', '+998930000603', 'EMPLOYEE', cls.school, 'Нодира')
        cls.investor = make('st_inv', '+998930000604', 'INVESTOR')
        cls.cook = Staff.objects.create(school=cls.school, full_name='Повар Тест', position='Повар', phone='+998901110001')
        cls.foreign = Staff.objects.create(school=cls.other, full_name='Чужой Сотрудник')


class StaffDirectoryTests(StaffBase):
    def test_employee_user_gets_a_card(self):
        card = self.employee.staff_profile
        self.assertEqual((card.full_name, card.school, card.phones), ('Нодира', self.school, ['+998930000603']))
        self.employee.school = self.other
        self.employee.save()
        card.refresh_from_db()
        self.assertEqual(card.school, self.other)
        self.assertFalse(Staff.objects.filter(user=self.director).exists())   # директору карточка не создаётся сама

    def test_director_adds_staff_without_login_and_pays(self):
        self.client.force_login(self.director)
        self.client.post(reverse('staff_add'), {'full_name': 'Охранник Тест', 'position': 'Охранник', 'phone': '90 111 00 02',
                                                'is_active': 'on'})
        guard = Staff.objects.get(full_name='Охранник Тест')
        self.assertEqual((guard.school, guard.phone, guard.user), (self.school, '+998901110002', None))
        self.client.post(reverse('payout_create'), {'employee': guard.pk, 'for_month': '2026-10-01', 'amount': '2000000',
                                                    'payout_date': '2026-10-31', 'notes': ''})
        payout = EmployeePayout.objects.get()
        self.assertEqual((payout.employee, payout.school), (guard, self.school))
        body = self.client.get(reverse('payout_list')).content.decode()
        self.assertIn('Охранник Тест', body)
        self.assertIn('Охранник', body)

    def test_scoping(self):
        self.client.force_login(self.director)
        names = [s.full_name for s in self.client.get(reverse('staff_list')).context['staff_list']]
        self.assertIn('Повар Тест', names)
        self.assertNotIn('Чужой Сотрудник', names)
        self.assertEqual(self.client.get(reverse('staff_edit', args=[self.foreign.pk])).status_code, 404)
        form = self.client.get(reverse('payout_create')).context['form']
        self.assertNotIn(self.foreign, form.fields['employee'].queryset)
        self.cook.is_active = False
        self.cook.save()
        form = self.client.get(reverse('payout_create')).context['form']
        self.assertNotIn(self.cook, form.fields['employee'].queryset)
        response = self.client.post(reverse('payout_create'), {'employee': self.foreign.pk, 'for_month': '2026-10-01',
                                                               'amount': '1', 'payout_date': '2026-10-31'})
        self.assertIn('employee', response.context['form'].errors)

    def test_access_and_validation(self):
        for user, code in ((self.employee, 403), (self.investor, 403), (self.owner, 200)):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('staff_list')).status_code, code)
        response = self.client.post(reverse('staff_add'), {'school': self.school.pk, 'full_name': 'X', 'phone': '123'})
        self.assertIn('phone', response.context['form'].errors)

    def test_totals_in_directory(self):
        EmployeePayout.objects.create(employee=self.cook, school=self.school, for_month=date(2026, 9, 1), amount=Decimal(100),
                                      payout_date=date(2026, 9, 30))
        reversed_payout = EmployeePayout.objects.create(employee=self.cook, school=self.school, for_month=date(2026, 10, 1),
                                                        amount=Decimal(999), payout_date=date(2026, 10, 30))
        reversed_payout.reverse(None, 'ошибка')
        self.client.force_login(self.director)
        cook = [s for s in self.client.get(reverse('staff_list')).context['staff_list'] if s.pk == self.cook.pk][0]
        self.assertEqual((cook.paid_total, cook.last_payout), (Decimal(100), date(2026, 9, 30)))


class SalarySummaryTests(StaffBase):
    def test_summary_adds_lump_salary(self):
        salary = Category.objects.get(name='Зарплата')
        EmployeePayout.objects.create(employee=self.cook, school=self.school, for_month=date(2026, 10, 1), amount=Decimal(300))
        EmployeePayout.objects.create(employee=self.employee.staff_profile, school=self.school, for_month=date(2026, 10, 1),
                                      amount=Decimal(200))
        Transaction.objects.create(school=self.school, category=salary, amount=Decimal(1000), date=date(2026, 10, 25))
        Transaction.objects.create(school=self.school, category=Category.objects.get(name='Аренда'), amount=Decimal(7),
                                   date=date(2026, 10, 25))
        EmployeePayout.objects.create(employee=self.foreign, school=self.other, for_month=date(2026, 10, 1), amount=Decimal(5000))
        self.client.force_login(self.director)
        rows = self.client.get(reverse('payout_list') + '?mode=summary').context['summary']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['month'], row['payouts'], row['people'], row['lump'], row['total']),
                         (date(2026, 10, 1), Decimal(500), 2, Decimal(1000), Decimal(1500)))
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('payout_list') + '?mode=summary').context['summary'][0]['total'], Decimal(6500))


class StaffImportTests(StaffBase):
    def test_excel_matches_staff_without_login(self):
        self.client.force_login(self.director)
        rows = [
            {'Сотрудник': 'повар тест', 'За месяц': '10.2026', 'Сумма': 100, 'Дата выплаты': '31.10.2026', 'Счёт': 'Перечисление'},
            {'Сотрудник': '+998 90 111 00 01', 'За месяц': '09.2026', 'Сумма': 100, 'Дата выплаты': '30.09.2026'},
            {'Сотрудник': 'Нодира', 'За месяц': '10.2026', 'Сумма': 50, 'Дата выплаты': '31.10.2026'},
            {'Сотрудник': 'Чужой Сотрудник', 'За месяц': '10.2026', 'Сумма': 50, 'Дата выплаты': '31.10.2026'},
        ]
        headers = ['Сотрудник', 'За месяц', 'Сумма', 'Дата выплаты', 'Счёт']
        response = self.client.post(reverse('excel_import', args=['payouts']), {'file': xlsx(headers, rows)})
        self.assertEqual([r.status for r in response.context['results']], ['ok', 'ok', 'ok', 'error'])
        self.client.post(reverse('excel_import', args=['payouts']), {'action': 'confirm', 'token': response.context['token']})
        self.assertEqual(EmployeePayout.objects.filter(employee=self.cook).count(), 2)
        self.assertEqual(EmployeePayout.objects.get(employee=self.cook, for_month=date(2026, 10, 1)).account.kind, 'BANK')

    def test_onec_links_staff_by_phone_and_pays_by_guid(self):
        link = run_import('employees', [{'external_id': 'FL-7', 'phone': '+998901110001'}])
        self.assertEqual(link.updated, 1, link.errors)
        self.cook.refresh_from_db()
        self.assertEqual(self.cook.external_id, 'FL-7')
        run = run_import('payouts', [{'external_id': 'PO-7', 'employee_external_id': 'FL-7', 'payout_date': '31.10.2026',
                                      'for_month': '2026-10', 'amount': 1000}])
        self.assertEqual(run.created, 1, run.errors)
        self.assertEqual(EmployeePayout.objects.get().school, self.school)
