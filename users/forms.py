from django import forms
from django.contrib.auth.forms import AuthenticationForm
from .models import User, School, Contract, Payment, EmployeePayout, Transaction
from .phone import normalize_phone, is_valid_uz_phone, format_phone
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
    class Meta:
        model = Contract
        fields = [
            'number', 'school', 'date', 'month', 'student_full_name', 'student_birth_year',
            'student_class', 'duration_months', 'parents_full_name', 'passport_data',
            'inps', 'address', 'phone_number', 'specialization', 'tuition_fee',
            'discount_percentage', 'discount_amount', 'food_cost', 'total_amount',
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
            self.fields['cancelled_at'].help_text=_("С месяца отмены оплата больше не начисляется. Если не указать — сегодняшняя дата.")

        # Hide school field for non-OWNER users
        if user and user.role != 'OWNER':
            if 'school' in self.fields:
                self.fields.pop('school')

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
        fields = ['employee', 'school', 'for_month', 'amount', 'payout_date', 'notes']
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

class TransactionForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = ['school', 'type', 'category', 'amount', 'date', 'notes']
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
