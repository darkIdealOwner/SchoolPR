import re
from datetime import datetime

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from .models import (
    User, School, AcademicYear, Contract, Payment, EmployeePayout, Transaction,
    Account, Category, Counterparty, Transfer, UnidentifiedReceipt,
)
from .phone import normalize_phone, is_valid_uz_phone, format_phone
from .templatetags.money import money as format_money
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

PHONE_ATTRS = {
    'class': 'input-field phone-input',
    'placeholder': '+998 (__)-___-__-__',
    'inputmode': 'tel',
    'autocomplete': 'tel',
    'maxlength': '19',
}


class ISODateInput(forms.DateInput):
    # <input type="date"> принимает только YYYY-MM-DD, независимо от языка интерфейса
    input_type = 'date'

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format='%Y-%m-%d')


class MonthInput(forms.DateInput):
    # <input type="month"> отдаёт YYYY-MM
    input_type = 'month'

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format='%Y-%m')


class MonthField(forms.DateField):
    """Месяц: «2026-09» → 1 сентября 2026."""
    widget = MonthInput
    input_formats = ['%Y-%m', '%Y-%m-%d']

    def to_python(self, value):
        value = super().to_python(value)
        return value.replace(day=1) if value else value

class PhoneAuthenticationForm(AuthenticationForm):
    username = forms.CharField(
        label=_("Номер телефона"),
        widget=forms.TextInput(attrs={**PHONE_ATTRS, 'type': 'tel', 'autofocus': True})
    )
    password = forms.CharField(
        label=_("Пароль"),
        widget=forms.PasswordInput(attrs={'class': 'input-field', 'placeholder': '••••••••'})
    )

    def clean_username(self):
        # Номер с маской «+998 (50)-002-21-31» приводим к виду из базы «+998500022131»
        return normalize_phone(self.cleaned_data.get('username'))

class UserManagementForm(forms.ModelForm):
    password = forms.CharField(
        label=_("Пароль"), 
        required=False, 
        widget=forms.PasswordInput(attrs={'placeholder': _('Оставьте пустым, чтобы не менять')})
    )
    
    class Meta:
        model = User
        fields = ['username', 'first_name', 'phone_number', 'role', 'school']

    def __init__(self, *args, **kwargs):
        current_user = kwargs.pop('current_user', None)
        super().__init__(*args, **kwargs)
        # Filter role choices to exclude OWNER for new users
        role_choices = [c for c in User.ROLE_CHOICES if c[0] != 'OWNER']
        
        # If we are editing the existing OWNER, we must keep OWNER in choices to avoid validation error
        if self.instance and self.instance.role == 'OWNER':
            role_choices.insert(0, ('OWNER', dict(User.ROLE_CHOICES)['OWNER']))
            
        self.fields['role'].choices = role_choices
        
        if self.instance.pk:
            self.fields['password'].required = False
            self.fields['role'].disabled = True
        else:
            self.fields['password'].required = True

        # Customization for DIRECTORs
        if current_user and current_user.role == 'DIRECTOR':
            # Hide school field since they can only add to their own school
            if 'school' in self.fields:
                self.fields.pop('school')
            # Directors can only manage EMPLOYEEs
            self.fields['role'].choices = [('EMPLOYEE', dict(User.ROLE_CHOICES)['EMPLOYEE'])]
            if not self.instance.pk:
                self.fields['role'].initial = 'EMPLOYEE'

        for field_name, field in self.fields.items():
            field.widget.attrs.update({'class': 'input-field'})
        self.fields['phone_number'].widget.attrs.update({**PHONE_ATTRS, 'type': 'tel'})
        if self.instance.pk and not self.is_bound:
            self.initial['phone_number'] = format_phone(self.instance.phone_number)

    def clean_phone_number(self):
        phone = normalize_phone(self.cleaned_data.get('phone_number'))
        if not is_valid_uz_phone(phone):
            raise forms.ValidationError(_("Введите номер в формате +998 (XX)-XXX-XX-XX."))
        return phone

