"""Строки книги с ошибками: загружаются с красной пометкой «что не так», пометку видно в таблицах и можно снять.

Все данные вымышленные.
"""
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from users.models import AuditLog, Contract, Payment, School, Transaction, Transfer, UnidentifiedReceipt
from users.test_workbook_import import Base, book, contract

D = Decimal


def pay(number, day, amount=1000000, period='сент.26'):
    return {'Дата': day, 'Вид поступление': 'Наличный расчет', '№ договора': number, 'Оплата за период': period,
            'Сумма': amount, 'Краткое назначение': 'Таълим пули'}


def spend(day, amount=50000, category='Бошқалар'):
    return {'Дата': day, 'Вид расходы': 'Наличный расчет', 'Сумма': amount, 'Краткое назначение': category}


BAD_CONTRACTS = [
    contract(11, **{'№ телефона': '9012345'}),        # короткий номер
    contract(12, **{'Класс': 14}),
    contract(13, **{'Год рождения': None}),
    contract(14, **{'Срок, мес': None}),
    contract(15),
]


class FlaggedImportTests(Base):
    def load(self, contracts, income=(), expense=(), user=None):
        return self.confirm(self.upload(user or self.owner, book(list(contracts), list(income), list(expense))))

    def test_preview_lists_flagged_rows_separately(self):
        preview = self.upload(self.owner, book(BAD_CONTRACTS, [], []))
        plan = preview.context['plan']
        self.assertEqual(plan.counts()['contracts']['flagged'], 4)
        self.assertEqual(plan.problems(), [])
        self.assertEqual(len(plan.flagged()), 4)
        self.assertContains(preview, 'Загрузятся с красной пометкой (4)')
        self.assertFalse(Contract.objects.exists())

    def test_bad_phone_is_kept_as_in_book(self):
        self.load(BAD_CONTRACTS)
        c = Contract.objects.get(number='11')
        self.assertEqual(c.phone_number, '9012345')
        self.assertIn('строка', c.data_problem)
        self.assertIn('Телефон', c.data_problem)
        self.assertIn('9012345', c.data_problem)

    def test_bad_class_is_kept_as_in_book(self):
        self.load(BAD_CONTRACTS)
        c = Contract.objects.get(number='12')
        self.assertEqual(c.student_class, '14')
        self.assertIn('Класс', c.data_problem)

    def test_missing_birth_year_stays_empty(self):
        self.load(BAD_CONTRACTS)
        c = Contract.objects.get(number='13')
        self.assertIsNone(c.student_birth_year)
        self.assertIn('Год рождения', c.data_problem)
        self.assertIn('оставлено пустым', c.data_problem)

    def test_missing_duration_runs_to_year_end(self):
        self.load(BAD_CONTRACTS)
        c = Contract.objects.get(number='14')
        last = c.academic_year.end_month
        expected = (last.year - 2026) * 12 + last.month - 9 + 1
        self.assertEqual(c.duration_months, expected)
        self.assertIn(f'{expected} мес.', c.data_problem)
        self.assertEqual(c.last_billing_month, last)

    def test_clean_contract_has_no_mark(self):
        self.load(BAD_CONTRACTS)
        self.assertEqual(Contract.objects.get(number='15').data_problem, '')

    def test_flagged_contract_keeps_money_fields(self):
        self.load(BAD_CONTRACTS)
        c = Contract.objects.get(number='13')
        self.assertEqual((c.tuition_fee, c.food_cost, c.total_amount, c.school), (D(1936000), D(616000), D(2552000), self.school))

    def test_payments_of_flagged_contract_are_loaded(self):
        response = self.load(BAD_CONTRACTS, [pay(14, datetime(2026, 9, 4)), pay(11, datetime(2026, 9, 5))])
        self.assertEqual(response.context['created']['payment'], 2)
        self.assertEqual(Payment.objects.filter(contract__number__in=['11', '14']).count(), 2)
        self.assertEqual(Payment.objects.exclude(data_problem='').count(), 0)
        self.assertTrue(response.context['recon']['ok'])

    def test_result_page_shows_flag_count(self):
        response = self.load(BAD_CONTRACTS)
        self.assertEqual(response.context['created']['flagged'], 4)
        self.assertContains(response, 'Записей с красной пометкой: 4')
        self.assertContains(response, '?status=problem')

    def test_wrong_year_in_payment_date_is_fixed_with_mark(self):
        self.load(BAD_CONTRACTS[4:], [pay(15, datetime(2023, 9, 25))])
        p = Payment.objects.get()
        self.assertEqual(p.payment_date, date(2026, 9, 25))
        self.assertIn('25.09.2023', p.data_problem)
        self.assertIn('25.09.2026', p.data_problem)

    def test_wrong_year_in_expense_is_fixed_with_mark(self):
        self.load([], [], [spend(datetime(2006, 9, 19))])
        t = Transaction.objects.get()
        self.assertEqual(t.date, date(2026, 9, 19))
        self.assertIn('19.09.2006', t.data_problem)

    def test_wrong_year_spring_goes_to_next_calendar_year(self):
        self.load([], [], [spend(datetime(2023, 3, 10))])
        self.assertEqual(Transaction.objects.get().date, date(2027, 3, 10))

    def test_wrong_year_in_transfer_and_receipt(self):
        income = [{'Дата': datetime(2024, 9, 5), 'Вид поступление': 'Перечисление', 'Сумма': 500000,
                   'Назначение платежа': 'без номера', 'Краткое назначение': 'Таълим пули'}]
        expense = [spend(datetime(2025, 10, 1), 400000, 'Ҳисоб рақамни тўлдириш')]
        self.load([], income, expense)
        self.assertIn('05.09.2024', UnidentifiedReceipt.objects.get().data_problem)
        self.assertIn('01.10.2025', Transfer.objects.get().data_problem)

    def test_reconciliation_counts_fixed_dates(self):
        response = self.load(BAD_CONTRACTS[4:], [pay(15, datetime(2023, 9, 25))], [spend(datetime(2006, 9, 19))])
        self.assertTrue(response.context['recon']['ok'], [c for c in response.context['recon']['checks'] if not c['ok']])

    def test_unreadable_date_is_still_an_error(self):
        preview = self.upload(self.owner, book([], [], [spend('когда-то')]))
        self.assertEqual(preview.context['plan'].counts()['expense']['error'], 1)

    def test_missing_student_name_is_still_an_error(self):
        preview = self.upload(self.owner, book([contract(21, **{'ФИО ученика': None})], [], []))
        counts = preview.context['plan'].counts()['contracts']
        self.assertEqual((counts['error'], counts['flagged']), (1, 0))

    def test_missing_fee_is_still_an_error(self):
        preview = self.upload(self.owner, book([contract(22, **{'Стоимость учебы': None})], [], []))
        self.assertEqual(preview.context['plan'].counts()['contracts']['error'], 1)

    def test_error_in_fatal_and_flaggable_column_is_error(self):
        preview = self.upload(self.owner, book([contract(23, **{'ФИО ученика': None, 'Класс': 14})], [], []))
        self.assertEqual(preview.context['plan'].counts()['contracts']['error'], 1)

    def test_payment_for_unloadable_contract_is_error(self):
        preview = self.upload(self.owner, book([contract(24, **{'ФИО ученика': None})], [pay(24, datetime(2026, 9, 4))], []))
        self.assertEqual(preview.context['plan'].counts()['income']['error'], 1)

    def test_director_loads_flagged_contract_into_own_school(self):
        self.load(BAD_CONTRACTS[:1], user=self.director)
        self.assertEqual(Contract.objects.get().school, self.school)

    def test_audit_log_records_problem(self):
        self.load(BAD_CONTRACTS[:1])
        log = AuditLog.objects.get(model_name=Contract._meta.verbose_name, action='CREATE')
        self.assertIn('Телефон', log.changes['data_problem'])


