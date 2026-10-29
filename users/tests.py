from django.test import TestCase
from django.db import IntegrityError
from users.models import User, Contract, School, EmployeePayout, Transaction
from users.forms import ContractForm, EmployeePayoutForm, TransactionForm

class ContractDuplicateTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            phone_number="+998901234567",
            password="testpassword",
            role="EMPLOYEE"
        )

    def test_duplicate_contract_number_raises_integrity_error(self):
        # Create first contract
        Contract.objects.create(
            number="C-001",
            user=self.user
        )
        
        # Creating a second contract with the same number should raise IntegrityError
        with self.assertRaises(IntegrityError):
            Contract.objects.create(
                number="C-001",
                user=self.user
            )

    def test_contract_form_validation_prevents_duplicate_number(self):
        # Create first contract
        Contract.objects.create(
            number="C-001",
            user=self.user
        )
        
        # Test creating a duplicate via form
        form_data = {
            'number': 'C-001',
            'student_full_name': 'Test Student',
            'student_birth_year': 2010,
            'student_class': '5-A',
            'date': '2026-08-12',
            'month': 'Август',
            'duration_months': 12,
            'parents_full_name': 'Parent Name',
            'passport_data': 'AA1234567',
            'inps': '12345678901234',
            'address': 'Test Address',
            'phone_number': '+998901112233',
            'specialization': 'Русский язык',
            'tuition_fee': 1000000,
            'discount_percentage': 0,
            'discount_amount': 0,
            'food_cost': 200000,
            'total_amount': 1200000,
        }
        form = ContractForm(data=form_data, user=self.user)
        self.assertFalse(form.is_valid())
        self.assertIn('number', form.errors)
        self.assertEqual(form.errors['number'][0], "Договор с таким номером уже существует.")

        # Test editing same contract is allowed (should not trigger validation error for its own number)
        contract = Contract.objects.get(number="C-001")
        form = ContractForm(instance=contract, data=form_data, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)


class FinanceTestCase(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Test School")
        self.director = User.objects.create_user(
            username="test_director",
            phone_number="+998901111110",
            password="dirpassword",
            role="DIRECTOR",
            school=self.school
        )
        self.employee = User.objects.create_user(
            username="test_employee",
            phone_number="+998902222220",
            password="emppassword",
            role="EMPLOYEE",
            school=self.school
        )
        self.owner = User.objects.create_user(
            username="test_owner",
            phone_number="+998903333330",
            password="ownerpassword",
            role="OWNER"
        )

    def test_employee_payout_creation_and_form_filtering(self):
        # Director form should filter employees to their school
        form = EmployeePayoutForm(user=self.director)
        self.assertIn(self.employee, form.fields['employee'].queryset)
        
        # Test creating payout
        payout = EmployeePayout.objects.create(
            employee=self.employee,
            school=self.school,
            for_month="2026-08-01",
            amount=1500000,
            notes="Salary"
        )
        self.assertEqual(payout.amount, 1500000)

    def test_other_transaction_creation(self):
        # Create an income (investment) transaction
        tr_income = Transaction.objects.create(
            school=self.school,
            type="INCOME",
            category="Инвестиции",
            amount=50000000,
            notes="Инвестиция от Олега"
        )
        self.assertEqual(tr_income.type, "INCOME")
        self.assertEqual(float(tr_income.amount), 50000000.0)

        # Create an expense transaction
        tr_expense = Transaction.objects.create(
            school=self.school,
            type="EXPENSE",
            category="Траты",
            amount=1200000,
            notes="Непредвиденный ремонт замка"
        )
        self.assertEqual(tr_expense.type, "EXPENSE")


from datetime import date
from decimal import Decimal
from django.test import Client
from django.urls import reverse
from users.models import Payment, AuditLog
from users.views import get_debt_details


class DebtCalculationTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="emp", phone_number="+998900000001", password="x", role="EMPLOYEE")

    def make_contract(self, **kwargs):
        defaults = dict(number="D-1", user=self.user, date=date(2026, 1, 15), duration_months=3, tuition_fee=Decimal(100))
        defaults.update(kwargs)
        return Contract.objects.create(**defaults)

    def debt(self, contract, target):
        return get_debt_details(Contract.objects.get(pk=contract.pk), target)

    def test_debt_stops_after_contract_duration(self):
        contract = self.make_contract()
        total, months = self.debt(contract, date(2026, 10, 1))
        self.assertEqual(total, Decimal(300))
        self.assertEqual(months, [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)])

    def test_cancelled_contract_stops_accruing_from_cancellation_month(self):
        contract = self.make_contract(duration_months=12, is_cancelled=True, cancelled_at=date(2026, 3, 10))
        total, months = self.debt(contract, date(2026, 10, 1))
        self.assertEqual(total, Decimal(200))
        self.assertEqual(len(months), 2)

    def test_cancelled_in_first_month_has_no_debt(self):
        contract = self.make_contract(is_cancelled=True, cancelled_at=date(2026, 1, 20))
        self.assertEqual(self.debt(contract, date(2026, 10, 1)), (Decimal(0), []))

    def test_cancel_sets_date_automatically(self):
        contract = self.make_contract()
        contract.is_cancelled = True
        contract.save()
        self.assertIsNotNone(contract.cancelled_at)

    def test_reversed_payment_is_not_counted(self):
        contract = self.make_contract()
        payment = Payment.objects.create(contract=contract, for_month=date(2026, 1, 1), amount=Decimal(100))
        self.assertEqual(self.debt(contract, date(2026, 1, 1))[0], Decimal(0))
        payment.reverse(self.user, "ошибка")
        self.assertEqual(self.debt(contract, date(2026, 1, 1))[0], Decimal(100))