class SchoolForm(forms.ModelForm):
    class Meta:
        model = School
        fields = ['name', 'address', 'director']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Filter director choices to only users with role DIRECTOR
        # Also, ideally, those who don't already manage a school, but we'll keep it simple for now
        self.fields['director'].queryset = User.objects.filter(role='DIRECTOR')
        for field_name, field in self.fields.items():
            field.widget.attrs.update({'class': 'input-field'})

    def save(self, commit=True):
        instance = super().save(commit=commit)
        if commit:
            # Sync: Ensure the chosen director points back to this school
            if instance.director:
                director = instance.director
                director.school = instance
                director.save()
        return instance

class ContractForm(forms.ModelForm):
    billing_start = MonthField(
        required=False, label=_("Первый месяц обучения"),
        help_text=_("С него начисляется оплата. Пусто — первый месяц учебного года или месяц договора, если он позже."),
    )

    class Meta:
        model = Contract
        fields = [
            'number', 'school', 'academic_year', 'date', 'billing_start', 'month', 'student_full_name', 'student_birth_year',
            'student_class', 'duration_months', 'parents_full_name', 'passport_data',
            'inps', 'address', 'phone_number', 'phone_number_2', 'specialization', 'tuition_fee',
            'discount_percentage', 'discount_fixed', 'discount_reason', 'discount_amount', 'food_cost', 'total_amount',
            'is_cancelled', 'cancelled_at', 'notes'
        ]
        widgets = {
            'date': ISODateInput(),
            'cancelled_at': ISODateInput(),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if field.widget.__class__ != forms.CheckboxInput:
                field.widget.attrs.update({'class': 'input-field'})
        
        # Hide is_cancelled if creating a new contract
        if not self.instance.pk:
            self.fields.pop('is_cancelled', None)
            self.fields.pop('cancelled_at', None)
        else:
            self.fields['cancelled_at'].help_text=_("С месяца отмены оплата больше не начисляется. Если дата неизвестна — оставьте пустым: начисления остановятся на последнем оплаченном месяце.")
        if not self.instance.pk and not self.is_bound:
            self.initial.setdefault('academic_year', AcademicYear.for_date(timezone.localdate()))
        self.fields['academic_year'].help_text = _("Пусто — определится по дате договора.")

        # Hide school field for non-OWNER users
        if user and user.role != 'OWNER':
            if 'school' in self.fields:
                self.fields.pop('school')

        for name in ('phone_number', 'phone_number_2'):
            self.fields[name].widget.attrs.update({**PHONE_ATTRS, 'type': 'tel'})
            if self.instance.pk and not self.is_bound:
                self.initial[name] = format_phone(getattr(self.instance, name))
        self.fields['passport_data'].widget.attrs['placeholder'] = 'AA1234567'
        self.fields['discount_fixed'].required = False
        self.fields['discount_fixed'].help_text =_("Можно вместе с процентом. Всего скидка не больше стоимости учёбы.")
        for name in ('passport_data', 'inps', 'address'):
            self.fields[name].help_text = _("Можно оставить пустым — договор будет помечен «неполные данные».")

    def _clean_phone(self, name, required):
        value = (self.cleaned_data.get(name) or '').strip()
        if not value:
            if required:
                raise forms.ValidationError(_("Обязательное поле."))
            return ''
        phone = normalize_phone(value)
        if not is_valid_uz_phone(phone):
            raise forms.ValidationError(_("Введите номер в формате +998 (XX)-XXX-XX-XX."))
        return phone

    def clean_phone_number(self):
        return self._clean_phone('phone_number', required=True)

    def clean_phone_number_2(self):
        return self._clean_phone('phone_number_2', required=False)

    def clean_passport_data(self):
        return re.sub(r'\s+', '', self.cleaned_data.get('passport_data') or '').upper()

    def clean_inps(self):
        value = re.sub(r'\s+', '', self.cleaned_data.get('inps') or '')
        if value and not value.isdigit():
            raise forms.ValidationError(_("ИНПС — только цифры."))
        return value

    def clean_student_class(self):
        value = (self.cleaned_data.get('student_class') or '').strip()
        match = re.match(r'(\d+)', value)
        if match and not 0 <= int(match.group(1)) <= 11:
            raise forms.ValidationError(_("Класс — от 0 до 11."))
        return value

    def clean_discount_percentage(self):
        value = self.cleaned_data.get('discount_percentage') or 0
        if not 0 <= value <= 100:
            raise forms.ValidationError(_("Скидка — от 0 до 100 %."))
        return value

    def clean_discount_fixed(self):
        value = self.cleaned_data.get('discount_fixed') or 0
        if value < 0:
            raise forms.ValidationError(_("Скидка не может быть отрицательной."))
        return value

    def clean_number(self):
        number = self.cleaned_data.get('number')
        qs = Contract.objects.filter(number=number)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_("Договор с таким номером уже существует."))
        return number

    def clean(self):
        cleaned = super().clean()
        cancelled_at = cleaned.get('cancelled_at')
        start = cleaned.get('date')
        if cleaned.get('is_cancelled') and cancelled_at and start and cancelled_at < start:
            self.add_error('cancelled_at', _("Дата отмены не может быть раньше даты договора."))
        return cleaned

