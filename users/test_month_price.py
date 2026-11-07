"""Стоимость отдельного месяца договора вручную: расчёты долгов и отчётов, форма в карточке, загрузка из «Контроля оплаты».

Все данные вымышленные. Договор: 1000 сум в месяц, обучение сентябрь 2026 – июнь 2027 (10 месяцев).
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from users import reports
from users.finance import contract_schedule, contracts_with_active_payments, get_debt_details
from users.models import AcademicYear, AuditLog, Contract, MonthPrice, Payment, School, User
from users.test_workbook_import import Base, book, contract

D = Decimal
PASSWORD = 'Strong-pass-2026'
SEP, OCT, NOV, DEC = date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)


class PriceBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.get(name='2026–2027')
        cls.school = School.objects.create(name='Тест Цена')
        cls.other = School.objects.create(name='Тест Цена Другая')
        make = lambda u, phone, role, school=None: User.objects.create_user(username=u, phone_number=phone, password=PASSWORD,
                                                                              role=role, school=school, first_name=u)
        cls.owner = make('mp_owner', '+998930001101', 'OWNER')
        cls.director = make('mp_dir', '+998930001102', 'DIRECTOR', cls.school)
        cls.employee = make('mp_emp', '+998930001103', 'EMPLOYEE', cls.school)
        cls.other_director = make('mp_dir2', '+998930001104', 'DIRECTOR', cls.other)

    def setUp(self):
        self.c = Contract.objects.create(number='MP-1', school=self.school, academic_year=self.year, date=date(2026, 8, 1),
                                         tuition_fee=D(1000), duration_months=10, student_full_name='Тест Ученик',
                                         phone_number='+998901234567')

    def fresh(self):
        return contracts_with_active_payments(Contract.objects.filter(pk=self.c.pk)).get()

    def price(self, month, amount, **kw):
        return MonthPrice.objects.create(contract=self.c, month=month, amount=D(amount), **kw)


class AccrualTests(PriceBase):
    def test_standard_month(self):
        self.assertEqual(self.fresh().accrual_for(OCT), D(1000))

    def test_override_wins(self):
        self.price(OCT, 300)
        self.assertEqual(self.fresh().accrual_for(OCT), D(300))
        self.assertEqual(self.fresh().accrual_for(NOV), D(1000))

    def test_zero_month(self):
        self.price(OCT, 0)
        self.assertEqual(self.fresh().accrual_for(OCT), D(0))
        self.assertTrue(self.fresh().has_accrual_in(OCT))

    def test_month_outside_term(self):
        self.price(date(2027, 7, 1), 500)
        c = self.fresh()
        self.assertEqual(c.accrual_for(date(2027, 7, 1)), D(500))
        self.assertTrue(c.has_accrual_in(date(2027, 7, 1)))
        self.assertFalse(c.has_accrual_in(date(2027, 8, 1)))

    def test_partial_month_after_cancellation(self):
        Contract.objects.filter(pk=self.c.pk).update(is_cancelled=True, cancelled_at=date(2026, 11, 15))
        self.assertEqual(self.fresh().accrual_for(NOV), D(0))       # месяц отмены обычно не начисляется
        self.price(NOV, 450)
        self.assertEqual(self.fresh().accrual_for(NOV), D(450))

    def test_month_is_saved_as_first_day(self):
        price = self.price(date(2026, 10, 17), 100)
        self.assertEqual(price.month, OCT)

    def test_one_price_per_month(self):
        from django.db import IntegrityError, transaction
        self.price(OCT, 100)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.price(OCT, 200)

    def test_deleted_with_contract(self):
        self.price(OCT, 100)
        self.c.delete()
        self.assertFalse(MonthPrice.objects.exists())


class DebtTests(PriceBase):
    def test_debt_uses_override(self):
        self.price(SEP, 300)
        debt, unpaid = get_debt_details(self.fresh(), OCT)
        self.assertEqual(debt, D(1300))
        self.assertEqual(unpaid, [SEP, OCT])

    def test_paid_partial_month_is_not_debt(self):
        self.price(SEP, 300)
        Payment.objects.create(contract=self.c, for_month=SEP, amount=D(300), payment_date=SEP)
        debt, unpaid = get_debt_details(self.fresh(), SEP)
        self.assertEqual((debt, unpaid), (D(0), []))

    def test_free_month_is_not_debt(self):
        self.price(OCT, 0)
        Payment.objects.create(contract=self.c, for_month=SEP, amount=D(1000), payment_date=SEP)
        self.assertEqual(get_debt_details(self.fresh(), OCT)[0], D(0))

    def test_schedule_shows_override(self):
        self.price(OCT, 616, reason='только питание')
        rows = {r['month']: r for r in contract_schedule(self.fresh(), until=DEC)}
        self.assertEqual((rows[OCT]['accrued'], rows[OCT]['price'].reason), (D(616), 'только питание'))
        self.assertIsNone(rows[SEP]['price'])
        self.assertEqual(len(rows), 10)

    def test_schedule_includes_extra_month(self):
        self.price(date(2027, 7, 1), 200)
        self.assertEqual(len(contract_schedule(self.fresh())), 11)


class ReportTests(PriceBase):
    def test_payment_control(self):
        self.price(OCT, 400)
        data = reports.payment_control(self.year, self.school)
        cells = data['rows'][0]['cells']
        self.assertEqual((cells[0]['accrued'], cells[1]['accrued'], cells[1]['balance']), (D(1000), D(400), D(-1400)))
        self.assertEqual(data['rows'][0]['accrued'], D(9400))

    def test_summary_debts(self):
        self.price(SEP, 250)
        data = reports.contract_summary(self.year, self.school, '2026-09')
        self.assertEqual(data['debts'], D(250))

    def test_debts_report(self):
        self.price(SEP, 250)
        data = reports.debts_for_month(self.year, self.school, '2026-10')
        self.assertEqual(data['rows'][0]['debt'], D(1250))

    def test_pl_reference_accrual(self):
        self.price(OCT, 0)
        months = reports.financial_months(self.year)
        self.assertEqual(reports.tuition_accrued(months, self.school)[OCT], D(0))

    def test_debt_list_page(self):
        self.price(SEP, 250)
        self.client.force_login(self.owner)
        response = self.client.get(reverse('debt_list'), {'mode': 'monthly', 'month': '2026-09'})
        self.assertEqual(response.context['debtors'][0].total_debt, D(250))

    def test_payment_list_month(self):
        self.price(OCT, 400)
        self.client.force_login(self.owner)
        response = self.client.get(reverse('payment_list'), {'month': '2026-10'})
        row = response.context['contracts'][0]
        self.assertEqual((row.this_month_accrued, row.this_month_balance), (D(400), D(400)))
        self.assertContains(response, 'вручную')

    def test_payment_list_extra_month(self):
        self.price(date(2027, 7, 1), 100)
        self.client.force_login(self.owner)
        response = self.client.get(reverse('payment_list'), {'month': '2027-07'})
        self.assertEqual([c.number for c in response.context['contracts']], ['MP-1'])

    def test_payment_form_suggests_override(self):
        self.price(OCT, 400)
        self.client.force_login(self.owner)
        response = self.client.get(reverse('payment_create'), {'contract': self.c.pk, 'month': '2026-10'})
        self.assertEqual(response.context['form'].initial['amount'], D(400))

    def test_home_dashboard_still_works(self):
        self.price(OCT, 400)
        for user in (self.owner, self.director):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('home')).status_code, 200)


class MonthPriceViewTests(PriceBase):
    def url(self, contract=None):
        return reverse('contract_month_price', args=[(contract or self.c).pk])

    def post(self, user, **data):
        self.client.force_login(user)
        return self.client.post(self.url(), data)

    def test_owner_sets_price(self):
        response = self.post(self.owner, month='2026-10', amount='616000', reason='только питание')
        self.assertRedirects(response, reverse('contract_detail', args=[self.c.pk]), fetch_redirect_response=False)
        price = MonthPrice.objects.get()
        self.assertEqual((price.month, price.amount, price.reason, price.source, price.created_by),
                         (OCT, D(616000), 'только питание', 'MANUAL', self.owner))
        self.assertTrue(AuditLog.objects.filter(action='CREATE', model_name=MonthPrice._meta.verbose_name).exists())

    def test_second_save_updates(self):
        self.post(self.owner, month='2026-10', amount='100')
        self.post(self.owner, month='2026-10', amount='200', reason='поправил')
        price = MonthPrice.objects.get()
        self.assertEqual((price.amount, price.reason), (D(200), 'поправил'))
        self.assertTrue(AuditLog.objects.filter(action='UPDATE', model_name=MonthPrice._meta.verbose_name).exists())

    def test_saving_workbook_price_makes_it_manual(self):
        self.price(OCT, 100, source='WORKBOOK')
        self.post(self.owner, month='2026-10', amount='150')
        self.assertEqual(MonthPrice.objects.get().source, 'MANUAL')

    def test_delete(self):
        price = self.price(OCT, 100)
        self.post(self.director, delete=price.pk)
        self.assertFalse(MonthPrice.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action='DELETE', model_name=MonthPrice._meta.verbose_name).exists())

    def test_cannot_delete_other_contract_price(self):
        other = Contract.objects.create(number='MP-2', school=self.school, date=SEP, tuition_fee=D(1))
        price = MonthPrice.objects.create(contract=other, month=OCT, amount=D(1))
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.url(), {'delete': price.pk}).status_code, 404)
        self.assertTrue(MonthPrice.objects.exists())

    def test_negative_amount_rejected(self):
        response = self.post(self.owner, month='2026-10', amount='-5')
        self.assertIn('amount', response.context['form'].errors)
        self.assertFalse(MonthPrice.objects.exists())

    def test_month_required(self):
        response = self.post(self.owner, amount='5')
        self.assertIn('month', response.context['form'].errors)

    def test_access(self):
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.client.force_login(self.other_director)
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.client.force_login(self.director)
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_get_prefills_existing(self):
        self.price(OCT, 321, reason='каникулы')
        self.client.force_login(self.owner)
        form = self.client.get(self.url(), {'month': '2026-10'}).context['form']
        self.assertEqual((form.initial['amount'], form.initial['reason']), (D(321), 'каникулы'))

    def test_get_prefills_standard(self):
        self.client.force_login(self.owner)
        form = self.client.get(self.url(), {'month': '2026-11'}).context['form']
        self.assertEqual(form.initial['amount'], D(1000))

    def test_detail_page_links_and_marks(self):
        self.price(OCT, 616, reason='только питание')
        self.client.force_login(self.director)
        response = self.client.get(reverse('contract_detail', args=[self.c.pk]))
        self.assertContains(response, 'вручную: только питание')
        self.assertContains(response, self.url() + '?month=2026-10')
        self.client.force_login(self.employee)
        self.assertNotContains(self.client.get(reverse('contract_detail', args=[self.c.pk])), self.url())


class WorkbookPriceTests(Base):
    """Лист «Контроль оплаты»: число вместо формулы в «Стоимости учебы» месяца — ручная стоимость месяца."""
    CONTRACTS = [contract(1), contract(2, **{'Отметка об отмены договора': 'Отменено'})]

    def load(self, control, contracts=None):
        return self.confirm(self.upload(self.owner, book(contracts or self.CONTRACTS, [], [], control=control)))

    def test_constant_becomes_month_price(self):
        response = self.load({1: {'окт.26': 616000}})
        price = MonthPrice.objects.get()
        self.assertEqual((price.contract.number, price.month, price.amount, price.source), ('1', OCT, D(616000), 'WORKBOOK'))
        self.assertEqual(response.context['created']['month_prices'], 1)
        self.assertIn('Контроле оплаты', price.reason)

    def test_formulas_are_not_prices(self):
        self.load({1: {}, 2: {}})
        self.assertFalse(MonthPrice.objects.exists())

    def test_constant_equal_to_standard_is_skipped(self):
        self.load({1: {'сент.26': 2552000}})
        self.assertFalse(MonthPrice.objects.exists())

    def test_zero_month(self):
        self.load({1: {'дек.26': 0}})
        self.assertEqual(MonthPrice.objects.get().amount, D(0))

    def test_preview_counts(self):
        preview = self.upload(self.owner, book(self.CONTRACTS, [], [], control={1: {'окт.26': 616000, 'сент.26': 2552000}}))
        self.assertEqual(dict(preview.context['plan'].price_counts), {'create': 1, 'standard': 1})
        self.assertContains(preview, 'Стоимость месяцев из «Контроля оплаты»')
        self.assertFalse(MonthPrice.objects.exists())

    def test_reupload_adds_nothing(self):
        self.load({1: {'окт.26': 616000}})
        response = self.load({1: {'окт.26': 616000}})
        self.assertEqual(MonthPrice.objects.count(), 1)
        self.assertNotIn('month_prices', response.context['created'])

    def test_changed_constant_updates(self):
        self.load({1: {'окт.26': 616000}})
        self.load({1: {'окт.26': 700000}})
        self.assertEqual(MonthPrice.objects.get().amount, D(700000))

    def test_removed_constant_removes_workbook_price(self):
        self.load({1: {'окт.26': 616000}})
        response = self.load({1: {}})
        self.assertFalse(MonthPrice.objects.exists())
        self.assertEqual(response.context['created']['month_prices_removed'], 1)

    def test_manual_price_is_kept(self):
        self.load({1: {}})
        c = Contract.objects.get(number='1')
        MonthPrice.objects.create(contract=c, month=OCT, amount=D(5), source='MANUAL')
        preview = self.upload(self.owner, book(self.CONTRACTS, [], [], control={1: {'окт.26': 616000}}))
        self.assertEqual(preview.context['plan'].price_counts['manual'], 1)
        self.confirm(preview)
        self.assertEqual(MonthPrice.objects.get().amount, D(5))

    def test_unknown_contract(self):
        preview = self.upload(self.owner, book(self.CONTRACTS, [], [], control={77: {'окт.26': 1}}))
        self.assertEqual(preview.context['plan'].price_counts['no_contract'], 1)
        self.confirm(preview)
        self.assertFalse(MonthPrice.objects.exists())

    def test_cancelled_contract_partial_month(self):
        self.load({2: {'сент.26': 348000}})
        self.assertEqual(MonthPrice.objects.get(contract__number='2').amount, D(348000))

    def test_prefix_applies(self):
        self.confirm(self.upload(self.owner, book(self.CONTRACTS, [], [], control={1: {'окт.26': 616000}}), prefix='Z-'))
        self.assertEqual(MonthPrice.objects.get().contract.number, 'Z-1')

    def test_book_without_control_sheet(self):
        response = self.load(None)
        self.assertFalse(MonthPrice.objects.exists())
        self.assertFalse(any(c['label'].startswith('Начислено') for c in response.context['recon']['checks']))

    def test_reconciliation_compares_accruals(self):
        response = self.load({1: {'окт.26': 616000}})
        checks = {c['label']: c for c in response.context['recon']['checks']}
        october = checks['Начислено за 10.2026']
        self.assertEqual(october['book'], D(616000))     # в тестовой книге посчитаны только числа, формулы пустые
        self.assertEqual(october['platform'], D(616000))

    def test_debt_after_import(self):
        self.load({1: {'сент.26': 616000}})
        c = contracts_with_active_payments(Contract.objects.filter(number='1')).get()
        self.assertEqual(get_debt_details(c, SEP)[0], D(616000))