class FinanceViewsTestCase(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="School")
        self.owner = User.objects.create_user(username="t_owner", phone_number="+998900000010", password="x", role="OWNER")
        self.director = User.objects.create_user(username="t_dir", phone_number="+998900000011", password="x", role="DIRECTOR", school=self.school)
        self.employee = User.objects.create_user(username="t_emp", phone_number="+998900000012", password="x", role="EMPLOYEE", school=self.school)
        self.investor = User.objects.create_user(username="t_inv", phone_number="+998900000013", password="x", role="INVESTOR")
        self.contract = Contract.objects.create(
            number="F-1", user=self.employee, school=self.school, date=date(2026, 1, 1),
            tuition_fee=Decimal(1000), passport_data="AB7654321", parents_full_name="Родитель Тестовый",
        )

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_investor_does_not_see_personal_data(self):
        for url in [reverse('contract_list'), reverse('payment_list') + '?month=2026-01', reverse('debt_list')]:
            response = self.client_for(self.investor).get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "AB7654321")
            self.assertNotContains(response, "Родитель Тестовый")
        response = self.client_for(self.director).get(reverse('contract_list'))
        self.assertContains(response, "AB7654321")

    def test_payment_create_records_author_method_and_audit(self):
        response = self.client_for(self.employee).post(reverse('payment_create'), {
            'contract': self.contract.pk, 'for_month': '2026-01-15', 'payment_date': '2026-01-20',
            'amount': '1000', 'method': 'CLICK', 'notes': '',
        })
        self.assertEqual(response.status_code, 302)
        payment = Payment.objects.get()
        self.assertEqual(payment.created_by, self.employee)
        self.assertEqual(payment.method, 'CLICK')
        self.assertEqual(payment.for_month, date(2026, 1, 1))
        self.assertTrue(AuditLog.objects.filter(action='CREATE', object_id=str(payment.pk)).exists())

    def test_employee_cannot_pay_other_school_contract(self):
        other = School.objects.create(name="Other")
        contract = Contract.objects.create(number="F-2", user=self.owner, school=other, date=date(2026, 1, 1))
        response = self.client_for(self.employee).post(reverse('payment_create'), {
            'contract': contract.pk, 'for_month': '2026-01-01', 'payment_date': '2026-01-20',
            'amount': '1000', 'method': 'CASH',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.objects.exists())

    def test_reverse_transaction_keeps_record(self):
        tr = Transaction.objects.create(school=self.school, type='EXPENSE', amount=Decimal(500))
        response = self.client_for(self.director).post(reverse('transaction_reverse', args=[tr.pk]), {'reason': 'дубль'})
        self.assertEqual(response.status_code, 302)
        tr.refresh_from_db()
        self.assertTrue(tr.is_reversed)
        self.assertEqual(tr.reversed_by, self.director)
        self.assertEqual(tr.reversal_reason, 'дубль')
        self.assertEqual(Transaction.active.count(), 0)
        # Сторнированную запись нельзя редактировать
        response = self.client_for(self.director).get(reverse('transaction_edit', args=[tr.pk]))
        self.assertEqual(response.status_code, 404)

    def test_employee_cannot_reverse(self):
        payment = Payment.objects.create(contract=self.contract, for_month=date(2026, 1, 1), amount=Decimal(100))
        response = self.client_for(self.employee).post(reverse('payment_reverse', args=[payment.pk]), {'reason': 'x'})
        self.assertEqual(response.status_code, 403)

    def test_contract_with_payments_cannot_be_deleted(self):
        Payment.objects.create(contract=self.contract, for_month=date(2026, 1, 1), amount=Decimal(100))
        response = self.client_for(self.director).post(reverse('contract_delete', args=[self.contract.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Contract.objects.filter(pk=self.contract.pk).exists())

    def test_deleting_employee_keeps_their_contracts(self):
        self.client_for(self.director).post(reverse('user_delete', args=[self.employee.pk]))
        self.assertFalse(User.objects.filter(pk=self.employee.pk).exists())
        self.contract.refresh_from_db()
        self.assertIsNone(self.contract.user)

    def test_payment_list_hides_expired_contracts(self):
        response = self.client_for(self.owner).get(reverse('payment_list') + '?month=2027-06')
        self.assertNotContains(response, "F-1")
        response = self.client_for(self.owner).get(reverse('payment_list') + '?month=2026-06')
        self.assertContains(response, "F-1")