class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ['amount', 'method', 'payment_date', 'notes', 'contract', 'for_month']
        widgets = {
            'contract': forms.HiddenInput(),
            'for_month': forms.HiddenInput(),
            'payment_date': ISODateInput(),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        contract_queryset = kwargs.pop('contract_queryset', None)
        super().__init__(*args, **kwargs)
        if contract_queryset is not None:
            self.fields['contract'].queryset = contract_queryset
        for field_name, field in self.fields.items():
            field.widget.attrs.update({'class': 'input-field'})

    def clean_for_month(self):
        return self.cleaned_data['for_month'].replace(day=1)

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= 0:
            raise forms.ValidationError(_("Сумма оплаты должна быть больше нуля."))
        return amount

class EmployeePayoutForm(forms.ModelForm):
    class Meta:
        model = EmployeePayout
        fields = ['employee', 'school', 'account', 'for_month', 'amount', 'payout_date', 'notes']
        widgets = {
            'for_month': ISODateInput(),
            'payout_date': ISODateInput(),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs.update({'class': 'input-field'})
        
        # Hide school field for non-OWNER users
        if user and user.role != 'OWNER':
            if 'school' in self.fields:
                self.fields.pop('school')
                
        # Filter employee choices to those in user's school and only role EMPLOYEE
        if user:
            employee_qs = User.objects.filter(role='EMPLOYEE')
            if user.role != 'OWNER':
                employee_qs = employee_qs.filter(school=user.school)
            self.fields['employee'].queryset = employee_qs
        setup_account_field(self, user)

    def clean(self):
        cleaned = super().clean()
        check_account_school(self, cleaned)
        return cleaned


def setup_account_field(form, user):
    """Счёт: только счета своей школы (у владельца — все, с названием школы); пусто — касса школы."""
    field = form.fields['account']
    qs = Account.objects.filter(is_active=True).select_related('school')
    if user and user.role != 'OWNER':
        qs = qs.filter(school=user.school)
        field.label_from_instance = lambda a: a.display_name
    field.queryset = qs
    field.required = False
    field.help_text = _("Пусто — касса школы.")


def check_account_school(form, cleaned):
    account = cleaned.get('account')
    # Директору список счетов уже ограничен его школой; владелец выбирает школу в форме
    school = cleaned.get('school') or (form.instance.school if form.instance.school_id else None)
    if account and school and account.school_id != school.pk:
        form.add_error('account', _("Счёт другой школы."))


class TransactionForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = ['school', 'category', 'account', 'counterparty', 'amount', 'date', 'notes']
        widgets = {
            'date': ISODateInput(),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs.update({'class': 'input-field'})

        # Hide school field for non-OWNER users
        if user and user.role != 'OWNER':
            if 'school' in self.fields:
                self.fields.pop('school')

        category = self.fields['category']
        category.queryset = Category.objects.filter(is_active=True)
        category.required = True
        category.label_from_instance = lambda c: f"{c.get_direction_display()}: {c.display_name}"
        self.fields['counterparty'].required = False
        self.fields['counterparty'].help_text = _("Учредитель, банк, поставщик — если деньги от них или им.")
        self.fields['notes'].label = _("Назначение платежа / примечание")
        setup_account_field(self, user)

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= 0:
            raise forms.ValidationError(_("Сумма должна быть больше нуля."))
        return amount

    def clean(self):
        cleaned = super().clean()
        check_account_school(self, cleaned)
        return cleaned


class TransferForm(forms.ModelForm):
    class Meta:
        model = Transfer
        fields = ['school', 'from_account', 'to_account', 'amount', 'date', 'notes']
        widgets = {
            'date': ISODateInput(),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        qs = Account.objects.filter(is_active=True).select_related('school')
        if self.user and self.user.role != 'OWNER':
            self.fields.pop('school')
            qs = qs.filter(school=self.user.school)
        for name in ('from_account', 'to_account'):
            self.fields[name].queryset = qs
            if self.user and self.user.role != 'OWNER':
                self.fields[name].label_from_instance = lambda a: a.display_name
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'input-field'})

    def clean(self):
        cleaned = super().clean()
        source, target = cleaned.get('from_account'), cleaned.get('to_account')
        school = cleaned.get('school') if 'school' in self.fields else (self.user.school if self.user else None)
        if source and target:
            if source == target:
                self.add_error('to_account', _("Выберите другой счёт."))
            elif source.school_id != target.school_id or (school and source.school_id != school.pk):
                self.add_error('to_account', _("Переводить можно только между счетами одной школы."))
        amount = cleaned.get('amount')
        if amount is not None and amount <= 0:
            self.add_error('amount', _("Сумма должна быть больше нуля."))
        return cleaned


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ['school', 'kind', 'name', 'opening_balance', 'opening_date', 'is_active']
        widgets = {'opening_date': ISODateInput()}

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if user and user.role != 'OWNER':
            self.fields.pop('school')
        if self.instance.pk:
            self.fields.pop('school', None)
        self.fields['opening_date'].help_text = _("С этой даты считается остаток. Движения раньше неё не учитываются.")
        for name, field in self.fields.items():
            if name != 'is_active':
                field.widget.attrs.update({'class': 'input-field'})


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'name_uz', 'name_en', 'name_ko', 'direction', 'section', 'in_pl', 'is_salary', 'sort_order', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['in_pl'].help_text = _("Нет — для вложений и выплат учредителям, кредитов, покупки основных средств.")
        self.fields['is_salary'].help_text = _("Расходы по этой статье считаются зарплатой вместе с выплатами сотрудникам.")
        for name, field in self.fields.items():
            if name not in ('in_pl', 'is_salary', 'is_active'):
                field.widget.attrs.update({'class': 'input-field'})


class CounterpartyForm(forms.ModelForm):
    class Meta:
        model = Counterparty
        fields = ['name', 'name_uz', 'name_en', 'name_ko', 'kind', 'investor', 'notes']
        widgets = {'notes': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['investor'].queryset = User.objects.filter(role='INVESTOR')
        self.fields['investor'].help_text = _("Если этот учредитель заходит на платформу как инвестор.")
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'input-field'})


class ReversalForm(forms.Form):
    reason = forms.CharField(
        label=_("Причина сторно"),
        max_length=255,
        widget=forms.Textarea(attrs={'rows': 3, 'class': 'input-field', 'placeholder': _('Например: ошибка в сумме, оплата внесена дважды')}),
    )


class ExcelUploadForm(forms.Form):
    file = forms.FileField(
        label=_("Файл Excel (.xlsx) или CSV"),
        widget=forms.ClearableFileInput(attrs={'accept': '.xlsx,.xlsm,.csv', 'class': 'input-field'}),
    )


class AcademicYearForm(forms.ModelForm):
    start_month = MonthField(label=_("Первый месяц"))
    end_month = MonthField(label=_("Последний месяц"))

    class Meta:
        model = AcademicYear
        fields = ['name', 'start_month', 'end_month']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].widget.attrs['placeholder'] = '2027–2028'
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'input-field'})

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get('start_month'), cleaned.get('end_month')
        if start and end:
            if end < start:
                self.add_error('end_month', _("Последний месяц раньше первого."))
            overlap = AcademicYear.objects.filter(start_month__lte=end, end_month__gte=start).exclude(pk=self.instance.pk)
            if overlap.exists():
                self.add_error(None, _("Пересекается с учебным годом «%(name)s».") % {'name': overlap.first().name})
        return cleaned


