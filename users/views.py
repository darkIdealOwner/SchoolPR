from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import models, transaction as db_transaction
from django.db.models import Prefetch, ProtectedError
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import TemplateView, ListView, CreateView, UpdateView, DeleteView, FormView
from django.urls import reverse_lazy
from .forms import (
    PhoneAuthenticationForm, UserManagementForm, SchoolForm, ContractForm, PaymentForm,
    EmployeePayoutForm, TransactionForm, ReversalForm,
)
from .models import User, School, Contract, Payment, EmployeePayout, Transaction, AuditLog, shift_month

class CustomLoginView(LoginView):
    template_name = 'authorization/login.html'
    authentication_form = PhoneAuthenticationForm
    redirect_authenticated_user = True

class HomeView(LoginRequiredMixin, TemplateView):
    def get_template_names(self):
        role = self.request.user.role
        role_templates = {
            'OWNER': 'home/owner.html',
            'DIRECTOR': 'home/director.html',
            'EMPLOYEE': 'home/employee.html',
            'INVESTOR': 'home/investor.html',
        }
        return [role_templates.get(role, 'home/home.html')]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.role == 'OWNER':
            context['total_users'] = User.objects.count()
            context['total_schools'] = School.objects.count()
            context['total_investors'] = User.objects.filter(role='INVESTOR').count()
            context['total_contracts'] = Contract.objects.count()
        elif self.request.user.role == 'DIRECTOR':
            school = self.request.user.school
            context['total_employees'] = User.objects.filter(school=school, role='EMPLOYEE').count()
            context['total_contracts'] = Contract.objects.filter(school=school).count()
        elif self.request.user.role == 'EMPLOYEE':
            school = self.request.user.school
            context['total_contracts'] = Contract.objects.filter(school=school).count()
        elif self.request.user.role == 'INVESTOR':
            context['total_contracts'] = Contract.objects.count()
            context['total_payments'] = Payment.active.count()
        return context

class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = 'authorization/profile.html'

class CustomPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    template_name = 'authorization/password_change.html'
    success_url = reverse_lazy('profile')

# User Management CRUD (Owner Only or Director)
class OwnerOnlyMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.role == 'OWNER'

class OwnerOrDirectorMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.role in ['OWNER', 'DIRECTOR']

class OwnerOrDirectorOrEmployeeMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.role in ['OWNER', 'DIRECTOR', 'EMPLOYEE']

class OwnerOrDirectorOrEmployeeOrInvestorMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.role in ['OWNER', 'DIRECTOR', 'EMPLOYEE', 'INVESTOR']

class OwnerOrDirectorOrInvestorMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.role in ['OWNER', 'DIRECTOR', 'INVESTOR']


# Audit / protection helpers
def _audit_value(value):
    if isinstance(value, models.Model):
        return str(value)
    if isinstance(value, models.QuerySet):
        return [str(v) for v in value]
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    return value

class AuditMixin:
    """Пишет создание/изменение объекта в журнал и проставляет автора денежных записей."""
    audit_exclude = ('password',)

    def form_valid(self, form):
        # CreateView держит self.object = None до сохранения, UpdateView — редактируемый объект
        is_new = getattr(self, 'object', None) is None
        if is_new and hasattr(form.instance, 'created_by_id'):
            form.instance.created_by = self.request.user
        if is_new:
            changes = {name: _audit_value(value) for name, value in form.cleaned_data.items()
                       if name not in self.audit_exclude}
        else:
            changes = {name: [_audit_value(form.initial.get(name)), _audit_value(form.cleaned_data.get(name))]
                       for name in form.changed_data if name not in self.audit_exclude}
        response = super().form_valid(form)
        AuditLog.record(self.request.user, 'CREATE' if is_new else 'UPDATE', self.object, changes)
        return response

class ProtectedDeleteMixin:
    """Удаление с записью в журнал; если на объект ссылаются денежные записи — показываем сообщение вместо ошибки."""
    protected_message = "Нельзя удалить: есть связанные финансовые записи."

    def form_valid(self, form):
        self.object = self.get_object()
        try:
            with db_transaction.atomic():
                AuditLog.record(self.request.user, 'DELETE', self.object)
                self.object.delete()
        except ProtectedError:
            messages.error(self.request, self.protected_message)
            return redirect(self.get_success_url())
        messages.success(self.request, "Запись удалена.")
        return redirect(self.get_success_url())

