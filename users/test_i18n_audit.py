"""Аудит переводов: каждая страница каждой роли на узбекском, английском и корейском не должна содержать русского текста.

Все тестовые данные — латиницей, поэтому любая кириллица в ответе — это непереведённый интерфейс.
Не проверяются: значения атрибутов value (служебные данные форм) и название «Русский» в переключателе языка.
"""
import re
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from integrations.models import SyncRun
from users.models import Contract, EmployeePayout, Payment, School, Transaction, User

LANG_COOKIE = settings.LANGUAGE_COOKIE_NAME
LANGS = ['uz', 'en', 'ko']
CYR = re.compile(r'[А-Яа-яЁё]+')


def visible_text(html):
    """Ответ без того, что пользователь не видит или что является данными, а не интерфейсом."""
    html = re.sub(r'<style.*?</style>', ' ', html, flags=re.S)
    html = re.sub(r'<script[^>]*>(.*?)</script>',
                  lambda m: ' ' + re.sub(r'/\*.*?\*/|//[^\n]*', ' ', m.group(1), flags=re.S) + ' ', html, flags=re.S)
    html = re.sub(r'<!--.*?-->', ' ', html, flags=re.S)
    html = re.sub(r'<option value="ru"[^>]*>\s*Русский\s*</option>', ' ', html)
    html = re.sub(r'\bvalue="[^"]*"', ' ', html)
    return html


def cyrillic_snippets(html):
    text = visible_text(html)
    out = []
    for m in CYR.finditer(text):
        start, end = max(0, m.start() - 40), min(len(text), m.end() + 40)
        out.append(' '.join(text[start:end].split()))
    return out


class I18nAuditBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Пользователи из seed-миграции названы по-русски — это данные, а не интерфейс
        User.objects.all().delete()
        cls.school = School.objects.create(name='Alpha School', address='Main street 1')
        cls.empty_school = School.objects.create(name='Empty School')

        def make(username, phone, role, school=None, name='Tester'):
            return User.objects.create_user(username=username, phone_number=phone, password='Pass-12345-x',
                                            role=role, school=school, first_name=name)

        cls.owner = make('a_owner', '+998950000001', 'OWNER', name='Owner')
        cls.director = make('a_dir', '+998950000002', 'DIRECTOR', cls.school, 'Director')
        cls.employee = make('a_emp', '+998950000003', 'EMPLOYEE', cls.school, 'Employee')
        cls.investor = make('a_inv', '+998950000004', 'INVESTOR', name='Investor')
        cls.spare = make('a_spare', '+998950000005', 'EMPLOYEE', cls.school, 'Spare')
        cls.school.director = cls.director
        cls.school.save()

        today = timezone.localdate()
        start = (today.replace(day=1) - timedelta(days=70)).replace(day=1)
        cls.month = start
        cls.contract = Contract.objects.create(
            number='C-1', school=cls.school, user=cls.employee, date=start, month='Январь', duration_months=9,
            student_full_name='John Smith', student_class='5A', parents_full_name='Jane Smith', passport_data='AA1234567',
            inps='12345678901234', address='Main street 2', phone_number='+998901112233', specialization='Узбекский язык',
            tuition_fee=Decimal(1000000), food_cost=Decimal(100000), notes='second child')
        cls.cancelled = Contract.objects.create(
            number='C-2', school=cls.school, user=cls.employee, date=start, month='Март', student_full_name='Kim Lee',
            parents_full_name='Park Lee', is_cancelled=True, cancelled_at=today, tuition_fee=Decimal(500000))
        cls.payment = Payment.objects.create(contract=cls.contract, for_month=start, amount=Decimal(400000), method='CLICK',
                                             created_by=cls.employee, notes='part')
        reversed_payment = Payment.objects.create(contract=cls.contract, for_month=start, amount=Decimal(100), method='CASH')
        reversed_payment.reverse(cls.director, 'typo')
        Payment.objects.create(contract=cls.contract, for_month=start, amount=Decimal(50000), method='BANK',
                               source='1C', external_id='X-1')
        cls.payout = EmployeePayout.objects.create(employee=cls.employee, school=cls.school, for_month=start,
                                                   amount=Decimal(3000000), notes='salary', created_by=cls.director)
        cls.transaction = Transaction.objects.create(school=cls.school, type='EXPENSE', category='Rent',
                                                     amount=Decimal(800000), notes='October')
        Transaction.objects.create(school=cls.school, type='INCOME', category='Investment', amount=Decimal(5000000))
        SyncRun.objects.create(entity='payments', channel='file', status='SUCCESS', total=1, created=1,
                               triggered_by=cls.owner, finished_at=timezone.now())

    def get(self, user, lang, url):
        client = Client()
        if user:
            client.force_login(user)
        client.cookies[LANG_COOKIE] = lang
        return client.get(url)