class RefundForm(forms.ModelForm):
    """Возврат родителю: за какой месяц возвращаем и сколько (не больше, чем оплачено за этот месяц)."""
    for_month = forms.TypedChoiceField(label=_("За какой месяц возврат"), coerce=lambda v: datetime.strptime(v, '%Y-%m').date())

    class Meta:
        model = Payment
        fields = ['for_month', 'amount', 'method', 'payment_date', 'notes']
        widgets = {'payment_date': ISODateInput(), 'notes': forms.Textarea(attrs={'rows': 2})}
        labels = {'payment_date': _("Дата возврата"), 'amount': _("Сумма возврата"), 'method': _("Как вернули")}

    def __init__(self, *args, **kwargs):
        self.contract = kwargs.pop('contract')
        super().__init__(*args, **kwargs)
        self.paid = {}
        for p in self.contract.payments.all():
            if not p.is_reversed:
                month = p.for_month.replace(day=1)
                self.paid[month] = self.paid.get(month, 0) + p.signed_amount
        months = sorted((m for m, amount in self.paid.items() if amount > 0), reverse=True)
        self.fields['for_month'].choices = [(m.strftime('%Y-%m'), m.strftime('%m.%Y')) for m in months]
        self.fields['notes'].required = True
        self.fields['notes'].label = _("Причина возврата")
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'input-field'})

    def clean(self):
        cleaned = super().clean()
        month, amount = cleaned.get('for_month'), cleaned.get('amount')
        if amount is not None and amount <= 0:
            self.add_error('amount', _("Сумма должна быть больше нуля."))
        elif month and amount and amount > self.paid.get(month, 0):
            self.add_error('amount', _("За этот месяц оплачено только %(paid)s.") % {'paid': format_money(self.paid.get(month, 0))})
        return cleaned