class BaseReverseView(LoginRequiredMixin, OwnerOrDirectorMixin, FormView):
    """Сторно денежной записи: запись остаётся в базе, но перестаёт учитываться в расчётах."""
    form_class = ReversalForm
    template_name = 'home/finance/reverse_confirm.html'
    model = None
    success_url = None

    def get_queryset(self):
        return self.model.active.all()

    def get_object(self):
        if not hasattr(self, 'object'):
            self.object = get_object_or_404(self.get_queryset(), pk=self.kwargs['pk'])
        return self.object

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['object'] = self.get_object()
        context['object_verbose_name'] = self.model._meta.verbose_name
        context['back_url'] = self.get_success_url()
        return context

    def form_valid(self, form):
        obj = self.get_object()
        reason = form.cleaned_data['reason']
        with db_transaction.atomic():
            obj.reverse(self.request.user, reason)
            AuditLog.record(self.request.user, 'REVERSE', obj, {'reason': reason})
        messages.success(self.request, "Запись сторнирована.")
        return super().form_valid(form)


class UserListView(LoginRequiredMixin, OwnerOrDirectorMixin, ListView):
    model = User
    template_name = 'home/users/user_list.html'
    context_object_name = 'users_list'
    paginate_by = 10

    def get_queryset(self):
        from django.db.models import Q, Case, When, IntegerField
        queryset = super().get_queryset()
        if self.request.user.role == 'DIRECTOR':
            queryset = queryset.filter(school=self.request.user.school)

        # Search filter
        query = self.request.GET.get('q')
        if query:
            queryset = queryset.filter(
                Q(username__icontains=query) |
                Q(first_name__icontains=query) |
                Q(phone_number__icontains=query) |
                Q(school__name__icontains=query)
            )

        # Sorting
        sort = self.request.GET.get('sort', 'priority')
        queryset = queryset.annotate(
            priority=Case(
                When(role='OWNER', then=1),
                When(role='DIRECTOR', then=2),
                When(role='EMPLOYEE', then=3),
                When(role='INVESTOR', then=4),
                default=5,
                output_field=IntegerField(),
            )
        )

        if sort == 'priority':
            queryset = queryset.order_by('priority')
        elif sort == '-priority':
            queryset = queryset.order_by('-priority')

        return queryset