class ReuploadTests(Base):
    def load(self, contracts, income=(), expense=()):
        return self.confirm(self.upload(self.owner, book(list(contracts), list(income), list(expense))))

    def test_same_bad_book_adds_nothing(self):
        self.load(BAD_CONTRACTS, [pay(15, datetime(2023, 9, 25))], [spend(datetime(2006, 9, 19))])
        before = (Contract.objects.count(), Payment.objects.count(), Transaction.objects.count())
        response = self.load(BAD_CONTRACTS, [pay(15, datetime(2023, 9, 25))], [spend(datetime(2006, 9, 19))])
        self.assertEqual(before, (Contract.objects.count(), Payment.objects.count(), Transaction.objects.count()))
        self.assertEqual(dict(response.context['created']), {})
        self.assertEqual(Contract.objects.exclude(data_problem='').count(), 4)
        self.assertEqual(Transaction.objects.exclude(data_problem='').count(), 1)

    def test_still_bad_row_is_reported_as_existing(self):
        self.load(BAD_CONTRACTS[:1])
        preview = self.upload(self.owner, book(BAD_CONTRACTS[:1], [], []))
        item = preview.context['plan'].items['contracts'][0]
        self.assertEqual(item.status, 'exists')
        self.assertIn('ошибка осталась', ' '.join(item.notes))

    def test_corrected_contract_is_updated_and_mark_removed(self):
        self.load(BAD_CONTRACTS)
        fixed = [contract(11), contract(12, **{'Класс': 5}), contract(13, **{'Год рождения': 2016}),
                 contract(14, **{'Срок, мес': 9}), contract(15)]
        preview = self.upload(self.owner, book(fixed, [], []))
        self.assertEqual(preview.context['plan'].counts()['contracts']['fixed'], 4)
        response = self.confirm(preview)
        self.assertEqual(response.context['created']['fixed'], 4)
        self.assertEqual(Contract.objects.count(), 5)
        self.assertFalse(Contract.objects.exclude(data_problem='').exists())
        self.assertEqual(Contract.objects.get(number='11').phone_number, '+998901234567')
        self.assertEqual(Contract.objects.get(number='12').student_class, '5')
        self.assertEqual(Contract.objects.get(number='13').student_birth_year, 2016)
        self.assertEqual(Contract.objects.get(number='14').duration_months, 9)
        self.assertTrue(AuditLog.objects.filter(model_name=Contract._meta.verbose_name, action='UPDATE').exists())

    def test_corrected_contract_keeps_its_payments(self):
        self.load(BAD_CONTRACTS[:1], [pay(11, datetime(2026, 9, 4))])
        c = Contract.objects.get()
        self.load([contract(11)], [pay(11, datetime(2026, 9, 4))])
        self.assertEqual(Contract.objects.get().pk, c.pk)
        self.assertEqual(Payment.objects.count(), 1)

    def test_corrected_cancellation_is_applied(self):
        self.load(BAD_CONTRACTS[:1])
        self.load([contract(11, **{'Отметка об отмены договора': 'Отменено'})])
        c = Contract.objects.get()
        self.assertTrue(c.is_cancelled)
        self.assertEqual(c.data_problem, '')

    def test_clean_contract_is_not_rewritten(self):
        self.load([contract(15)])
        response = self.load([contract(15, **{'Класс': 7})])
        self.assertEqual(Contract.objects.get().student_class, '4')
        self.assertEqual(dict(response.context['created']), {})

    def test_corrected_date_clears_mark_without_duplicate(self):
        self.load(BAD_CONTRACTS[4:], [pay(15, datetime(2023, 9, 25))], [spend(datetime(2006, 9, 19))])
        preview = self.upload(self.owner, book(BAD_CONTRACTS[4:], [pay(15, datetime(2026, 9, 25))], [spend(datetime(2026, 9, 19))]))
        counts = preview.context['plan'].counts()
        self.assertEqual((counts['income']['fixed'], counts['expense']['fixed']), (1, 1))
        response = self.confirm(preview)
        self.assertEqual(response.context['created']['fixed'], 2)
        self.assertEqual((Payment.objects.count(), Transaction.objects.count()), (1, 1))
        self.assertEqual(Payment.objects.get().data_problem, '')
        self.assertEqual(Transaction.objects.get().data_problem, '')

    def test_corrected_row_prefers_clean_twin(self):
        # Две одинаковые траты: одна загружена с пометкой, другая без — исправленная книга снимает пометку только раз
        self.load([], [], [spend(datetime(2006, 9, 19)), spend(datetime(2026, 9, 19))])
        self.load([], [], [spend(datetime(2026, 9, 19)), spend(datetime(2026, 9, 19))])
        self.assertEqual(Transaction.objects.count(), 2)
        self.assertEqual(Transaction.objects.exclude(data_problem='').count(), 0)

    def test_bad_book_after_clean_one_adds_flagged_copy_only_once(self):
        self.load([], [], [spend(datetime(2026, 9, 19))])
        self.load([], [], [spend(datetime(2006, 9, 19))])
        self.assertEqual(Transaction.objects.count(), 1)   # та же запись — не задваивается


class ScreensTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bad = Contract.objects.create(number='B-1', school=cls.school, date=date(2026, 9, 1), tuition_fee=D(1000000),
                                          student_full_name='Тест Ученик Красный', student_birth_year=None,
                                          phone_number='12345', data_problem='строка 9: «Телефон»: неверный — сохранено как в книге')
        cls.good = Contract.objects.create(number='G-1', school=cls.school, date=date(2026, 9, 1), tuition_fee=D(1000000),
                                           student_full_name='Тест Ученик Обычный', phone_number='+998901234567')
        cls.foreign = Contract.objects.create(number='F-1', school=cls.other, date=date(2026, 9, 1), tuition_fee=D(1),
                                              data_problem='чужая школа')
        cls.payment = Payment.objects.create(contract=cls.good, for_month=date(2026, 9, 1), amount=D(1000),
                                             payment_date=date(2026, 9, 25), data_problem='в книге дата 25.09.2023')

    def get(self, user, name, *args, **params):
        self.client.force_login(user)
        return self.client.get(reverse(name, args=args), params)

    def test_contract_list_marks_row_red_with_text(self):
        response = self.get(self.owner, 'contract_list')
        self.assertContains(response, 'class="problem-row"', count=2)
        self.assertContains(response, 'сохранено как в книге')
        self.assertContains(response, 'Договоров с ошибками из книги (выделены красным): 2.')

    def test_director_banner_counts_only_own_school(self):
        self.assertContains(self.get(self.director, 'contract_list'), 'выделены красным): 1.')

    def test_problem_filter(self):
        response = self.get(self.owner, 'contract_list', status='problem')
        numbers = {c.number for c in response.context['contracts_list']}
        self.assertEqual(numbers, {'B-1', 'F-1'})
        self.assertNotContains(response, 'Показать только их')

    def test_empty_birth_year_shows_dash(self):
        response = self.get(self.owner, 'contract_list', status='problem')
        self.assertNotContains(response, '>None<')

    def test_detail_banner(self):
        response = self.get(self.director, 'contract_detail', self.bad.pk)
        self.assertContains(response, 'При загрузке из книги в договоре была ошибка')
        self.assertContains(response, 'снять пометку')
        self.assertNotContains(self.get(self.director, 'contract_detail', self.good.pk), 'При загрузке из книги')

    def test_detail_payment_mark(self):
        response = self.get(self.director, 'contract_detail', self.good.pk)
        self.assertContains(response, 'в книге дата 25.09.2023')

    def test_employee_sees_mark_without_clear_button(self):
        response = self.get(self.employee, 'contract_list')
        self.assertContains(response, 'сохранено как в книге')
        self.assertNotContains(response, reverse('data_problem_clear', args=['contract', self.bad.pk]))

    def test_payment_list_marks(self):
        response = self.get(self.owner, 'payment_list', month='2026-09')
        self.assertContains(response, 'class="problem-row"')
        self.assertContains(response, 'в книге дата 25.09.2023')

    def test_debt_list_marks(self):
        response = self.get(self.owner, 'debt_list', mode='monthly', month='2026-09')
        self.assertContains(response, 'class="problem-row"')

    def test_transaction_list_marks(self):
        Transaction.objects.create(school=self.school, type='EXPENSE', amount=D(5), date=date(2026, 9, 19),
                                   data_problem='в книге дата 19.09.2006')
        response = self.get(self.owner, 'transaction_list')
        self.assertContains(response, 'problem-row')
        self.assertContains(response, '19.09.2006')

    def test_csv_has_problem_column(self):
        response = self.get(self.owner, 'contract_list', export='csv')
        text = response.content.decode('utf-8-sig')
        self.assertIn('Ошибка в книге', text.splitlines()[0])
        self.assertIn('сохранено как в книге', text)