def _owner_pages(t):
    c, p, po, tr = t.contract.pk, t.payment.pk, t.payout.pk, t.transaction.pk
    m = t.month.strftime('%Y-%m')
    return {
        'home': reverse('home'),
        'contract_list': reverse('contract_list'),
        'contract_add': reverse('contract_add'),
        'contract_edit': reverse('contract_edit', args=[c]),
        'contract_edit_cancelled': reverse('contract_edit', args=[t.cancelled.pk]),
        'contract_detail': reverse('contract_detail', args=[c]),
        'contract_delete': reverse('contract_delete', args=[t.cancelled.pk]),
        'payment_list': reverse('payment_list') + f'?month={m}',
        'payment_create': reverse('payment_create') + f'?contract={c}&month={m}',
        'payment_reverse': reverse('payment_reverse', args=[p]),
        'debt_list': reverse('debt_list'),
        'debt_list_monthly': reverse('debt_list') + f'?mode=monthly&month={m}',
        'payout_list': reverse('payout_list'),
        'payout_create': reverse('payout_create'),
        'payout_edit': reverse('payout_edit', args=[po]),
        'transaction_list': reverse('transaction_list'),
        'transaction_create': reverse('transaction_create'),
        'transaction_edit': reverse('transaction_edit', args=[tr]),
        'user_list': reverse('user_list'),
        'user_add': reverse('user_add'),
        'user_edit': reverse('user_edit', args=[t.employee.pk]),
        'user_delete': reverse('user_delete', args=[t.spare.pk]),
        'school_list': reverse('school_list'),
        'school_add': reverse('school_add'),
        'school_edit': reverse('school_edit', args=[t.school.pk]),
        'school_delete': reverse('school_delete', args=[t.empty_school.pk]),
        'integration': reverse('integration'),
        'profile': reverse('profile'),
        'password_change': reverse('password_change'),
    }


OWNER_PAGES = ['home', 'contract_list', 'contract_add', 'contract_edit', 'contract_edit_cancelled', 'contract_detail',
               'contract_delete', 'payment_list', 'payment_create', 'payment_reverse', 'debt_list', 'debt_list_monthly',
               'payout_list', 'payout_create', 'payout_edit', 'transaction_list', 'transaction_create', 'transaction_edit',
               'user_list', 'user_add', 'user_edit', 'user_delete', 'school_list', 'school_add', 'school_edit',
               'school_delete', 'integration', 'profile', 'password_change']
ROLE_PAGES = {
    'director_home': ('director', 'home'),
    'employee_home': ('employee', 'home'),
    'investor_home': ('investor', 'home'),
    'login': (None, 'login'),
}


class PageTranslationAudit(I18nAuditBase):
    """По одному тесту на страницу и язык — 99 проверок."""

    def check_page(self, user, lang, url):
        response = self.get(user, lang, url)
        self.assertEqual(response.status_code, 200, url)
        snippets = cyrillic_snippets(response.content.decode('utf-8'))
        self.assertEqual(snippets, [], f'[{lang}] {url}: непереведённый текст')


def _make_owner_test(page, lang):
    def test(self):
        self.check_page(self.owner, lang, _owner_pages(self)[page])
    return test


def _make_role_test(role, name, lang):
    def test(self):
        user = getattr(self, role) if role else None
        self.check_page(user, lang, reverse(name))
    return test


