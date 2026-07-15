from django.contrib.auth.views import LoginView, PasswordChangeView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import TemplateView, ListView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy
from .forms import PhoneAuthenticationForm, UserManagementForm, SchoolForm, ContractForm, PaymentForm
from .models import User, School, Contract, Payment

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
        return context

class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = 'authorization/profile.html'

class CustomPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    template_name = 'authorization/password_change.html'
    success_url = reverse_lazy('profile')

# User Management CRUD (Owner Only)
class OwnerOnlyMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.role == 'OWNER'

class UserListView(LoginRequiredMixin, OwnerOnlyMixin, ListView):
    model = User
    template_name = 'home/users/user_list.html'
    context_object_name = 'users_list'
    paginate_by = 10

    def get_queryset(self):
        from django.db.models import Q, Case, When, IntegerField
        queryset = super().get_queryset()
        
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

class UserCreateView(LoginRequiredMixin, OwnerOnlyMixin, CreateView):
    model = User
    template_name = 'home/users/user_form.html'
    form_class = UserManagementForm
    success_url = reverse_lazy('user_list')

    def form_valid(self, form):
        from django.contrib.auth.hashers import make_password
        user = form.save(commit=False)
        if form.cleaned_data['password']:
            user.password = make_password(form.cleaned_data['password'])
        user.save()
        return super().form_valid(form)