class ReceiptForm(forms.ModelForm):
    class Meta:
        model = UnidentifiedReceipt
        fields = ['school', 'account', 'date', 'amount', 'payer', 'notes']
        widgets = {'date': ISODateInput(), 'notes': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if user and user.role != 'OWNER':
            self.fields.pop('school')
        setup_account_field(self, user)
        self.fields['account'].required = True
        self.fields['account'].help_text = ''
        self.fields['payer'].help_text = _("Например, текст из выписки: ФИО плательщика, «за сентябрь».")
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'input-field'})

    def clean(self):
        cleaned = super().clean()
        check_account_school(self, cleaned)
        if cleaned.get('amount') is not None and cleaned['amount'] <= 0:
            self.add_error('amount', _("Сумма должна быть больше нуля."))
        return cleaned


class ResolveReceiptForm(forms.Form):
    contract = forms.ModelChoiceField(queryset=Contract.objects.none(), label=_("Договор"))
    for_month = MonthField(label=_("За месяц"))

    def __init__(self, *args, **kwargs):
        receipt = kwargs.pop('receipt')
        super().__init__(*args, **kwargs)
        self.fields['contract'].queryset = Contract.objects.filter(school=receipt.school).order_by('number')
        self.fields['contract'].label_from_instance = lambda c: f"№ {c.number} — {c.student_full_name}"
        self.fields['for_month'].initial = receipt.date.replace(day=1)
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'input-field'})
