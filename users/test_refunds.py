"""Возвраты родителям и нераспознанные поступления."""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from users.finance import account_balances, build_overview, get_debt_details
from users.models import Account, Contract, Payment, School, UnidentifiedReceipt, User

PASSWORD = 'Strong-pass-2026'


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name='Школа Р')
        cls.other = School.objects.create(name='Школа С')
        make = lambda username, phone, role, school=None: User.objects.create_user(
            username=username, phone_number=phone, password=PASSWORD, role=role, school=school, first_name=username)
        cls.owner = make('r_owner2', '+998930000501', 'OWNER')
        cls.director = make('r_dir2', '+998930000502', 'DIRECTOR', cls.school)
        cls.other_director = make('r_dir3', '+998930000503', 'DIRECTOR', cls.other)
        cls.employee = make('r_emp2', '+998930000504', 'EMPLOYEE', cls.school)
        cls.cash = Account.objects.get(school=cls.school, kind='CASH')
        cls.bank = Account.objects.get(school=cls.school, kind='BANK')

    def setUp(self):
        self.contract = Contract.objects.create(number='R-1', school=self.school, date=date(2026, 9, 1), duration_months=10,
                                                student_full_name='Ученик Р', tuition_fee=Decimal(1000))

    def pay(self, month, amount=1000, kind='PAYMENT', **kwargs):
        return Payment.objects.create(contract=self.contract, for_month=month, amount=Decimal(amount), kind=kind,
                                      payment_date=kwargs.pop('payment_date', month), **kwargs)

    def fresh(self):
        return Contract.objects.get(pk=self.contract.pk)


class RefundRulesTests(Base):
    def test_refund_reduces_paid_and_brings_debt_back(self):
        self.pay(date(2026, 9, 1))
        self.assertEqual(get_debt_details(self.fresh(), date(2026, 9, 1))[0], 0)
        self.pay(date(2026, 9, 1), 400, kind='REFUND', payment_date=date(2026, 9, 20))
        self.assertEqual(get_debt_details(self.fresh(), date(2026, 9, 1))[0], Decimal(400))

    def test_refund_leaves_the_account(self):
        self.pay(date(2026, 9, 1), method='CASH')
        self.pay(date(2026, 9, 1), 300, kind='REFUND', method='CASH', payment_date=date(2026, 9, 2))
        self.assertEqual(account_balances([self.cash], until=date(2026, 9, 30))[self.cash.pk], Decimal(700))

    def test_cancelled_without_date_and_full_refund(self):
        self.contract.is_cancelled = True
        self.contract.save()
        self.pay(date(2026, 9, 1))
        self.pay(date(2026, 10, 1))
        self.pay(date(2026, 10, 1), 1000, kind='REFUND')
        self.assertEqual(self.fresh().last_billing_month, date(2026, 9, 1))
        self.assertEqual(get_debt_details(self.fresh(), date(2027, 1, 1))[0], 0)

    def test_overview_counts_refund_as_minus(self):
        self.pay(date(2026, 10, 1), 1000, payment_date=date(2026, 10, 3))
        self.pay(date(2026, 10, 1), 250, kind='REFUND', payment_date=date(2026, 10, 9))
        overview = build_overview(school=self.school, today=date(2026, 10, 20))
        self.assertEqual(overview['current']['collected'], Decimal(750))

    def test_reversed_refund_is_ignored(self):
        self.pay(date(2026, 9, 1))
        refund = self.pay(date(2026, 9, 1), 1000, kind='REFUND')
        refund.reverse(self.owner, 'ошибка')
        self.assertEqual(get_debt_details(self.fresh(), date(2026, 9, 1))[0], 0)


class RefundPageTests(Base):
    def url(self):
        return reverse('refund_create', args=[self.contract.pk])

    def test_director_records_refund(self):
        self.pay(date(2026, 9, 1))
        self.pay(date(2026, 10, 1), 500)
        self.client.force_login(self.director)
        detail = self.client.get(reverse('contract_detail', args=[self.contract.pk]))
        self.assertContains(detail, self.url())
        form = self.client.get(self.url()).context['form']
        self.assertEqual([v for v, _ in form.fields['for_month'].choices], ['2026-10', '2026-09'])
        response = self.client.post(self.url(), {'for_month': '2026-09', 'amount': '600', 'method': 'CASH',
                                                 'payment_date': '2026-09-25', 'notes': 'ученик ушёл'})
        self.assertRedirects(response, reverse('contract_detail', args=[self.contract.pk]))
        refund = Payment.objects.get(kind='REFUND')
        self.assertEqual((refund.amount, refund.for_month, refund.created_by), (Decimal(600), date(2026, 9, 1), self.director))
        self.assertContains(self.client.get(reverse('contract_detail', args=[self.contract.pk])), 'возврат')

    def test_validation(self):
        self.pay(date(2026, 9, 1), 500)
        self.client.force_login(self.owner)
        too_much = self.client.post(self.url(), {'for_month': '2026-09', 'amount': '600', 'method': 'CASH',
                                                 'payment_date': '2026-09-25', 'notes': 'x'})
        self.assertIn('amount', too_much.context['form'].errors)
        no_reason = self.client.post(self.url(), {'for_month': '2026-09', 'amount': '100', 'method': 'CASH',
                                                  'payment_date': '2026-09-25', 'notes': ''})
        self.assertIn('notes', no_reason.context['form'].errors)
        unpaid_month = self.client.post(self.url(), {'for_month': '2026-11', 'amount': '100', 'method': 'CASH',
                                                     'payment_date': '2026-09-25', 'notes': 'x'})
        self.assertIn('for_month', unpaid_month.context['form'].errors)
        self.assertFalse(Payment.objects.filter(kind='REFUND').exists())

    def test_access(self):
        self.pay(date(2026, 9, 1))
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.client.force_login(self.other_director)
        self.assertEqual(self.client.get(self.url()).status_code, 404)