class UserCreateView(LoginRequiredMixin, OwnerOrDirectorMixin, AuditMixin, CreateView):
    model = User
    template_name = 'home/users/user_form.html'
    form_class = UserManagementForm
    success_url = reverse_lazy('user_list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['current_user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        from django.contrib.auth.hashers import make_password
        user = form.save(commit=False)
        if self.request.user.role == 'DIRECTOR':
            user.school = self.request.user.school
            user.role = 'EMPLOYEE'
        if form.cleaned_data['password']:
            user.password = make_password(form.cleaned_data['password'])
        user.save()
        return super().form_valid(form)

class UserUpdateView(LoginRequiredMixin, OwnerOrDirectorMixin, AuditMixin, UpdateView):
    model = User
    template_name = 'home/users/user_form.html'
    form_class = UserManagementForm
    success_url = reverse_lazy('user_list')

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.role == 'DIRECTOR':
            return qs.filter(school=self.request.user.school, role='EMPLOYEE') | qs.filter(id=self.request.user.id)
        return qs

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['current_user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

    def form_valid(self, form):
        from django.contrib.auth.hashers import make_password
        user = form.save(commit=False)
        if self.request.user.role == 'DIRECTOR':
            user.school = self.request.user.school
            user.role = 'EMPLOYEE'
        if form.cleaned_data['password']:
            user.password = make_password(form.cleaned_data['password'])
        user.save()
        return super().form_valid(form)

class UserDeleteView(LoginRequiredMixin, OwnerOrDirectorMixin, ProtectedDeleteMixin, DeleteView):
    model = User
    template_name = 'home/users/user_confirm_delete.html'
    success_url = reverse_lazy('user_list')
    protected_message = "Нельзя удалить пользователя: по нему есть выплаты. Отключите доступ вместо удаления."

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.role == 'DIRECTOR':
            return qs.filter(school=self.request.user.school, role='EMPLOYEE')
        return qs

# Schools Management CRUD (Owner Only)
class SchoolListView(LoginRequiredMixin, OwnerOnlyMixin, ListView):
    model = School
    template_name = 'home/schools/school_list.html'
    context_object_name = 'schools_list'

class SchoolCreateView(LoginRequiredMixin, OwnerOnlyMixin, AuditMixin, CreateView):
    model = School
    template_name = 'home/schools/school_form.html'
    form_class = SchoolForm
    success_url = reverse_lazy('school_list')

class SchoolUpdateView(LoginRequiredMixin, OwnerOnlyMixin, AuditMixin, UpdateView):
    model = School
    template_name = 'home/schools/school_form.html'
    form_class = SchoolForm
    success_url = reverse_lazy('school_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

class SchoolDeleteView(LoginRequiredMixin, OwnerOnlyMixin, ProtectedDeleteMixin, DeleteView):
    model = School
    template_name = 'home/schools/school_confirm_delete.html' # We'll reuse logic but create a separate template or generic one
    success_url = reverse_lazy('school_list')
    protected_message = "Нельзя удалить школу: к ней привязаны договоры, выплаты или транзакции."

# Contract Management CRUD (Owner, Director, Employee or Investor)
class ContractListView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeOrInvestorMixin, ListView):
    model = Contract
    template_name = 'home/contracts/contract_list.html'
    context_object_name = 'contracts_list'
    paginate_by = 10

    def get_queryset(self):
        from django.db.models import Q
        queryset = super().get_queryset().select_related('school')
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            queryset = queryset.filter(school=self.request.user.school)

        # Search filter
        query = self.request.GET.get('q')
        if query:
            search = Q(number__icontains=query) | Q(student_full_name__icontains=query)
            if self.request.user.can_view_personal_data:
                search |= Q(parents_full_name__icontains=query) | Q(phone_number__icontains=query)
            queryset = queryset.filter(search)

        return queryset.order_by('-created_at')

class ContractCreateView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeMixin, AuditMixin, CreateView):
    model = Contract
    template_name = 'home/contracts/contract_form.html'
    form_class = ContractForm
    success_url = reverse_lazy('contract_list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.user = self.request.user
        # Automatically assign school if creator is not OWNER
        if self.request.user.role != 'OWNER':
            form.instance.school = self.request.user.school
        return super().form_valid(form)

class ContractUpdateView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeMixin, AuditMixin, UpdateView):
    model = Contract
    template_name = 'home/contracts/contract_form.html'
    form_class = ContractForm
    success_url = reverse_lazy('contract_list')

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            return qs.filter(school=self.request.user.school)
        return qs

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

class ContractDeleteView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeMixin, ProtectedDeleteMixin, DeleteView):
    model = Contract
    template_name = 'home/contracts/contract_confirm_delete.html'
    success_url = reverse_lazy('contract_list')
    protected_message = "Нельзя удалить договор: по нему есть оплаты. Отметьте договор как отменённый."

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            return qs.filter(school=self.request.user.school)
        return qs

# Payment Management
def parse_month(month_str):
    """'YYYY-MM' -> дата 1-го числа месяца; при ошибке — текущий месяц."""
    try:
        return datetime.strptime(month_str, '%Y-%m').date()
    except (TypeError, ValueError):
        return timezone.localdate().replace(day=1)

def contracts_with_active_payments(queryset):
    return queryset.select_related('school').prefetch_related(
        Prefetch('payments', queryset=Payment.active.order_by('payment_date'))
    )

class PaymentListView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeOrInvestorMixin, ListView):
    model = Contract # We list contracts to show their payment status for a month
    template_name = 'home/payments/payment_list.html'
    context_object_name = 'contracts'

    def get_queryset(self):
        self.selected_month = parse_month(self.request.GET.get('month'))
        qs = super().get_queryset()
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            qs = qs.filter(school=self.request.user.school)
        qs = contracts_with_active_payments(qs.filter(date__lt=shift_month(self.selected_month, 1)).order_by('-date'))
        # Только договоры, по которым в этом месяце есть начисление (учитываем срок и отмену)
        return [c for c in qs if c.is_billable_in(self.selected_month)]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_month = self.selected_month
        context['selected_month'] = selected_month
        context['month_str'] = selected_month.strftime('%Y-%m')

        for contract in context['contracts']:
            month_payments = [p for p in contract.payments.all() if p.for_month.replace(day=1) == selected_month]
            contract.month_payments = month_payments
            contract.this_month_paid = sum((p.amount for p in month_payments), Decimal(0))
            contract.this_month_balance = contract.total_amount - contract.this_month_paid

        return context

