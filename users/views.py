from django.shortcuts import render
from django.contrib.auth.views import LoginView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from .forms import PhoneAuthenticationForm

class CustomLoginView(LoginView):
    template_name = 'authorization/login.html'
    authentication_form = PhoneAuthenticationForm

class HomeView(LoginRequiredMixin, TemplateView):
    template_name = 'home/home.html'
