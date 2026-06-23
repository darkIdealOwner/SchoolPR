from django.contrib.auth.views import LoginView, PasswordChangeView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import TemplateView, ListView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy
from .forms import PhoneAuthenticationForm, UserManagementForm, SchoolForm
from .models import User, School, Contract

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
    template_name = 'home/user_list.html'
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
    template_name = 'home/user_form.html'
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
    template_name = 'home/user_form.html'
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
    template_name = 'home/user_confirm_delete.html'
    success_url = reverse_lazy('user_list')

# Schools Management CRUD (Owner Only)
class SchoolListView(LoginRequiredMixin, OwnerOnlyMixin, ListView):
    model = School
    template_name = 'home/school_list.html'
    context_object_name = 'schools_list'

class SchoolCreateView(LoginRequiredMixin, OwnerOnlyMixin, CreateView):
    model = School
    template_name = 'home/school_form.html'
    form_class = SchoolForm
    success_url = reverse_lazy('school_list')

class SchoolUpdateView(LoginRequiredMixin, OwnerOnlyMixin, UpdateView):
    model = School
    template_name = 'home/school_form.html'
    form_class = SchoolForm
    success_url = reverse_lazy('school_list')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

class SchoolDeleteView(LoginRequiredMixin, OwnerOnlyMixin, DeleteView):
    model = School
    template_name = 'home/school_confirm_delete.html' # We'll reuse logic but create a separate template or generic one
    success_url = reverse_lazy('school_list')