# Helper functions for Debt logic
RUSSIAN_MONTHS = {
    1: 'Январь', 2: 'Февраль', 3: 'Март', 4: 'Апрель',
    5: 'Май', 6: 'Июнь', 7: 'Июль', 8: 'Август',
    9: 'Сентябрь', 10: 'Октябрь', 11: 'Ноябрь', 12: 'Декабрь'
}

def format_russian_month(d):
    return f"{RUSSIAN_MONTHS[d.month]} {d.year}"

def get_debt_details(contract, target_date):
    """Долг по договору на месяц target_date включительно и список неоплаченных месяцев.

    Начисления идут только в пределах срока договора и до месяца отмены.
    Сторнированные оплаты не учитываются.
    """
    start_first = contract.first_billing_month
    last_first = contract.last_billing_month
    target_first = target_date.replace(day=1)

    if last_first is None or target_first < start_first:
        return Decimal(0), []
    end_first = min(target_first, last_first)

    payment_map = defaultdict(Decimal)
    for p in contract.payments.all():
        if not p.is_reversed:
            payment_map[p.for_month.replace(day=1)] += p.amount

    unpaid_months = []
    running_balance = Decimal(0)
    monthly_fee = contract.total_amount

    curr = start_first
    while curr <= end_first:
        running_balance += monthly_fee
        running_balance -= payment_map[curr]

        if running_balance > Decimal('0.01'):
            unpaid_months.append(curr)

        curr = shift_month(curr, 1)

    return max(Decimal(0), running_balance), unpaid_months

class DebtListView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeOrInvestorMixin, ListView):
    model = Contract
    template_name = 'home/debts/debt_list.html'
    context_object_name = 'contracts'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Mode: 'general' (all debt till now) or 'monthly' (snapshot for month)
        view_mode = self.request.GET.get('mode', 'general')
        target_month = parse_month(self.request.GET.get('month'))

        context['view_mode'] = view_mode
        context['month_str'] = target_month.strftime('%Y-%m')
        context['target_month_label'] = format_russian_month(target_month)

        # Target month to calculate debt up to
        if view_mode == 'general':
            calc_limit = timezone.localdate().replace(day=1)
        else:
            calc_limit = target_month

        contract_qs = Contract.objects.all()
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            contract_qs = contract_qs.filter(school=self.request.user.school)

        debtors = []
        for contract in contracts_with_active_payments(contract_qs):
            total_debt, unpaid_month_dates = get_debt_details(contract, calc_limit)
            if total_debt > Decimal('0.01'):
                # Format months to readable Russian strings
                unpaid_months_formatted = [format_russian_month(d) for d in unpaid_month_dates]

                contract.total_debt = total_debt
                contract.unpaid_months = unpaid_months_formatted
                contract.unpaid_count = len(unpaid_month_dates)
                debtors.append(contract)

        # Sort debtors by debt amount descending
        debtors.sort(key=lambda c: c.total_debt, reverse=True)
        context['debtors'] = debtors
        return context

class PaymentCreateView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeMixin, AuditMixin, CreateView):
    model = Payment
    form_class = PaymentForm
    template_name = 'home/payments/payment_form.html'

    def get_success_url(self):
        return f"{reverse_lazy('payment_list')}?month={self.object.for_month.strftime('%Y-%m')}"

    def get_allowed_contracts(self):
        qs = Contract.objects.all()
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            qs = qs.filter(school=self.request.user.school)
        return qs

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['contract_queryset'] = self.get_allowed_contracts()
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        contract_id = self.request.GET.get('contract')
        month_str = self.request.GET.get('month') # YYYY-MM
        if contract_id:
            initial['contract'] = contract_id
        if month_str:
            initial['for_month'] = f"{month_str}-01"
        initial['payment_date'] = timezone.localdate()
        # Подставляем остаток к оплате за месяц
        contract = self.get_allowed_contracts().filter(id=contract_id).first() if contract_id else None
        if contract and month_str:
            month_first = parse_month(month_str)
            paid = sum((p.amount for p in Payment.active.filter(contract=contract, for_month=month_first)), Decimal(0))
            initial['amount'] = max(contract.total_amount - paid, Decimal(0))
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contract_id = self.request.GET.get('contract') or self.request.POST.get('contract')
        if contract_id:
            context['contract_obj'] = self.get_allowed_contracts().filter(id=contract_id).first()
        context['selected_month_str'] = self.request.GET.get('month')
        return context

