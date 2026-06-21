from django import forms
from django.contrib.auth.forms import AuthenticationForm

class PhoneAuthenticationForm(AuthenticationForm):
    username = forms.CharField(
        label="Номер телефона",
        widget=forms.TextInput(attrs={'class': 'input-field', 'placeholder': '+7...'})
    )
    password = forms.CharField(
        label="Пароль",
        widget=forms.PasswordInput(attrs={'class': 'input-field', 'placeholder': '••••••••'})
    )
