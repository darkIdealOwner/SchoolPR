"""Языки интерфейса: переключение, перевод страниц, полнота каталогов."""
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse

from users.models import Contract, School, User

sys.path.insert(0, str(Path(settings.BASE_DIR) / 'scripts'))
import i18n  # noqa: E402

LANG_COOKIE = settings.LANGUAGE_COOKIE_NAME


class LanguageSwitchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="Школа А")
        cls.owner = User.objects.create_user(username='l_owner', phone_number='+998940000001', password='x', role='OWNER', first_name='Ali')
        cls.director = User.objects.create_user(username='l_dir', phone_number='+998940000002', password='x', role='DIRECTOR', school=cls.school)
        cls.employee = User.objects.create_user(username='l_emp', phone_number='+998940000003', password='x', role='EMPLOYEE', school=cls.school)
        cls.investor = User.objects.create_user(username='l_inv', phone_number='+998940000004', password='x', role='INVESTOR')
        cls.contract = Contract.objects.create(number='L-1', school=cls.school, user=cls.owner, date=date(2026, 1, 1),
                                               tuition_fee=Decimal(1000), student_full_name='Ученик Л')

    def client_in(self, user, lang):
        client = Client()
        client.force_login(user)
        client.cookies[LANG_COOKIE] = lang
        return client

    def test_default_is_russian(self):
        client = Client()
        client.force_login(self.owner)
        response = client.get(reverse('home'))
        self.assertContains(response, 'lang="ru"')
        self.assertContains(response, 'Договоры')

    def test_switching_language_sets_cookie_and_redirects_back(self):
        client = Client()
        client.force_login(self.owner)
        response = client.post(reverse('set_language'), {'language': 'en', 'next': reverse('contract_list')})
        self.assertRedirects(response, reverse('contract_list'), fetch_redirect_response=False)
        self.assertEqual(client.cookies[LANG_COOKIE].value, 'en')
        page = client.get(reverse('contract_list'))
        self.assertContains(page, 'lang="en"')
        self.assertContains(page, 'Create contract')
        self.assertNotContains(page, 'Создать договор')

    def test_menu_and_dashboard_translated(self):
        expected = {
            'uz': ['Shartnomalar', 'Qarzlar', 'Chiqish', 'Assalomu alaykum, Ali!'],
            'en': ['Contracts', 'Debts', 'Sign out', 'Hello, Ali!'],
            'ko': ['계약', '미납', '로그아웃', '안녕하세요, Ali님!'],
        }
        for lang, words in expected.items():
            response = self.client_in(self.owner, lang).get(reverse('home'))
            for word in words:
                self.assertContains(response, word, msg_prefix=lang)

    def test_every_role_page_renders_in_every_language(self):
        pages = {
            self.owner: ['home', 'contract_list', 'payment_list', 'debt_list', 'payout_list', 'transaction_list',
                         'user_list', 'school_list', 'integration', 'contract_add', 'user_add', 'profile'],
            self.director: ['home', 'payout_create', 'transaction_create'],
            self.employee: ['home', 'contract_list'],
            self.investor: ['home', 'debt_list'],
        }
        for lang in ['ru', 'uz', 'en', 'ko']:
            for user, names in pages.items():
                client = self.client_in(user, lang)
                for name in names:
                    response = client.get(reverse(name))
                    self.assertEqual(response.status_code, 200, f'{lang} {user.role} {name}')
            card = self.client_in(self.owner, lang).get(reverse('contract_detail', args=[self.contract.pk]))
            self.assertEqual(card.status_code, 200, lang)

    def test_choices_and_messages_translated(self):
        response = self.client_in(self.owner, 'en').get(reverse('payment_create') + f'?contract={self.contract.pk}&month=2026-01')
        self.assertContains(response, 'Cash')
        self.assertContains(response, 'Bank transfer')
        response = self.client_in(self.owner, 'en').post(reverse('school_delete', args=[self.school.pk]), follow=True)
        self.assertContains(response, 'The school cannot be deleted')

    def test_login_page_has_switcher_and_translates(self):
        client = Client()
        client.cookies[LANG_COOKIE] = 'ko'
        response = client.get(reverse('login'))
        self.assertContains(response, 'name="language"')
        self.assertContains(response, '전화번호로 로그인하세요')
        for code, name in settings.LANGUAGES:
            self.assertContains(response, f'value="{code}"')

    def test_csv_export_headers_translated(self):
        body = self.client_in(self.owner, 'en').get(reverse('contract_list') + '?export=csv').content.decode('utf-8')
        self.assertIn('Contract No.', body.splitlines()[0])


class CatalogTests(TestCase):
    def test_catalogs_complete_and_placeholders_match(self):
        for lang in ['uz', 'en', 'ko']:
            entries = i18n.read_po(i18n.po_path(lang))
            self.assertGreater(len(entries), 300, lang)
            missing = [k for k, v in entries.items() if not v]
            self.assertEqual(missing, [], f'{lang}: нет перевода')
            for msgid, msgstr in entries.items():
                self.assertEqual(sorted(re.findall(r'%\(\w+\)s', msgid)), sorted(re.findall(r'%\(\w+\)s', msgstr)),
                                 f'{lang}: {msgid!r}')
            self.assertTrue(i18n.po_path(lang).with_suffix('.mo').exists(), lang)

    def test_catalogs_up_to_date_with_code(self):
        messages = {}
        i18n.extract_templates(messages)
        i18n.extract_python(messages)
        entries = i18n.read_po(i18n.po_path('en'))
        self.assertEqual(sorted(set(messages) - set(entries)), [],
                         'В коде есть новые фразы — запустите python scripts/i18n.py extract и переведите их')