class PaymentReverseView(BaseReverseView):
    model = Payment

    def get_queryset(self):
        qs = super().get_queryset().select_related('contract')
        if self.request.user.role == 'DIRECTOR':
            qs = qs.filter(contract__school=self.request.user.school)
        return qs

    def get_success_url(self):
        return f"{reverse_lazy('payment_list')}?month={self.get_object().for_month.strftime('%Y-%m')}"

# Employee Payouts CRUD Views
class EmployeePayoutListView(LoginRequiredMixin, OwnerOrDirectorOrInvestorMixin, ListView):
    model = EmployeePayout
    template_name = 'home/payouts/payout_list.html'
    context_object_name = 'payouts_list'
    paginate_by = 10

    def get_queryset(self):
        queryset = super().get_queryset().select_related('employee', 'school', 'reversed_by')
        if self.request.user.role == 'DIRECTOR':
            queryset = queryset.filter(school=self.request.user.school)
        return queryset.order_by('-payout_date', '-created_at')

class EmployeePayoutCreateView(LoginRequiredMixin, OwnerOrDirectorMixin, AuditMixin, CreateView):
    model = EmployeePayout
    form_class = EmployeePayoutForm
    template_name = 'home/payouts/payout_form.html'
    success_url = reverse_lazy('payout_list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        if self.request.user.role == 'DIRECTOR':
            form.instance.school = self.request.user.school
        return super().form_valid(form)

class EmployeePayoutUpdateView(LoginRequiredMixin, OwnerOrDirectorMixin, AuditMixin, UpdateView):
    model = EmployeePayout
    form_class = EmployeePayoutForm
    template_name = 'home/payouts/payout_form.html'
    success_url = reverse_lazy('payout_list')

    def get_queryset(self):
        qs = EmployeePayout.active.all()
        if self.request.user.role == 'DIRECTOR':
            return qs.filter(school=self.request.user.school)
        return qs

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

class EmployeePayoutReverseView(BaseReverseView):
    model = EmployeePayout
    success_url = reverse_lazy('payout_list')

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.role == 'DIRECTOR':
            return qs.filter(school=self.request.user.school)
        return qs


# General Transactions CRUD Views
class TransactionListView(LoginRequiredMixin, OwnerOrDirectorOrInvestorMixin, ListView):
    model = Transaction
    template_name = 'home/transactions/transaction_list.html'
    context_object_name = 'transactions_list'
    paginate_by = 10

    def get_queryset(self):
        queryset = super().get_queryset().select_related('school', 'reversed_by')
        if self.request.user.role == 'DIRECTOR':
            queryset = queryset.filter(school=self.request.user.school)
        return queryset.order_by('-date', '-created_at')

class TransactionCreateView(LoginRequiredMixin, OwnerOrDirectorMixin, AuditMixin, CreateView):
    model = Transaction
    form_class = TransactionForm
    template_name = 'home/transactions/transaction_form.html'
    success_url = reverse_lazy('transaction_list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        if self.request.user.role == 'DIRECTOR':
            form.instance.school = self.request.user.school
        return super().form_valid(form)

class TransactionUpdateView(LoginRequiredMixin, OwnerOrDirectorMixin, AuditMixin, UpdateView):
    model = Transaction
    form_class = TransactionForm
    template_name = 'home/transactions/transaction_form.html'
    success_url = reverse_lazy('transaction_list')

    def get_queryset(self):
        qs = Transaction.active.all()
        if self.request.user.role == 'DIRECTOR':
            return qs.filter(school=self.request.user.school)
        return qs

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

class TransactionReverseView(BaseReverseView):
    model = Transaction
    success_url = reverse_lazy('transaction_list')

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.role == 'DIRECTOR':
            return qs.filter(school=self.request.user.school)
        return qs
