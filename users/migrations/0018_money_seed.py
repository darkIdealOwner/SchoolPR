from datetime import date

from django.db import migrations

# (название, узб., англ., кор., раздел, входит в P&L, зарплата, порядок)
INCOME = [
    ('Вложения учредителей', 'Taʼsischilar mablagʻi', 'Founder contributions', '설립자 출자', 'FINANCING', False, False, 20),
    ('Кредит банка (получение)', 'Bank krediti (olish)', 'Bank loan received', '은행 대출 수령', 'FINANCING', False, False, 30),
    ('Возврат на счёт', 'Hisob raqamiga qaytarish', 'Refund to account', '계좌 환급', 'OPERATING', True, False, 40),
    ('Ошибочный платёж', 'Notoʻgʻri toʻlov', 'Erroneous payment', '잘못된 입금', 'OPERATING', False, False, 50),
    ('Продажа основных средств', 'Asosiy vositalarni sotish', 'Sale of fixed assets', '고정자산 매각', 'INVESTING', False, False, 60),
    ('Прочие поступления', 'Boshqa tushumlar', 'Other income', '기타 수입', 'OPERATING', True, False, 900),
]
EXPENSE = [
    ('Зарплата', 'Ish haqi', 'Salaries', '급여', 'OPERATING', True, True, 10),
    ('Налоги', 'Soliqlar', 'Taxes', '세금', 'OPERATING', True, False, 20),
    ('Питание', 'Oziq-ovqat', 'Food', '급식', 'OPERATING', True, False, 30),
    ('Коммунальные услуги', 'Kommunal toʻlovlar', 'Utilities', '공과금', 'OPERATING', True, False, 40),
    ('Аренда', 'Ijara', 'Rent', '임대료', 'OPERATING', True, False, 50),
    ('Хозяйственные расходы', 'Xoʻjalik xarajatlari', 'Household expenses', '운영 소모품', 'OPERATING', True, False, 60),
    ('Товарно-материальные запасы', 'Tovar-moddiy zaxiralar', 'Inventory', '재고 자산', 'OPERATING', True, False, 70),
    ('Питьевая вода', 'Ichimlik suvi', 'Drinking water', '식수', 'OPERATING', True, False, 80),
    ('Связь', 'Aloqa', 'Communications', '통신비', 'OPERATING', True, False, 90),
    ('Охрана', 'Qoʻriqlash', 'Security', '경비', 'OPERATING', True, False, 100),
    ('Вывоз мусора', 'Chiqindi', 'Waste removal', '폐기물 처리', 'OPERATING', True, False, 110),
    ('Услуги банка', 'Bank xizmati', 'Bank fees', '은행 수수료', 'OPERATING', True, False, 120),
    ('Проценты по кредиту', 'Kredit foizi', 'Loan interest', '대출 이자', 'OPERATING', True, False, 130),
    ('Основные средства', 'Asosiy vositalar', 'Fixed assets', '고정자산 구입', 'INVESTING', False, False, 140),
    ('Погашение кредита', 'Kreditni qaytarish', 'Loan repayment', '대출 상환', 'FINANCING', False, False, 150),
    ('Выплаты учредителям', 'Taʼsischilarga toʻlov', 'Payments to founders', '설립자 지급', 'FINANCING', False, False, 160),
    ('Возврат ошибочного платежа', 'Notoʻgʻri toʻlovni qaytarish', 'Return of erroneous payment', '잘못된 입금 반환', 'OPERATING', False, False, 170),
    ('Прочие расходы', 'Boshqa xarajatlar', 'Other expenses', '기타 지출', 'OPERATING', True, False, 900),
]
METHOD_TO_KIND = {'CASH': 'CASH', 'CARD': 'CARD', 'CLICK': 'BANK', 'PAYME': 'BANK', 'UZUM': 'BANK', 'BANK': 'BANK'}


def forwards(apps, schema_editor):
    Category = apps.get_model('users', 'Category')
    Account = apps.get_model('users', 'Account')
    School = apps.get_model('users', 'School')
    Payment = apps.get_model('users', 'Payment')
    EmployeePayout = apps.get_model('users', 'EmployeePayout')
    Transaction = apps.get_model('users', 'Transaction')

    for direction, rows in (('INCOME', INCOME), ('EXPENSE', EXPENSE)):
        for name, uz, en, ko, section, in_pl, is_salary, order in rows:
            Category.objects.get_or_create(name=name, direction=direction, defaults={
                'name_uz': uz, 'name_en': en, 'name_ko': ko, 'section': section, 'in_pl': in_pl,
                'is_salary': is_salary, 'sort_order': order})

    today = date.today().replace(month=1, day=1)
    accounts = {}
    for school in School.objects.all():
        for kind in ('CASH', 'BANK', 'CARD'):
            account, _ = Account.objects.get_or_create(school=school, kind=kind, defaults={'name': '', 'opening_date': today})
            accounts[(school.pk, kind)] = account

    for payment in Payment.objects.filter(account__isnull=True).select_related('contract'):
        account = accounts.get((payment.contract.school_id, METHOD_TO_KIND.get(payment.method, 'CASH')))
        if account:
            payment.account = account
            payment.save(update_fields=['account'])
    for payout in EmployeePayout.objects.filter(account__isnull=True):
        payout.account = accounts.get((payout.school_id, 'CASH'))
        payout.save(update_fields=['account'])

    other = {d: Category.objects.get(name='Прочие поступления' if d == 'INCOME' else 'Прочие расходы', direction=d)
             for d in ('INCOME', 'EXPENSE')}
    for tr in Transaction.objects.all():
        text = (tr.category_text or '').strip()
        category = other[tr.type]
        if text:
            category = Category.objects.filter(name__iexact=text, direction=tr.type).first() or \
                Category.objects.create(name=text[:150], direction=tr.type)
        tr.category = category
        tr.account = tr.account or accounts.get((tr.school_id, 'CASH'))
        tr.save(update_fields=['category', 'account'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0017_money_accounts_categories'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name='transaction', name='category_text'),
    ]