class UserUpdateView(LoginRequiredMixin, OwnerOnlyMixin, UpdateView):
    model = User
    template_name = 'home/users/user_form.html'
    form_class = UserManagementForm
    success_url = reverse_lazy('user_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

    def form_valid(self, form):
        from django.contrib.auth.hashers import make_password
        user = form.save(commit=False)
        if form.cleaned_data['password']:
            user.password = make_password(form.cleaned_data['password'])
        user.save()
        return super().form_valid(form)

class UserDeleteView(LoginRequiredMixin, OwnerOnlyMixin, DeleteView):
    model = User
    template_name = 'home/users/user_confirm_delete.html'
    success_url = reverse_lazy('user_list')

# Schools Management CRUD (Owner Only)
class SchoolListView(LoginRequiredMixin, OwnerOnlyMixin, ListView):
    model = School
    template_name = 'home/schools/school_list.html'
    context_object_name = 'schools_list'

class SchoolCreateView(LoginRequiredMixin, OwnerOnlyMixin, CreateView):
    model = School
    template_name = 'home/schools/school_form.html'
    form_class = SchoolForm
    success_url = reverse_lazy('school_list')

class SchoolUpdateView(LoginRequiredMixin, OwnerOnlyMixin, UpdateView):
    model = School
    template_name = 'home/schools/school_form.html'
    form_class = SchoolForm
    success_url = reverse_lazy('school_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

class SchoolDeleteView(LoginRequiredMixin, OwnerOnlyMixin, DeleteView):
    model = School
    template_name = 'home/schools/school_confirm_delete.html' # We'll reuse logic but create a separate template or generic one
    success_url = reverse_lazy('school_list')

# Contract Management CRUD (Owner Only)
class ContractListView(LoginRequiredMixin, OwnerOnlyMixin, ListView):
    model = Contract
    template_name = 'home/contracts/contract_list.html'
    context_object_name = 'contracts_list'
    paginate_by = 10

    def get_queryset(self):
        from django.db.models import Q
        queryset = super().get_queryset()
        
        # Search filter
        query = self.request.GET.get('q')
        if query:
            queryset = queryset.filter(
                Q(number__icontains=query) |
                Q(student_full_name__icontains=query) |
                Q(parents_full_name__icontains=query) |
                Q(phone_number__icontains=query)
            )
        
        return queryset.order_by('-created_at')

class ContractCreateView(LoginRequiredMixin, OwnerOnlyMixin, CreateView):
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

class ContractUpdateView(LoginRequiredMixin, OwnerOnlyMixin, UpdateView):
    model = Contract
    template_name = 'home/contracts/contract_form.html'
    form_class = ContractForm
    success_url = reverse_lazy('contract_list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

class ContractDeleteView(LoginRequiredMixin, OwnerOnlyMixin, DeleteView):
    model = Contract
    template_name = 'home/contracts/contract_confirm_delete.html'
    success_url = reverse_lazy('contract_list')

# Payment Management
class PaymentListView(LoginRequiredMixin, OwnerOnlyMixin, ListView):
    model = Contract # We list contracts to show their payment status for a month
    template_name = 'home/payments/payment_list.html'
    context_object_name = 'contracts'
    
    def get_queryset(self):
        # We need to filter contracts that were active during the selected month
        # To do this, we need the selected_month, which we usually get in get_context_data.
        # We'll calculate it here too or move the logic.
        from datetime import datetime
        now = datetime.now()
        month_str = self.request.GET.get('month', now.strftime('%Y-%m'))
        try:
            selected_month = datetime.strptime(month_str, '%Y-%m').date()
        except ValueError:
            selected_month = now.replace(day=1).date()
        
        # Filter: contract signed on or before the end of the selected month
        import calendar
        last_day = calendar.monthrange(selected_month.year, selected_month.month)[1]
        end_of_month = selected_month.replace(day=last_day)
        
        return super().get_queryset().filter(date__lte=end_of_month).order_by('-date')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Handle month filtering (re-using logic or we could have stored it in self)
        from datetime import datetime
        now = datetime.now()
        month_str = self.request.GET.get('month', now.strftime('%Y-%m'))
        try:
            selected_month = datetime.strptime(month_str, '%Y-%m').date()
        except ValueError:
            selected_month = now.replace(day=1).date()
        
        context['selected_month'] = selected_month
        context['month_str'] = month_str
        
        # Annotate contracts with payment info for this month
        for contract in context['contracts']:
            payment = contract.payments.filter(
                for_month__year=selected_month.year,
                for_month__month=selected_month.month
            ).first()
            contract.this_month_paid = payment.amount if payment else 0
            contract.this_month_balance = contract.total_amount - contract.this_month_paid
            contract.payment_id = payment.id if payment else None
            
        return context

# Helper functions for Debt logic
def add_month(d):
    try:
        return d.replace(month=d.month+1)
    except ValueError:
        if d.month == 12:
            return d.replace(year=d.year+1, month=1)
        else:
            raise

RUSSIAN_MONTHS = {
    1: 'Январь', 2: 'Февраль', 3: 'Март', 4: 'Апрель',
    5: 'Май', 6: 'Июнь', 7: 'Июль', 8: 'Август',
    9: 'Сентябрь', 10: 'Октябрь', 11: 'Ноябрь', 12: 'Декабрь'
}

def format_russian_month(d):
    return f"{RUSSIAN_MONTHS[d.month]} {d.year}"

def get_debt_details(contract, target_date):
    from collections import defaultdict
    start_date = contract.date
    if not start_date:
        return 0.0, []
        
    start_first = start_date.replace(day=1)
    target_first = target_date.replace(day=1)
    
    if target_first < start_first:
        return 0.0, []
        
    payments = contract.payments.filter(for_month__lte=target_first)
    payment_map = defaultdict(float)
    for p in payments:
        payment_map[p.for_month.replace(day=1)] += float(p.amount)
        
    unpaid_months = []
    running_balance = 0.0
    monthly_fee = float(contract.total_amount)
    
    curr = start_first
    while curr <= target_first:
        running_balance += monthly_fee
        running_balance -= payment_map[curr]
        
        if running_balance > 0.01:
            unpaid_months.append(curr)
            
        curr = add_month(curr)
        
    return max(0.0, running_balance), unpaid_months

class DebtListView(LoginRequiredMixin, OwnerOnlyMixin, ListView):
    model = Contract
    template_name = 'home/debts/debt_list.html'
    context_object_name = 'contracts'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from datetime import datetime
        now = datetime.now()
        
        # Mode: 'general' (all debt till now) or 'monthly' (snapshot for month)
        view_mode = self.request.GET.get('mode', 'general')
        month_str = self.request.GET.get('month', now.strftime('%Y-%m'))
        
        try:
            target_month = datetime.strptime(month_str, '%Y-%m').date()
        except ValueError:
            target_month = now.replace(day=1).date()
            
        context['view_mode'] = view_mode
        context['month_str'] = month_str
        context['target_month_label'] = format_russian_month(target_month)
        
        # Target month to calculate debt up to
        if view_mode == 'general':
            calc_limit = now.replace(day=1).date()
        else:
            calc_limit = target_month
            
        debtors = []
        for contract in Contract.objects.all():
            total_debt, unpaid_month_dates = get_debt_details(contract, calc_limit)
            if total_debt > 0.01:
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

class PaymentCreateView(LoginRequiredMixin, OwnerOnlyMixin, CreateView):
    model = Payment
    form_class = PaymentForm
    template_name = 'home/payments/payment_form.html'
    success_url = reverse_lazy('payment_list')

    def get_initial(self):
        initial = super().get_initial()
        contract_id = self.request.GET.get('contract')
        month_str = self.request.GET.get('month') # YYYY-MM
        if contract_id:
            initial['contract'] = contract_id
        if month_str:
            initial['for_month'] = f"{month_str}-01"
        from django.utils import timezone
        initial['payment_date'] = timezone.now().date()
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contract_id = self.request.GET.get('contract')
        if contract_id:
            context['contract_obj'] = Contract.objects.filter(id=contract_id).first()
        context['selected_month_str'] = self.request.GET.get('month')
        return context