class ClearMarkTests(Base):
    def setUp(self):
        self.bad = Contract.objects.create(number='B-2', school=self.school, date=date(2026, 9, 1), tuition_fee=D(1),
                                           data_problem='что-то не так')
        self.url = reverse('data_problem_clear', args=['contract', self.bad.pk])

    def test_owner_clears_mark(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.url, {'next': reverse('contract_detail', args=[self.bad.pk])})
        self.assertRedirects(response, reverse('contract_detail', args=[self.bad.pk]), fetch_redirect_response=False)
        self.bad.refresh_from_db()
        self.assertEqual(self.bad.data_problem, '')
        log = AuditLog.objects.get(action='UPDATE', model_name=Contract._meta.verbose_name)
        self.assertEqual(log.changes['data_problem'], ['что-то не так', ''])

    def test_director_clears_own_school(self):
        self.client.force_login(self.director)
        self.client.post(self.url)
        self.bad.refresh_from_db()
        self.assertEqual(self.bad.data_problem, '')

    def test_director_cannot_clear_other_school(self):
        other = Contract.objects.create(number='O-2', school=self.other, date=date(2026, 9, 1), tuition_fee=D(1),
                                        data_problem='x')
        self.client.force_login(self.director)
        self.assertEqual(self.client.post(reverse('data_problem_clear', args=['contract', other.pk])).status_code, 404)

    def test_employee_and_investor_cannot_clear(self):
        for user in (self.employee, self.investor):
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.url).status_code, 403)
        self.bad.refresh_from_db()
        self.assertEqual(self.bad.data_problem, 'что-то не так')

    def test_get_is_not_allowed(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_unknown_kind(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse('data_problem_clear', args=['user', self.owner.pk])).status_code, 404)

    def test_external_next_is_ignored(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.url, {'next': 'https://evil.example.com/'})
        self.assertEqual(response.url, reverse('home'))

    def test_clear_payment_and_transaction(self):
        payment = Payment.objects.create(contract=self.bad, for_month=date(2026, 9, 1), amount=D(1), data_problem='дата')
        tx = Transaction.objects.create(school=self.school, type='INCOME', amount=D(1), data_problem='дата')
        self.client.force_login(self.director)
        self.client.post(reverse('data_problem_clear', args=['payment', payment.pk]))
        self.client.post(reverse('data_problem_clear', args=['transaction', tx.pk]))
        payment.refresh_from_db()
        tx.refresh_from_db()
        self.assertEqual((payment.data_problem, tx.data_problem), ('', ''))

    def edit_data(self, **kw):
        data = {'number': 'B-2', 'academic_year': '', 'date': '2026-09-01', 'billing_start': '', 'month': 'Сентябрь',
                'student_full_name': 'Тест Ученик', 'student_birth_year': 2015, 'student_class': '4',
                'duration_months': 9, 'parents_full_name': 'Тест Родитель', 'passport_data': '', 'inps': '', 'address': '',
                'phone_number': '+998901234567', 'phone_number_2': '', 'specialization': 'Русский язык',
                'tuition_fee': 1, 'discount_percentage': 0, 'discount_fixed': 0, 'discount_reason': '',
                'discount_amount': 0, 'food_cost': 0, 'total_amount': 1, 'cancelled_at': '', 'notes': '',
                'school': self.school.pk}
        data.update(kw)
        return data

    def test_edit_form_shows_problem_and_checkbox(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('contract_edit', args=[self.bad.pk]))
        self.assertIn('clear_problem', response.context['form'].fields)
        self.assertContains(response, 'что-то не так')

    def test_edit_without_checkbox_keeps_mark(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('contract_edit', args=[self.bad.pk]), self.edit_data())
        self.bad.refresh_from_db()
        self.assertEqual((self.bad.student_class, self.bad.data_problem), ('4', 'что-то не так'))

    def test_edit_with_checkbox_clears_mark(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('contract_edit', args=[self.bad.pk]), self.edit_data(clear_problem='on'))
        self.bad.refresh_from_db()
        self.assertEqual(self.bad.data_problem, '')

    def test_clean_contract_form_has_no_checkbox(self):
        good = Contract.objects.create(number='G-2', school=self.school, date=date(2026, 9, 1), tuition_fee=D(1))
        self.client.force_login(self.owner)
        response = self.client.get(reverse('contract_edit', args=[good.pk]))
        self.assertNotIn('clear_problem', response.context['form'].fields)

    def test_birth_year_still_required_in_form(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('contract_edit', args=[self.bad.pk]), self.edit_data(student_birth_year=''))
        self.assertIn('student_birth_year', response.context['form'].errors)
