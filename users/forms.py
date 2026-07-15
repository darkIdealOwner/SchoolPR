from django import forms
from django.contrib.auth.forms import AuthenticationForm
from .models import User, School, Contract, Payment

class PhoneAuthenticationForm(AuthenticationForm):
    username = forms.CharField(
        label="Номер телефона",
        widget=forms.TextInput(attrs={'class': 'input-field', 'placeholder': '+7...'})
    )
    password = forms.CharField(
        label="Пароль",
        widget=forms.PasswordInput(attrs={'class': 'input-field', 'placeholder': '••••••••'})
    )

class UserManagementForm(forms.ModelForm):
    password = forms.CharField(
        label="Пароль", 
        required=False, 
        widget=forms.PasswordInput(attrs={'placeholder': 'Оставьте пустым, чтобы не менять'})
    )
    
    class Meta:
        model = User
        fields = ['username', 'first_name', 'phone_number', 'role', 'school']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Filter role choices to exclude OWNER for new users
        role_choices = [c for c in User.ROLE_CHOICES if c[0] != 'OWNER']
        
        # If we are editing the existing OWNER, we must keep OWNER in choices to avoid validation error
        if self.instance and self.instance.role == 'OWNER':
            role_choices.insert(0, ('OWNER', 'Владелец'))
            
        self.fields['role'].choices = role_choices
        
        if self.instance.pk:
            self.fields['password'].required = False
            self.fields['role'].disabled = True
        else:
            self.fields['password'].required = True

        for field_name, field in self.fields.items():
            field.widget.attrs.update({'class': 'input-field'})

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
            'is_cancelled', 'notes'
        ]
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
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
            if 'is_cancelled' in self.fields:
                self.fields.pop('is_cancelled')

        # Hide school field for non-OWNER users
        if user and user.role != 'OWNER':
            if 'school' in self.fields:
                self.fields.pop('school')

class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ['amount', 'notes', 'contract', 'for_month', 'payment_date']
        widgets = {
            'contract': forms.HiddenInput(),
            'for_month': forms.HiddenInput(),
            'payment_date': forms.HiddenInput(),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs.update({'class': 'input-field'})