for _lang in LANGS:
    for _page in OWNER_PAGES:
        setattr(PageTranslationAudit, f'test_{_lang}_owner_{_page}', _make_owner_test(_page, _lang))
    for _key, (_role, _name) in ROLE_PAGES.items():
        setattr(PageTranslationAudit, f'test_{_lang}_{_key}', _make_role_test(_role, _name, _lang))


class I18nBehaviourTests(I18nAuditBase):
    def test_switcher_shows_each_language_in_its_own_name(self):
        for lang in ['ru'] + LANGS:
            html = self.get(self.owner, lang, reverse('home')).content.decode('utf-8')
            for code, name in settings.LANGUAGES:
                self.assertRegex(html, rf'<option value="{code}"[^>]*>{re.escape(name)}</option>', f'{lang}: {code}')

    def test_current_language_is_selected(self):
        for lang in LANGS:
            html = self.get(self.owner, lang, reverse('home')).content.decode('utf-8')
            self.assertRegex(html, rf'<option value="{lang}" selected>')

    def test_unknown_language_is_ignored(self):
        client = Client()
        client.force_login(self.owner)
        client.post(reverse('set_language'), {'language': 'xx', 'next': '/home/'})
        self.assertContains(client.get(reverse('home')), 'lang="ru"')

    def test_set_language_does_not_redirect_to_other_sites(self):
        response = Client().post(reverse('set_language'), {'language': 'en', 'next': 'https://evil.example/'})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('evil.example', response['Location'])

    def test_language_survives_logout_and_login_page(self):
        client = Client()
        client.force_login(self.owner)
        client.post(reverse('set_language'), {'language': 'ko', 'next': '/home/'})
        client.post(reverse('logout'))
        self.assertContains(client.get(reverse('login')), '로그인')

    def test_form_errors_translated(self):
        client = Client()
        client.force_login(self.employee)
        client.cookies[LANG_COOKIE] = 'en'
        response = client.post(reverse('payment_create'), {
            'contract': self.contract.pk, 'for_month': self.month.isoformat(),
            'payment_date': timezone.localdate().isoformat(), 'method': 'CASH', 'amount': '0'})
        self.assertContains(response, 'The payment amount must be greater than zero.')
        self.assertEqual(cyrillic_snippets(response.content.decode('utf-8')), [])

    def test_django_builtin_errors_translated(self):
        # В узбекском каталоге Django этих строк нет — их закрывает locale/uz/LC_MESSAGES/overrides.po
        for lang in LANGS:
            client = Client()
            client.force_login(self.owner)
            client.cookies[LANG_COOKIE] = lang
            response = client.post(reverse('password_change'), {'old_password': 'wrong', 'new_password1': '1', 'new_password2': '2'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(cyrillic_snippets(response.content.decode('utf-8')), [], lang)

    def test_duplicate_user_errors_translated(self):
        for lang in LANGS:
            client = Client()
            client.force_login(self.owner)
            client.cookies[LANG_COOKIE] = lang
            response = client.post(reverse('user_add'), {
                'username': 'a_emp', 'first_name': 'Dup', 'phone_number': '+998950000003', 'role': 'EMPLOYEE',
                'school': self.school.pk, 'password': 'Pass-12345-x'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(cyrillic_snippets(response.content.decode('utf-8')), [], lang)

    def test_success_message_translated(self):
        client = Client()
        client.force_login(self.director)
        client.cookies[LANG_COOKIE] = 'uz'
        response = client.post(reverse('transaction_reverse', args=[self.transaction.pk]), {'reason': 'dup'}, follow=True)
        self.assertContains(response, 'Yozuv storno qilindi.')

    def test_csv_exports_have_no_russian_headers(self):
        for lang in LANGS:
            for name in ['contract_list', 'debt_list', 'payout_list', 'transaction_list']:
                body = self.get(self.owner, lang, reverse(name) + '?export=csv').content.decode('utf-8')
                self.assertEqual(CYR.findall(body.splitlines()[0]), [], f'{lang} {name}')

    def test_korean_dates_use_year_month_order(self):
        html = self.get(self.owner, 'ko', reverse('contract_detail', args=[self.contract.pk])).content.decode('utf-8')
        self.assertIn(f'{self.month.year}년 {self.month.month}월', html)
