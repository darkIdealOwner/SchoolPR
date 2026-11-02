"""Сквозные проверки платформы по ролям: что каждая роль может сделать и чего не может.

Данные: две школы (A и B), в каждой директор, сотрудник и договор; владелец и инвестор на всю сеть.
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from users.models import AuditLog, Category, Contract, EmployeePayout, Payment, School, Transaction, User

PASSWORD = 'Strong-pass-2026'


def contract_form_data(**overrides):
    data = {
        'number': 'N-100', 'date': '2026-09-01', 'month': 'Сентябрь',
        'student_full_name': 'Новый Ученик', 'student_birth_year': 2015, 'student_class': '5-А',
        'duration_months': 9, 'parents_full_name': 'Родитель Новый', 'passport_data': 'AB1234567',
        'inps': '12345678901234', 'address': 'Ташкент', 'phone_number': '+998901112233',
        'specialization': 'Русский язык', 'tuition_fee': '1000000', 'discount_percentage': 10,
        'discount_amount': '0', 'food_cost': '200000', 'total_amount': '0', 'notes': '',
    }
    data.update(overrides)
    return data


class RoleTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school_a = School.objects.create(name="Школа А")
        cls.school_b = School.objects.create(name="Школа Б")

        def make(username, phone, role, school=None, name=None):
            return User.objects.create_user(username=username, phone_number=phone, password=PASSWORD,
                                            role=role, school=school, first_name=name or username)

        cls.owner = make('r_owner', '+998930000001', 'OWNER', name='Владелец')
        cls.director_a = make('r_dir_a', '+998930000002', 'DIRECTOR', cls.school_a, 'Директор А')
        cls.director_b = make('r_dir_b', '+998930000003', 'DIRECTOR', cls.school_b, 'Директор Б')
        cls.employee_a = make('r_emp_a', '+998930000004', 'EMPLOYEE', cls.school_a, 'Сотрудник А')
        cls.employee_b = make('r_emp_b', '+998930000005', 'EMPLOYEE', cls.school_b, 'Сотрудник Б')
        cls.investor = make('r_inv', '+998930000006', 'INVESTOR', name='Инвестор')
        cls.school_a.director = cls.director_a
        cls.school_a.save()

        start = timezone.localdate().replace(day=1)
        start = start.replace(year=start.year - 1) if start.month <= 2 else start.replace(month=start.month - 2)
        cls.month_start = start  # договоры начались 2 месяца назад
        cls.contract_a = Contract.objects.create(
            number='A-1', school=cls.school_a, user=cls.employee_a, date=start, duration_months=9,
            student_full_name='Ученик Альфа', parents_full_name='Родитель Альфа', passport_data='AA0000001',
            phone_number='+998901000001', tuition_fee=Decimal(1000000))
        cls.contract_b = Contract.objects.create(
            number='B-1', school=cls.school_b, user=cls.employee_b, date=start, duration_months=9,
            student_full_name='Ученик Бета', parents_full_name='Родитель Бета', passport_data='BB0000002',
            phone_number='+998901000002', tuition_fee=Decimal(2000000))

    def login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def month_str(self, d=None):
        return (d or self.month_start).strftime('%Y-%m')


# ---------------------------------------------------------------- вход и профиль

class AuthFlowTests(RoleTestBase):
    def test_anonymous_is_redirected_to_login_everywhere(self):
        client = Client()
        for name in ['home', 'contract_list', 'payment_list', 'debt_list', 'payout_list',
                     'transaction_list', 'user_list', 'school_list', 'integration', 'profile']:
            response = client.get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn(reverse('login'), response['Location'], name)

    def test_each_role_lands_on_its_own_dashboard(self):
        expected = {
            self.owner: 'Панель владельца',
            self.director_a: 'Панель директора',
            self.employee_a: 'Рабочая панель',
            self.investor: 'Инвестиционный портфель',
        }
        for user, title in expected.items():
            client = Client()
            response = client.post(reverse('login'), {'username': user.phone_number, 'password': PASSWORD}, follow=True)
            self.assertContains(response, title, msg_prefix=user.role)

    def test_login_with_masked_phone(self):
        client = Client()
        response = client.post(reverse('login'), {'username': '+998 (93)-000-00-04', 'password': PASSWORD})
        self.assertRedirects(response, reverse('home'), fetch_redirect_response=False)

    def test_wrong_password_shows_error(self):
        response = Client().post(reverse('login'), {'username': self.owner.phone_number, 'password': 'nope'})
        self.assertContains(response, 'Неверный номер телефона или пароль')

    def test_logout_requires_post(self):
        client = self.login(self.director_a)
        self.assertEqual(client.get(reverse('logout')).status_code, 405)
        response = client.post(reverse('logout'))
        self.assertRedirects(response, reverse('login'), fetch_redirect_response=False)
        self.assertEqual(client.get(reverse('home')).status_code, 302)

    def test_profile_shows_masked_phone_and_role(self):
        response = self.login(self.employee_a).get(reverse('profile'))
        self.assertContains(response, 'Сотрудник')
        self.assertContains(response, '+998(93)-***-**-04')
        self.assertNotContains(response, '+998930000004')

    def test_password_change(self):
        client = self.login(self.employee_a)
        response = client.post(reverse('password_change'), {
            'old_password': PASSWORD, 'new_password1': 'Another-pass-2027', 'new_password2': 'Another-pass-2027'})
        self.assertRedirects(response, reverse('profile'), fetch_redirect_response=False)
        response = Client().post(reverse('login'), {'username': '+998930000004', 'password': 'Another-pass-2027'})
        self.assertEqual(response.status_code, 302)

    def test_header_has_logout_button_and_role_links(self):
        response = self.login(self.director_a).get(reverse('home'))
        self.assertContains(response, 'Выйти')
        self.assertContains(response, reverse('payout_list'))
        response = self.login(self.employee_a).get(reverse('home'))
        self.assertNotContains(response, reverse('payout_list'))


# ---------------------------------------------------------------- владелец

class OwnerTests(RoleTestBase):
    def setUp(self):
        self.client = self.login(self.owner)

    def test_all_pages_open(self):
        urls = [reverse(n) for n in ['home', 'contract_list', 'payment_list', 'debt_list', 'payout_list',
                                     'transaction_list', 'user_list', 'school_list', 'integration',
                                     'contract_add', 'payout_create', 'transaction_create', 'user_add', 'school_add']]
        urls += [reverse('contract_detail', args=[self.contract_b.pk]), reverse('contract_edit', args=[self.contract_a.pk])]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_sees_contracts_of_all_schools(self):
        response = self.client.get(reverse('contract_list'))
        self.assertContains(response, 'Ученик Альфа')
        self.assertContains(response, 'Ученик Бета')
        self.assertContains(response, 'AA0000001')

    def test_creates_school_and_assigns_director(self):
        director = User.objects.create_user(username='r_new_dir', phone_number='+998930000099', password=PASSWORD, role='DIRECTOR')
        response = self.client.post(reverse('school_add'), {'name': 'Школа В', 'address': 'Самарканд', 'director': director.pk})
        self.assertRedirects(response, reverse('school_list'), fetch_redirect_response=False)
        school = School.objects.get(name='Школа В')
        director.refresh_from_db()
        self.assertEqual(director.school, school)
        self.assertTrue(AuditLog.objects.filter(action='CREATE', object_id=str(school.pk)).exists())

    def test_cannot_delete_school_with_contracts_but_can_delete_empty(self):
        response = self.client.post(reverse('school_delete', args=[self.school_b.pk]), follow=True)
        self.assertContains(response, 'Нельзя удалить школу')
        self.assertTrue(School.objects.filter(pk=self.school_b.pk).exists())
        empty = School.objects.create(name='Пустая')
        self.client.post(reverse('school_delete', args=[empty.pk]))
        self.assertFalse(School.objects.filter(pk=empty.pk).exists())

    def test_creates_director_with_school_and_cannot_create_owner(self):
        response = self.client.post(reverse('user_add'), {
            'username': 'r_dir_c', 'first_name': 'Директор В', 'phone_number': '+998 (93)-000-00-50',
            'role': 'DIRECTOR', 'school': self.school_b.pk, 'password': PASSWORD})
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='r_dir_c')
        self.assertEqual((user.role, user.school, user.phone_number), ('DIRECTOR', self.school_b, '+998930000050'))
        self.assertTrue(user.check_password(PASSWORD))

        response = self.client.post(reverse('user_add'), {
            'username': 'r_owner2', 'first_name': 'X', 'phone_number': '+998930000051', 'role': 'OWNER', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='r_owner2').exists())

    def test_edits_user_without_changing_password_when_blank(self):
        response = self.client.post(reverse('user_edit', args=[self.employee_b.pk]), {
            'username': 'r_emp_b', 'first_name': 'Сотрудник Б (переименован)', 'phone_number': '+998930000005',
            'role': 'EMPLOYEE', 'school': self.school_b.pk, 'password': ''})
        self.assertEqual(response.status_code, 302)
        self.employee_b.refresh_from_db()
        self.assertEqual(self.employee_b.first_name, 'Сотрудник Б (переименован)')
        self.assertTrue(self.employee_b.check_password(PASSWORD))

    def test_creates_contract_for_any_school_with_computed_total(self):
        response = self.client.post(reverse('contract_add'), contract_form_data(school=self.school_b.pk))
        self.assertRedirects(response, reverse('contract_list'), fetch_redirect_response=False)
        contract = Contract.objects.get(number='N-100')
        self.assertEqual(contract.school, self.school_b)
        self.assertEqual(contract.user, self.owner)
        # 1 000 000 − 10% + 200 000 питание
        self.assertEqual(contract.total_amount, Decimal('1100000'))

    def test_transaction_for_any_school_and_reversal(self):
        self.client.post(reverse('transaction_create'), {
            'school': self.school_b.pk, 'category': Category.objects.get(name='Вложения учредителей').pk, 'amount': '50000000',
            'date': '2026-10-01', 'notes': 'Вклад'})
        tr = Transaction.objects.get()
        self.assertEqual((tr.school, tr.created_by), (self.school_b, self.owner))
        self.client.post(reverse('transaction_reverse', args=[tr.pk]), {'reason': 'ошибочный ввод'})
        tr.refresh_from_db()
        self.assertTrue(tr.is_reversed)
        self.assertContains(self.client.get(reverse('transaction_list')), 'ошибочный ввод')

    def test_dashboard_shows_network_and_per_school_table(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'По школам')
        self.assertContains(response, 'Вся сеть')
        self.assertContains(response, 'Школа А')
        self.assertContains(response, 'Школа Б')
        self.assertContains(response, 'Интеграция с 1С')

    def test_export_contains_personal_data(self):
        body = self.client.get(reverse('contract_list') + '?export=csv').content.decode('utf-8')
        self.assertIn('Паспорт', body)
        self.assertIn('BB0000002', body)

    def test_deleting_contract_without_payments(self):
        contract = Contract.objects.create(number='DEL-1', school=self.school_a, user=self.owner, date=date(2026, 1, 1))
        self.client.post(reverse('contract_delete', args=[contract.pk]))
        self.assertFalse(Contract.objects.filter(pk=contract.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action='DELETE', object_id=str(contract.pk)).exists())


# ---------------------------------------------------------------- директор

class DirectorTests(RoleTestBase):
    def setUp(self):
        self.client = self.login(self.director_a)

    def test_forbidden_pages(self):
        for name in ['school_list', 'school_add', 'integration']:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_sees_only_own_school_everywhere(self):
        for url in [reverse('contract_list'), reverse('debt_list'), reverse('payment_list') + f'?month={self.month_str()}']:
            response = self.client.get(url)
            self.assertContains(response, 'Ученик Альфа', msg_prefix=url)
            self.assertNotContains(response, 'Ученик Бета', msg_prefix=url)
        self.assertEqual(self.client.get(reverse('contract_detail', args=[self.contract_b.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('contract_edit', args=[self.contract_b.pk])).status_code, 404)

    def test_user_list_only_own_school(self):
        response = self.client.get(reverse('user_list'))
        self.assertContains(response, 'Сотрудник А')
        self.assertNotContains(response, 'Сотрудник Б')

    def test_new_user_is_always_employee_of_own_school(self):
        response = self.client.post(reverse('user_add'), {
            'username': 'r_emp_a2', 'first_name': 'Новый сотрудник', 'phone_number': '+998930000060',
            'role': 'EMPLOYEE', 'password': PASSWORD, 'school': self.school_b.pk})
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='r_emp_a2')
        self.assertEqual((user.role, user.school), ('EMPLOYEE', self.school_a))

        response = self.client.post(reverse('user_add'), {
            'username': 'r_hack', 'first_name': 'X', 'phone_number': '+998930000061', 'role': 'DIRECTOR', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username='r_hack').exists())

    def test_cannot_edit_or_delete_other_school_staff(self):
        self.assertEqual(self.client.get(reverse('user_edit', args=[self.employee_b.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('user_delete', args=[self.employee_b.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('user_delete', args=[self.director_b.pk])).status_code, 404)
        self.assertTrue(User.objects.filter(pk=self.employee_b.pk).exists())

    def test_contract_is_created_in_own_school(self):
        self.client.post(reverse('contract_add'), contract_form_data(school=self.school_b.pk))
        self.assertEqual(Contract.objects.get(number='N-100').school, self.school_a)

    def test_payout_flow_create_edit_reverse(self):
        form_page = self.client.get(reverse('payout_create'))
        self.assertNotContains(form_page, 'Сотрудник Б')
        self.client.post(reverse('payout_create'), {
            'employee': self.employee_a.pk, 'for_month': '2026-10-01', 'amount': '3000000',
            'payout_date': '2026-10-05', 'notes': 'Зарплата'})
        payout = EmployeePayout.objects.get()
        self.assertEqual((payout.school, payout.created_by), (self.school_a, self.director_a))

        self.client.post(reverse('payout_edit', args=[payout.pk]), {
            'employee': self.employee_a.pk, 'for_month': '2026-10-01', 'amount': '3200000',
            'payout_date': '2026-10-05', 'notes': 'Зарплата + премия'})
        payout.refresh_from_db()
        self.assertEqual(payout.amount, Decimal(3200000))
        log = AuditLog.objects.filter(action='UPDATE', object_id=str(payout.pk)).get()
        self.assertIn('amount', log.changes)

        self.client.post(reverse('payout_reverse', args=[payout.pk]), {'reason': 'выплачено дважды'})
        payout.refresh_from_db()
        self.assertTrue(payout.is_reversed)
        self.assertEqual(self.client.get(reverse('payout_edit', args=[payout.pk])).status_code, 404)

    def test_cannot_pay_out_to_other_school_employee(self):
        response = self.client.post(reverse('payout_create'), {
            'employee': self.employee_b.pk, 'for_month': '2026-10-01', 'amount': '100', 'payout_date': '2026-10-05'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(EmployeePayout.objects.exists())

    def test_transactions_scoped_to_own_school(self):
        other = Transaction.objects.create(school=self.school_b, type='EXPENSE', amount=Decimal(10), notes='Чужая трата')
        repair = Category.objects.create(name='Ремонт', direction='EXPENSE')
        self.client.post(reverse('transaction_create'), {
            'category': repair.pk, 'amount': '500000', 'date': '2026-10-02', 'notes': ''})
        mine = Transaction.objects.get(category=repair)
        self.assertEqual(mine.school, self.school_a)
        response = self.client.get(reverse('transaction_list'))
        self.assertContains(response, 'Ремонт')
        self.assertNotContains(response, 'Чужая трата')
        self.assertEqual(self.client.get(reverse('transaction_edit', args=[other.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('transaction_reverse', args=[other.pk]), {'reason': 'x'}).status_code, 404)

    def test_reverses_payment_of_own_school_only(self):
        own = Payment.objects.create(contract=self.contract_a, for_month=self.month_start, amount=Decimal(100))
        other = Payment.objects.create(contract=self.contract_b, for_month=self.month_start, amount=Decimal(100))
        card = reverse('contract_detail', args=[self.contract_a.pk])
        response = self.client.post(reverse('payment_reverse', args=[own.pk]) + f'?next={card}', {'reason': 'возврат'})
        self.assertRedirects(response, card, fetch_redirect_response=False)
        own.refresh_from_db()
        self.assertTrue(own.is_reversed)
        self.assertEqual(self.client.post(reverse('payment_reverse', args=[other.pk]), {'reason': 'x'}).status_code, 404)

    def test_reversal_requires_reason(self):
        payment = Payment.objects.create(contract=self.contract_a, for_month=self.month_start, amount=Decimal(100))
        response = self.client.post(reverse('payment_reverse', args=[payment.pk]), {'reason': ''})
        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertFalse(payment.is_reversed)

    def test_dashboard_is_scoped_and_shows_spending(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Школа А')
        self.assertContains(response, 'Зарплаты и расходы')
        self.assertContains(response, 'Ученик Альфа')
        self.assertNotContains(response, 'Ученик Бета')
        self.assertNotContains(response, 'По школам')

    def test_export_of_debts_only_own_school(self):
        body = self.client.get(reverse('debt_list') + '?export=csv').content.decode('utf-8')
        self.assertIn('Ученик Альфа', body)
        self.assertNotIn('Ученик Бета', body)


# ---------------------------------------------------------------- сотрудник

class EmployeeTests(RoleTestBase):
    def setUp(self):
        self.client = self.login(self.employee_a)

    def test_forbidden_pages(self):
        for name in ['user_list', 'user_add', 'school_list', 'payout_list', 'payout_create',
                     'transaction_list', 'transaction_create', 'integration']:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_allowed_pages(self):
        for url in [reverse('home'), reverse('contract_list'), reverse('contract_add'), reverse('debt_list'),
                    reverse('payment_list'), reverse('contract_detail', args=[self.contract_a.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_creates_and_edits_contract_in_own_school(self):
        self.client.post(reverse('contract_add'), contract_form_data())
        contract = Contract.objects.get(number='N-100')
        self.assertEqual((contract.school, contract.user), (self.school_a, self.employee_a))
        response = self.client.post(reverse('contract_edit', args=[contract.pk]),
                                    contract_form_data(student_full_name='Исправленное Имя'))
        self.assertEqual(response.status_code, 302)
        contract.refresh_from_db()
        self.assertEqual(contract.student_full_name, 'Исправленное Имя')

    def test_duplicate_contract_number_rejected(self):
        response = self.client.post(reverse('contract_add'), contract_form_data(number='A-1'))
        self.assertContains(response, 'Договор с таким номером уже существует.')

    def test_payment_form_prefills_remaining_balance(self):
        Payment.objects.create(contract=self.contract_a, for_month=self.month_start, amount=Decimal(400000))
        response = self.client.get(reverse('payment_create') + f'?contract={self.contract_a.pk}&month={self.month_str()}')
        self.assertContains(response, 'value="600000')

    def test_partial_payment_then_top_up(self):
        url = reverse('payment_create') + f'?contract={self.contract_a.pk}&month={self.month_str()}'
        data = {'contract': self.contract_a.pk, 'for_month': self.month_start.isoformat(),
                'payment_date': timezone.localdate().isoformat(), 'method': 'CASH', 'notes': ''}
        self.client.post(url, {**data, 'amount': '400000'})
        page = self.client.get(reverse('payment_list') + f'?month={self.month_str()}')
        self.assertContains(page, 'Доплатить')
        self.client.post(url, {**data, 'amount': '600000', 'method': 'PAYME'})
        page = self.client.get(reverse('payment_list') + f'?month={self.month_str()}')
        self.assertContains(page, 'Оплачено')
        self.assertEqual(Payment.active.filter(contract=self.contract_a).count(), 2)
        self.assertTrue(all(p.created_by == self.employee_a for p in Payment.objects.all()))

    def test_payment_returns_to_card_when_started_there(self):
        card = reverse('contract_detail', args=[self.contract_a.pk])
        response = self.client.post(
            reverse('payment_create') + f'?contract={self.contract_a.pk}&month={self.month_str()}&next={card}',
            {'contract': self.contract_a.pk, 'for_month': self.month_start.isoformat(),
             'payment_date': timezone.localdate().isoformat(), 'method': 'CARD', 'amount': '1000000'})
        self.assertRedirects(response, card, fetch_redirect_response=False)

    def test_zero_payment_rejected(self):
        response = self.client.post(reverse('payment_create'), {
            'contract': self.contract_a.pk, 'for_month': self.month_start.isoformat(),
            'payment_date': timezone.localdate().isoformat(), 'method': 'CASH', 'amount': '0'})
        self.assertContains(response, 'Сумма оплаты должна быть больше нуля')

    def test_cannot_reverse_payments(self):
        payment = Payment.objects.create(contract=self.contract_a, for_month=self.month_start, amount=Decimal(100))
        self.assertEqual(self.client.post(reverse('payment_reverse', args=[payment.pk]), {'reason': 'x'}).status_code, 403)
        card = self.client.get(reverse('contract_detail', args=[self.contract_a.pk]))
        self.assertNotContains(card, reverse('payment_reverse', args=[payment.pk]))

    def test_cancelling_contract_stops_debt(self):
        debts = self.client.get(reverse('debt_list'))
        self.assertContains(debts, 'Ученик Альфа')
        data = contract_form_data(number='A-1', date=self.month_start.isoformat(), duration_months=9,
                                  student_full_name='Ученик Альфа', tuition_fee='1000000', discount_percentage=0,
                                  food_cost='0', is_cancelled='on', cancelled_at=self.month_start.isoformat())
        self.client.post(reverse('contract_edit', args=[self.contract_a.pk]), data)
        self.contract_a.refresh_from_db()
        self.assertTrue(self.contract_a.is_cancelled)
        self.assertNotContains(self.client.get(reverse('debt_list')), 'Ученик Альфа')
        self.assertContains(self.client.get(reverse('contract_detail', args=[self.contract_a.pk])), 'Отменён')

    def test_contract_with_payments_cannot_be_deleted(self):
        Payment.objects.create(contract=self.contract_a, for_month=self.month_start, amount=Decimal(100))
        response = self.client.post(reverse('contract_delete', args=[self.contract_a.pk]), follow=True)
        self.assertContains(response, 'Нельзя удалить договор')
        self.assertTrue(Contract.objects.filter(pk=self.contract_a.pk).exists())

    def test_card_shows_schedule_with_pay_links(self):
        response = self.client.get(reverse('contract_detail', args=[self.contract_a.pk]))
        self.assertContains(response, 'График оплат')
        self.assertContains(response, 'AA0000001')
        self.assertContains(response, f'month={self.month_str()}')
        self.assertContains(response, 'Долг')

    def test_dashboard_has_no_spending_block(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Собрано оплат')
        self.assertNotContains(response, 'Зарплаты и расходы')
        self.assertNotContains(response, 'Результат месяца')


# ---------------------------------------------------------------- инвестор

class InvestorTests(RoleTestBase):
    def setUp(self):
        self.client = self.login(self.investor)

    def test_cannot_change_anything(self):
        for name in ['contract_add', 'payment_create', 'payout_create', 'transaction_create', 'user_add', 'school_add']:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        payment = Payment.objects.create(contract=self.contract_a, for_month=self.month_start, amount=Decimal(100))
        self.assertEqual(self.client.post(reverse('payment_reverse', args=[payment.pk]), {'reason': 'x'}).status_code, 403)
        self.assertEqual(self.client.post(reverse('contract_delete', args=[self.contract_a.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('contract_edit', args=[self.contract_a.pk])).status_code, 403)

    def test_cannot_open_management_pages(self):
        for name in ['user_list', 'school_list', 'integration']:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_sees_whole_network_without_personal_data(self):
        pages = [reverse('contract_list'), reverse('debt_list'), reverse('payment_list') + f'?month={self.month_str()}',
                 reverse('contract_detail', args=[self.contract_b.pk])]
        for url in pages:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            for secret in ['AA0000001', 'BB0000002', 'Родитель Альфа', 'Родитель Бета', '+998901000001']:
                self.assertNotContains(response, secret, msg_prefix=url)
        response = self.client.get(reverse('contract_list'))
        self.assertContains(response, 'Ученик Альфа')
        self.assertContains(response, 'Ученик Бета')

    def test_exports_without_personal_data(self):
        for name in ['contract_list', 'debt_list']:
            body = self.client.get(reverse(name) + '?export=csv').content.decode('utf-8')
            self.assertNotIn('BB0000002', body, name)
            self.assertNotIn('Родитель', body, name)
            self.assertNotIn('Телефон', body, name)

    def test_cannot_search_by_phone_or_parent(self):
        self.assertNotContains(self.client.get(reverse('contract_list') + '?q=Родитель Альфа'), 'Ученик Альфа')
        self.assertNotContains(self.client.get(reverse('contract_list') + '?q=901000001'), 'Ученик Альфа')
        self.assertContains(self.client.get(reverse('contract_list') + '?q=A-1'), 'Ученик Альфа')

    def test_reads_payouts_and_transactions_without_edit_links(self):
        payout = EmployeePayout.objects.create(employee=self.employee_a, school=self.school_a,
                                               for_month=date(2026, 9, 1), amount=Decimal(100))
        tr = Transaction.objects.create(school=self.school_b, type='EXPENSE', amount=Decimal(50),
                                        category=Category.objects.get(name='Аренда'))
        response = self.client.get(reverse('payout_list'))
        self.assertContains(response, 'Сотрудник А')
        self.assertNotContains(response, reverse('payout_edit', args=[payout.pk]))
        response = self.client.get(reverse('transaction_list'))
        self.assertContains(response, 'Аренда')
        self.assertNotContains(response, reverse('transaction_reverse', args=[tr.pk]))

    def test_dashboard_period_totals_without_debtor_names(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Итоги за 12 месяцев')
        self.assertContains(response, 'Инвестиции и прочие поступления')
        self.assertContains(response, 'По школам')
        self.assertNotContains(response, 'Ученик Альфа')
        self.assertNotContains(response, 'Крупнейшие должники')

    def test_payment_list_has_no_pay_buttons(self):
        response = self.client.get(reverse('payment_list') + f'?month={self.month_str()}')
        self.assertContains(response, 'Не оплачено')
        self.assertNotContains(response, reverse('payment_create'))


# ---------------------------------------------------------------- деньги сквозь роли

class CrossRoleMoneyFlowTests(RoleTestBase):
    def test_employee_payment_is_visible_to_director_owner_and_investor(self):
        employee = self.login(self.employee_a)
        employee.post(reverse('payment_create'), {
            'contract': self.contract_a.pk, 'for_month': self.month_start.isoformat(),
            'payment_date': timezone.localdate().isoformat(), 'method': 'CLICK', 'amount': '1000000'})
        for user in [self.director_a, self.owner, self.investor]:
            response = self.login(user).get(reverse('home'))
            self.assertContains(response, '1 000 000', msg_prefix=user.role)

    def test_director_reversal_brings_debt_back_for_everyone(self):
        payment = Payment.objects.create(contract=self.contract_a, for_month=self.month_start, amount=Decimal(1000000),
                                         created_by=self.employee_a)
        debt_before = self.login(self.owner).get(reverse('contract_detail', args=[self.contract_a.pk])).context['debt']
        self.login(self.director_a).post(reverse('payment_reverse', args=[payment.pk]), {'reason': 'чек не прошёл'})
        card = self.login(self.owner).get(reverse('contract_detail', args=[self.contract_a.pk]))
        self.assertEqual(card.context['debt'], debt_before + Decimal(1000000))
        self.assertContains(card, 'чек не прошёл')
        self.assertContains(card, 'reversed-row')