class ReceiptTests(Base):
    def test_full_flow(self):
        self.client.force_login(self.employee)
        self.client.post(reverse('receipt_create'), {'account': self.bank.pk, 'date': '2026-10-05', 'amount': '1000',
                                                     'payer': 'Иванов, за октябрь', 'notes': ''})
        receipt = UnidentifiedReceipt.objects.get()
        self.assertEqual((receipt.school, receipt.created_by), (self.school, self.employee))
        self.assertEqual(account_balances([self.bank], until=date(2026, 10, 31))[self.bank.pk], Decimal(1000))
        self.assertContains(self.client.get(reverse('payment_list') + '?month=2026-10'), '(1)')

        response = self.client.post(reverse('receipt_resolve', args=[receipt.pk]), {'contract': self.contract.pk, 'for_month': '2026-10'})
        self.assertRedirects(response, reverse('receipt_list'))
        receipt.refresh_from_db()
        payment = receipt.payment
        self.assertEqual((payment.contract, payment.account, payment.payment_date, payment.method, payment.for_month),
                         (self.contract, self.bank, date(2026, 10, 5), 'BANK', date(2026, 10, 1)))
        # Деньги на счёте не задвоились
        self.assertEqual(account_balances([self.bank], until=date(2026, 10, 31))[self.bank.pk], Decimal(1000))
        self.assertEqual(get_debt_details(self.fresh(), date(2026, 10, 1))[0], Decimal(1000))   # сентябрь не оплачен
        self.assertEqual(list(self.client.get(reverse('receipt_list')).context['receipts']), [])
        self.assertEqual(list(self.client.get(reverse('receipt_list') + '?status=resolved').context['receipts']), [receipt])
        self.assertEqual(self.client.get(reverse('receipt_resolve', args=[receipt.pk])).status_code, 404)

    def test_scoping_and_reversal(self):
        receipt = UnidentifiedReceipt.objects.create(school=self.school, account=self.cash, amount=Decimal(50), date=date(2026, 10, 1))
        self.client.force_login(self.other_director)
        self.assertEqual(self.client.get(reverse('receipt_resolve', args=[receipt.pk])).status_code, 404)
        self.assertEqual(list(self.client.get(reverse('receipt_list')).context['receipts']), [])
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('receipt_reverse', args=[receipt.pk])).status_code, 403)
        self.client.force_login(self.director)
        self.client.post(reverse('receipt_reverse', args=[receipt.pk]), {'reason': 'дубль'})
        receipt.refresh_from_db()
        self.assertTrue(receipt.is_reversed)
        self.assertEqual(account_balances([self.cash], until=date(2026, 10, 31))[self.cash.pk], 0)

    def test_contract_of_other_school_cannot_be_chosen(self):
        foreign = Contract.objects.create(number='S-9', school=self.other, date=date(2026, 9, 1), tuition_fee=Decimal(1))
        receipt = UnidentifiedReceipt.objects.create(school=self.school, account=self.cash, amount=Decimal(50), date=date(2026, 10, 1))
        self.client.force_login(self.owner)
        response = self.client.post(reverse('receipt_resolve', args=[receipt.pk]), {'contract': foreign.pk, 'for_month': '2026-10'})
        self.assertIn('contract', response.context['form'].errors)
        self.assertFalse(Payment.objects.exists())

    def test_api_marks_refunds(self):
        from django.test import override_settings
        self.pay(date(2026, 9, 1))
        self.pay(date(2026, 9, 1), 100, kind='REFUND')
        with override_settings(ONEC_API_TOKEN='t0ken'):
            data = self.client.get('/api/1c/payments/', HTTP_AUTHORIZATION='Token t0ken').json()
        self.assertEqual(sorted(r['kind'] for r in data['results']), ['PAYMENT', 'REFUND'])
