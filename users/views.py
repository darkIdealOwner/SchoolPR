import csv
import uuid
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import IntegrityError, models, transaction as db_transaction
from django.db.models import Prefetch, ProtectedError
from django.db.models.functions import Length
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy
from django.views.generic import View, TemplateView, ListView, DetailView, CreateView, UpdateView, DeleteView, FormView
from django.urls import reverse, reverse_lazy
from .forms import (
    PhoneAuthenticationForm, UserManagementForm, SchoolForm, ContractForm, PaymentForm,
    EmployeePayoutForm, TransactionForm, ReversalForm, ExcelUploadForm, AcademicYearForm,
    TransferForm, AccountForm, CategoryForm, CounterpartyForm, RefundForm, ReceiptForm, ResolveReceiptForm, StaffForm,
    WorkbookUploadForm, MonthPriceForm,
)
from . import excel_import, reports, workbook_import
from .excel_export import Line, Percent, xlsx_response
from .templatetags.money import money as format_money
from .models import Account, Category, Counterparty, MonthPrice, Transfer, UnidentifiedReceipt, Staff
from .models import User, School, AcademicYear, Contract, Payment, EmployeePayout, Transaction, AuditLog, shift_month
from .finance import (
    parse_month, contracts_with_active_payments, format_russian_month, get_debt_details,
    build_overview, chart_payload, contract_schedule, account_balances,
)

PAGE_SIZES = (10, 25, 50, 100)


def page_size(request, default=PAGE_SIZES[0]):
    value = request.GET.get('per_page', '')
    return int(value) if value.isdigit() and int(value) in PAGE_SIZES else default


def add_page_context(context):
    """Номера страниц с многоточием и выбор «показывать по» для общего шаблона partials/pagination.html."""
    page = context.get('page_obj')
    context['page_sizes'] = PAGE_SIZES
    if page is not None:
        context['page_range'] = page.paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1)
    return context


def paginate_list(request, items):
    """Постраничный вывод готового списка (когда строки считаются в Python, а не запросом)."""
    from django.core.paginator import Paginator
    page = Paginator(items, page_size(request)).get_page(request.GET.get('page'))
    return add_page_context({'page_obj': page, 'is_paginated': page.has_other_pages()})


class PageSizeMixin:
    """Списки по страницам: ?per_page=10|25|50|100, неверный номер страницы — ближайшая страница, а не ошибка."""
    paginate_by = PAGE_SIZES[0]

    def get_paginate_by(self, queryset):
        return page_size(self.request, self.paginate_by)

    def paginate_queryset(self, queryset, page_size):
        paginator = self.get_paginator(queryset, page_size, allow_empty_first_page=True)
        page = paginator.get_page(self.request.GET.get(self.page_kwarg))
        return paginator, page, page.object_list, page.has_other_pages()

    def get_context_data(self, **kwargs):
        return add_page_context(super().get_context_data(**kwargs))


def contract_search(queryset, query, user, prefix=''):
    """Поиск договоров по любому полю. Каждое слово запроса должно найтись хотя бы в одном поле договора.

    Ищет по номеру, ФИО, классу, направлению, школе, учебному году, примечанию и пометке об ошибке; телефон — по цифрам
    в любом виде; «01.09.2026» — по дате; число от 4 цифр — ещё и по году рождения и суммам. Личные данные (родители,
    телефоны, паспорт, ИНПС, адрес) — только тем, кому их можно видеть. prefix — путь к договору (для оплат: 'contract__').
    """
    import re
    from django.db.models import Q
    query = re.sub(r'(?<=\d) (?=\d{3}\b)', '', (query or '').strip())     # «2 552 000» → «2552000»
    text_fields = ['number', 'student_full_name', 'student_class', 'specialization', 'notes', 'data_problem',
                   'school__name', 'academic_year__name']
    personal = user.can_view_personal_data
    if personal:
        text_fields += ['parents_full_name', 'passport_data', 'inps', 'address', 'phone_number', 'phone_number_2']
    for word in query.split():
        match = Q()
        for field in text_fields:
            match |= Q(**{f'{prefix}{field}__icontains': word})
        digits = re.sub(r'\D', '', word)
        if personal and len(digits) >= 3 and digits != word:
            # «+998 (90)-123-45-67» — в базе телефон хранится цифрами
            match |= Q(**{f'{prefix}phone_number__contains': digits}) | Q(**{f'{prefix}phone_number_2__contains': digits})
        if word.isdigit() and len(word) >= 4:
            number = int(word)
            match |= Q(**{f'{prefix}student_birth_year': number}) | Q(**{f'{prefix}tuition_fee': number}) | \
                Q(**{f'{prefix}total_amount': number}) | Q(**{f'{prefix}discount_amount': number}) | Q(**{f'{prefix}food_cost': number})
        try:
            day = datetime.strptime(word, '%d.%m.%Y').date()
            match |= Q(**{f'{prefix}date': day})
        except ValueError:
            pass
        queryset = queryset.filter(match)
    return queryset      # связи только «договор → школа, учебный год» — строки не дублируются


def number_key(number):
    """Порядок номеров договоров как у людей: 9, 10, 94, 155, а не 10, 155, 9, 94."""
    return (len(number), number)


def sort_list(items, sorts, choice):
    """Сортировка готового списка: sorts — {значение: (подпись, ключ, по убыванию)}, первое значение — по умолчанию."""
    choice = choice if choice in sorts else next(iter(sorts))
    _label, key, reverse = sorts[choice]
    return sorted(items, key=key, reverse=reverse), choice

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
        role = self.request.user.role
        if role in ['OWNER', 'INVESTOR']:
            overview = build_overview()
        elif role in ['DIRECTOR', 'EMPLOYEE'] and self.request.user.school:
            overview = build_overview(school=self.request.user.school)
        else:
            overview = None
        if overview:
            context['overview'] = overview
            months = [row['month'] for row in overview['series']]
            school = None if role in ['OWNER', 'INVESTOR'] else self.request.user.school
            breakdown = reports.monthly_breakdown(months, school) if role != 'EMPLOYEE' else None
            context['chart_data'] = chart_payload(overview['series'], include_spending=role != 'EMPLOYEE', breakdown=breakdown)
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


def safe_next_url(request):
    """Адрес возврата из ?next=, только внутри сайта."""
    url = request.POST.get('next') or request.GET.get('next')
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return url
    return None


def _csv_value(value):
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return format(value.quantize(Decimal('0.01')), 'f').replace('.', ',')
    if isinstance(value, (date, datetime)):
        return value.strftime('%d.%m.%Y')
    if isinstance(value, bool):
        return _('да') if value else _('нет')
    return str(value)


def export_subtitle(user, *parts):
    """Подзаголовок выгрузки: школа (или вся сеть), уточнения отчёта и дата выгрузки."""
    school = user.school.name if user.role in ('DIRECTOR', 'EMPLOYEE') and user.school else _('Вся сеть')
    stamp = _('выгружено %(date)s') % {'date': timezone.localtime().strftime('%d.%m.%Y %H:%M')}
    return ' · '.join(str(p) for p in (school, *parts, stamp) if p)


class CsvExportMixin:
    """?export=xlsx — оформленный файл Excel, ?export=csv — простая таблица (UTF-8 с BOM, разделитель «;»)."""
    export_filename = 'export'
    export_title = ''

    def get_export_rows(self, context):
        raise NotImplementedError

    def get_export_subtitle(self, context):
        return export_subtitle(self.request.user)

    def render_to_response(self, context, **response_kwargs):
        kind = self.request.GET.get('export')
        if kind not in ('csv', 'xlsx'):
            return super().render_to_response(context, **response_kwargs)
        headers, rows = self.get_export_rows(context)
        if kind == 'xlsx':
            return xlsx_response(self.export_filename, self.export_title, self.get_export_subtitle(context), headers, rows,
                                 autofilter=True)
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        stamp = timezone.localdate().strftime('%Y-%m-%d')
        response['Content-Disposition'] = f'attachment; filename="{self.export_filename}_{stamp}.csv"'
        response.write('\ufeff')
        writer = csv.writer(response, delimiter=';')
        writer.writerow(headers)
        for row in rows:
            writer.writerow([_csv_value(v) for v in row])
        return response


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
    protected_message = gettext_lazy("Нельзя удалить: есть связанные финансовые записи.")

    def form_valid(self, form):
        self.object = self.get_object()
        try:
            with db_transaction.atomic():
                AuditLog.record(self.request.user, 'DELETE', self.object)
                self.before_delete(self.object)
                self.object.delete()
        except ProtectedError:
            messages.error(self.request, self.protected_message)
            return redirect(self.get_success_url())
        messages.success(self.request, _("Запись удалена."))
        return redirect(self.get_success_url())

    def before_delete(self, obj):
        """Что удалить вместе с объектом (в той же транзакции)."""

class BaseReverseView(LoginRequiredMixin, OwnerOrDirectorMixin, FormView):
    """Сторно денежной записи: запись остаётся в базе, но перестаёт учитываться в расчётах."""
    form_class = ReversalForm
    template_name = 'home/finance/reverse_confirm.html'
    model = None
    success_url = None

    def get_queryset(self):
        # Документы из 1С сторнируются в самой 1С
        return self.model.active.exclude(source=self.model.SOURCE_1C)

    def get_object(self):
        if not hasattr(self, 'object'):
            self.object = get_object_or_404(self.get_queryset(), pk=self.kwargs['pk'])
        return self.object

    def get_success_url(self):
        return safe_next_url(self.request) or super().get_success_url()

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
        messages.success(self.request, _("Запись сторнирована."))
        return super().form_valid(form)


class DataProblemClearView(LoginRequiredMixin, OwnerOrDirectorMixin, View):
    """Снять красную пометку «ошибка в книге» с записи, когда её проверили."""
    models_by_kind = {
        'contract': (Contract, 'school'), 'payment': (Payment, 'contract__school'),
        'transaction': (Transaction, 'school'), 'transfer': (Transfer, 'school'), 'receipt': (UnidentifiedReceipt, 'school'),
    }

    def post(self, request, kind, pk):
        if kind not in self.models_by_kind:
            raise Http404
        model, school_path = self.models_by_kind[kind]
        qs = model.objects.all()
        if request.user.role == 'DIRECTOR':
            qs = qs.filter(**{school_path: request.user.school})
        obj = get_object_or_404(qs, pk=pk)
        if obj.data_problem:
            AuditLog.record(request.user, 'UPDATE', obj, {'data_problem': [obj.data_problem, '']})
            obj.data_problem = ''
            obj.save(update_fields=['data_problem', 'updated_at'])
            messages.success(request, _("Красная пометка снята."))
        return redirect(safe_next_url(request) or 'home')


class UserListView(LoginRequiredMixin, OwnerOrDirectorMixin, PageSizeMixin, ListView):
    model = User
    template_name = 'home/users/user_list.html'
    context_object_name = 'users_list'

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
    protected_message = gettext_lazy("Нельзя удалить пользователя: по нему есть выплаты. Отключите доступ вместо удаления.")

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

class AcademicYearListView(LoginRequiredMixin, OwnerOnlyMixin, ListView):
    model = AcademicYear
    template_name = 'home/schools/academic_year_list.html'
    context_object_name = 'years'

    def get_queryset(self):
        return AcademicYear.objects.annotate(contract_count=models.Count('contracts'))

class AcademicYearCreateView(LoginRequiredMixin, OwnerOnlyMixin, AuditMixin, CreateView):
    model = AcademicYear
    form_class = AcademicYearForm
    template_name = 'home/schools/academic_year_form.html'
    success_url = reverse_lazy('academic_year_list')

class AcademicYearUpdateView(LoginRequiredMixin, OwnerOnlyMixin, AuditMixin, UpdateView):
    model = AcademicYear
    form_class = AcademicYearForm
    template_name = 'home/schools/academic_year_form.html'
    success_url = reverse_lazy('academic_year_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_edit'] = True
        return context

class SchoolDeleteView(LoginRequiredMixin, OwnerOnlyMixin, ProtectedDeleteMixin, DeleteView):
    def before_delete(self, obj):
        # Пустые счета создаются вместе со школой; если по ним были движения — удаление остановит PROTECT
        obj.accounts.all().delete()

    model = School
    template_name = 'home/schools/school_confirm_delete.html' # We'll reuse logic but create a separate template or generic one
    success_url = reverse_lazy('school_list')
    protected_message = gettext_lazy("Нельзя удалить школу: к ней привязаны договоры, выплаты или транзакции.")

# Contract Management CRUD (Owner, Director, Employee or Investor)
class ContractListView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeOrInvestorMixin, CsvExportMixin, PageSizeMixin, ListView):
    model = Contract
    template_name = 'home/contracts/contract_list.html'
    context_object_name = 'contracts_list'
    SORTS = {
        '': (gettext_lazy('Сначала новые'), ['-created_at', '-pk']),
        'number': (gettext_lazy('№ по возрастанию'), [Length('number'), 'number']),
        '-number': (gettext_lazy('№ по убыванию'), [Length('number').desc(), '-number']),
        'student': (gettext_lazy('ФИО ученика: А → Я'), ['student_full_name', 'pk']),
        '-student': (gettext_lazy('ФИО ученика: Я → А'), ['-student_full_name', '-pk']),
        'date': (gettext_lazy('Дата договора: сначала старые'), ['date', 'pk']),
        '-date': (gettext_lazy('Дата договора: сначала новые'), ['-date', '-pk']),
        '-total': (gettext_lazy('Сумма в месяц: больше сначала'), ['-total_amount', 'pk']),
        'total': (gettext_lazy('Сумма в месяц: меньше сначала'), ['total_amount', 'pk']),
    }
    export_filename = 'dogovory'
    export_title = gettext_lazy('Договоры')

    def get_export_rows(self, context):
        personal = self.request.user.can_view_personal_data
        headers = [_('№ договора'), _('Школа'), _('Учебный год'), _('Дата'), _('Первый месяц обучения'), _('ФИО ученика'),
                   _('Год рождения'), _('Класс'), _('Срок, мес'), _('Направление')]
        if personal:
            headers += [_('ФИО родителей'), _('Паспорт'), _('ИНПС'), _('Адрес'), _('Телефон'), _('Доп. телефон')]
        headers += [_('Стоимость'), _('Скидка %'), _('Скидка суммой'), _('Причина скидки'), _('Скидка, сум'), _('Питание'), _('Итого в месяц'), _('Отменён'), _('Дата отмены'),
                    _('Начисления по'), _('Примечание'), _('Ошибка в книге')]
        rows = []
        for c in self.object_list:
            row = [c.number, c.school.name if c.school else '', c.academic_year.name if c.academic_year else '', c.date,
                   c.first_billing_month.strftime('%m.%Y'), c.student_full_name, c.student_birth_year,
                   c.student_class, c.duration_months, c.specialization]
            if personal:
                row += [c.parents_full_name, c.passport_data, c.inps, c.address, c.phone_number, c.phone_number_2]
            last = c.last_billing_month
            row += [c.tuition_fee, c.discount_percentage, c.discount_fixed, c.get_discount_reason_display(),
                    c.discount_amount, c.food_cost, c.total_amount,
                    c.is_cancelled, c.cancelled_at if c.cancelled_at else (_('не указана') if c.is_cancelled else ''),
                    last.strftime('%m.%Y') if last else '', c.notes, c.data_problem]
            rows.append(row)
        return headers, rows

    def get_queryset(self):
        from django.db.models import Q
        queryset = contracts_with_active_payments(super().get_queryset()).select_related('academic_year')
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            queryset = queryset.filter(school=self.request.user.school)

        status = self.request.GET.get('status')
        if status == 'active':
            queryset = queryset.filter(is_cancelled=False)
        elif status == 'cancelled':
            queryset = queryset.filter(is_cancelled=True)
        elif status == 'no_cancel_date':
            queryset = queryset.filter(is_cancelled=True, cancelled_at__isnull=True)
        elif status == 'problem':
            queryset = queryset.exclude(data_problem='')
        elif status == 'incomplete':
            queryset = queryset.filter(
                Q(passport_data='') | Q(inps='') | Q(address='')
                | ~Q(passport_data__regex=r'^[A-Z]{2}[0-9]{7}$') | ~Q(inps__regex=r'^[0-9]{14}$'))
        year = self.request.GET.get('year')
        if year and year.isdigit():
            queryset = queryset.filter(academic_year_id=int(year))

        queryset = contract_search(queryset, self.request.GET.get('q'), self.request.user)
        self.sort = self.request.GET.get('sort', '')
        return queryset.order_by(*self.SORTS.get(self.sort, self.SORTS[''])[1])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['academic_years'] = AcademicYear.objects.all()
        context['status_filters'] = [
            ('', _('Все')), ('active', _('Действующие')), ('cancelled', _('Отменённые')),
            ('no_cancel_date', _('Отменённые без даты')), ('problem', _('С ошибками из книги')),
        ]
        if self.request.user.can_view_personal_data:
            context['status_filters'].append(('incomplete', _('Неполные данные')))
        flagged = Contract.objects.exclude(data_problem='')
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            flagged = flagged.filter(school=self.request.user.school)
        context['problem_count'] = flagged.count()
        context['sort_options'] = [(value, label) for value, (label, _order) in self.SORTS.items()]
        context['sort'] = self.sort if self.sort in self.SORTS else ''
        return context

class ContractDetailView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeOrInvestorMixin, DetailView):
    model = Contract
    template_name = 'home/contracts/contract_detail.html'
    context_object_name = 'contract'

    def get_queryset(self):
        qs = contracts_with_active_payments(Contract.objects.all())
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            qs = qs.filter(school=self.request.user.school)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contract = self.object
        schedule = contract_schedule(contract)
        debt, unpaid = get_debt_details(contract, timezone.localdate())
        status = contract.status()
        siblings = contract.siblings().order_by('number')
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            siblings = siblings.filter(school=self.request.user.school)
        context.update({
            'siblings': siblings,
            'data_warnings': contract.data_warnings if self.request.user.can_view_personal_data else [],
            'schedule': schedule,
            'debt': debt,
            'unpaid_count': len(unpaid),
            'paid_total': sum((row['paid'] for row in schedule), Decimal(0)),
            'accrued_total': sum((row['accrued'] for row in schedule), Decimal(0)),
            'status': status,
            'payments_history': Payment.objects.filter(contract=contract)
                .select_related('created_by', 'reversed_by').order_by('-payment_date', '-created_at'),
            'can_edit': self.request.user.role in ['OWNER', 'DIRECTOR', 'EMPLOYEE'],
            'can_reverse': self.request.user.role in ['OWNER', 'DIRECTOR'],
        })
        return context

class MonthPriceView(LoginRequiredMixin, OwnerOrDirectorMixin, FormView):
    """Стоимость отдельного месяца договора вручную; без неё месяц стоит как обычно по договору."""
    form_class = MonthPriceForm
    template_name = 'home/contracts/month_price_form.html'

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            qs = Contract.objects.all()
            if request.user.role == 'DIRECTOR':
                qs = qs.filter(school=request.user.school)
            self.contract = get_object_or_404(qs, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), 'contract': self.contract}

    def get_initial(self):
        initial = super().get_initial()
        month = self.request.GET.get('month')
        if month:
            first = parse_month(month)
            initial['month'] = first
            existing = self.contract.month_price_map.get(first)
            initial['amount'] = existing.amount if existing else self.contract.accrual_for(first)
            if existing:
                initial['reason'] = existing.reason
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['contract'] = self.contract
        context['prices'] = self.contract.month_prices.select_related('created_by')
        return context

    def success(self):
        return redirect(safe_next_url(self.request) or reverse('contract_detail', args=[self.contract.pk]))

    def post(self, request, *args, **kwargs):
        delete_id = request.POST.get('delete')
        if delete_id:
            price = get_object_or_404(self.contract.month_prices, pk=delete_id)
            AuditLog.record(request.user, 'DELETE', price, {'month': str(price.month), 'amount': str(price.amount)})
            price.delete()
            messages.success(request, _("Стоимость месяца снова как по договору."))
            return self.success()
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        month, amount, reason = form.cleaned_data['month'], form.cleaned_data['amount'], form.cleaned_data['reason']
        price, created = MonthPrice.objects.get_or_create(
            contract=self.contract, month=month,
            defaults={'amount': amount, 'reason': reason, 'source': MonthPrice.SOURCE_MANUAL, 'created_by': self.request.user})
        if created:
            AuditLog.record(self.request.user, 'CREATE', price, {'month': str(month), 'amount': str(amount), 'reason': reason})
        else:
            AuditLog.record(self.request.user, 'UPDATE', price, {'amount': [str(price.amount), str(amount)],
                                                                 'reason': [price.reason, reason]})
            price.amount, price.reason, price.source = amount, reason, MonthPrice.SOURCE_MANUAL
            price.save()
        messages.success(self.request, _("Стоимость месяца сохранена."))
        return self.success()


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
    protected_message = gettext_lazy("Нельзя удалить договор: по нему есть оплаты. Отметьте договор как отменённый.")

    def form_valid(self, form):
        # Отменённые договоры остаются в отчётах — их не удаляют
        if self.get_object().is_cancelled:
            messages.error(self.request, _("Отменённый договор не удаляется: он нужен в отчётах."))
            return redirect(self.get_success_url())
        return super().form_valid(form)

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            return qs.filter(school=self.request.user.school)
        return qs

# Payment Management
class PaymentListView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeOrInvestorMixin, CsvExportMixin, PageSizeMixin, ListView):
    model = Contract # We list contracts to show their payment status for a month
    template_name = 'home/payments/payment_list.html'
    context_object_name = 'contracts'
    export_filename = 'oplaty'
    export_title = gettext_lazy('Оплаты за месяц')
    SORTS = {
        '': (gettext_lazy('Сначала новые договоры'), lambda c: (c.date, c.pk), True),
        'number': (gettext_lazy('№ по возрастанию'), lambda c: number_key(c.number), False),
        '-number': (gettext_lazy('№ по убыванию'), lambda c: number_key(c.number), True),
        'student': (gettext_lazy('ФИО ученика: А → Я'), lambda c: c.student_full_name.lower(), False),
        '-student': (gettext_lazy('ФИО ученика: Я → А'), lambda c: c.student_full_name.lower(), True),
        '-balance': (gettext_lazy('Остаток к оплате: больше сначала'), lambda c: c.this_month_balance, True),
        'balance': (gettext_lazy('Остаток к оплате: меньше сначала'), lambda c: c.this_month_balance, False),
    }

    def get_export_subtitle(self, context):
        return export_subtitle(self.request.user, format_russian_month(self.selected_month))

    def get_export_rows(self, context):
        headers = [_('Месяц'), _('№ договора'), _('Школа'), _('Ученик'), _('Начислено'), _('Оплачено'), _('Остаток')]
        month = format_russian_month(self.selected_month)
        rows = [[month, c.number, c.school.name if c.school else '', c.student_full_name,
                 c.this_month_accrued, c.this_month_paid, c.this_month_balance] for c in self.object_list]
        return headers, rows

    def get_queryset(self):
        self.selected_month = parse_month(self.request.GET.get('month'))
        qs = super().get_queryset()
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            qs = qs.filter(school=self.request.user.school)
        qs = contract_search(qs.filter(date__lt=shift_month(self.selected_month, 1)), self.request.GET.get('q'), self.request.user)
        qs = contracts_with_active_payments(qs.select_related('school').order_by('-date', '-pk'))
        # Только договоры, по которым в этом месяце есть начисление (учитываем срок и отмену)
        contracts = [c for c in qs if c.has_accrual_in(self.selected_month)]
        for contract in contracts:
            month_payments = [p for p in contract.payments.all() if p.for_month.replace(day=1) == self.selected_month]
            contract.month_payments = month_payments
            contract.this_month_paid = sum((p.signed_amount for p in month_payments), Decimal(0))
            contract.this_month_accrued = contract.accrual_for(self.selected_month)
            contract.this_month_balance = contract.this_month_accrued - contract.this_month_paid
        contracts, self.sort = sort_list(contracts, self.SORTS, self.request.GET.get('sort', ''))
        return contracts

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_month = self.selected_month
        context['selected_month'] = selected_month
        context['month_str'] = selected_month.strftime('%Y-%m')

        receipts = UnidentifiedReceipt.active.filter(payment__isnull=True)
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            receipts = receipts.filter(school=self.request.user.school)
        context['open_receipts'] = receipts.count()
        context['sort_options'] = [(value, label) for value, (label, *_rest) in self.SORTS.items()]
        context['sort'] = self.sort
        return context

class DebtListView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeOrInvestorMixin, CsvExportMixin, ListView):
    model = Contract
    template_name = 'home/debts/debt_list.html'
    context_object_name = 'contracts'
    export_filename = 'dolgi'
    export_title = gettext_lazy('Задолженности')
    SORTS = {
        '': (gettext_lazy('Долг: больше сначала'), lambda c: c.total_debt, True),
        'debt': (gettext_lazy('Долг: меньше сначала'), lambda c: c.total_debt, False),
        '-months': (gettext_lazy('Просрочено месяцев: больше сначала'), lambda c: (c.unpaid_count, c.total_debt), True),
        'number': (gettext_lazy('№ по возрастанию'), lambda c: number_key(c.number), False),
        '-number': (gettext_lazy('№ по убыванию'), lambda c: number_key(c.number), True),
        'student': (gettext_lazy('ФИО ученика: А → Я'), lambda c: c.student_full_name.lower(), False),
        '-student': (gettext_lazy('ФИО ученика: Я → А'), lambda c: c.student_full_name.lower(), True),
    }

    def get_export_subtitle(self, context):
        if context['view_mode'] == 'general':
            return export_subtitle(self.request.user, _('общий долг на сегодня'))
        return export_subtitle(self.request.user, _('долги за %(month)s') % {'month': context['target_month_label']})

    def get_export_rows(self, context):
        personal = self.request.user.can_view_personal_data
        headers = [_('№ договора'), _('Школа'), _('Ученик')] + ([_('Телефон'), _('Родитель')] if personal else []) + \
                  [_('Платёж в месяц'), _('Просрочено месяцев'), _('Неоплаченные месяцы'), _('Сумма долга')]
        rows = []
        for c in context['debtors']:
            row = [c.number, c.school.name if c.school else '', c.student_full_name]
            if personal:
                row += [c.phone_number, c.parents_full_name]
            row += [c.total_amount, c.unpaid_count, ', '.join(c.unpaid_months), c.total_debt]
            rows.append(row)
        return headers, rows

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

        contract_qs = Contract.objects.select_related('school')
        if self.request.user.role in ['DIRECTOR', 'EMPLOYEE']:
            contract_qs = contract_qs.filter(school=self.request.user.school)
        contract_qs = contract_search(contract_qs, self.request.GET.get('q'), self.request.user)

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

        debtors, context['sort'] = sort_list(debtors, self.SORTS, self.request.GET.get('sort', ''))
        context['sort_options'] = [(value, label) for value, (label, *_rest) in self.SORTS.items()]
        context['debtors'] = debtors        # все — для выгрузки и итога
        context['debt_total'] = sum((c.total_debt for c in debtors), Decimal(0))
        context.update(paginate_list(self.request, debtors))
        context['debtors_page'] = context['page_obj'].object_list
        return context

class PaymentCreateView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeMixin, AuditMixin, CreateView):
    model = Payment
    form_class = PaymentForm
    template_name = 'home/payments/payment_form.html'

    def get_success_url(self):
        return safe_next_url(self.request) or f"{reverse_lazy('payment_list')}?month={self.object.for_month.strftime('%Y-%m')}"

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
            paid = sum((p.signed_amount for p in Payment.active.filter(contract=contract, for_month=month_first)), Decimal(0))
            initial['amount'] = max(contract.accrual_for(month_first) - paid, Decimal(0))
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contract_id = self.request.GET.get('contract') or self.request.POST.get('contract')
        if contract_id:
            context['contract_obj'] = self.get_allowed_contracts().filter(id=contract_id).first()
        context['selected_month_str'] = self.request.GET.get('month')
        if context['selected_month_str']:
            context['selected_month_label'] = format_russian_month(parse_month(context['selected_month_str']))
        return context

class PaymentReverseView(BaseReverseView):
    model = Payment

    def get_queryset(self):
        qs = super().get_queryset().select_related('contract')
        if self.request.user.role == 'DIRECTOR':
            qs = qs.filter(contract__school=self.request.user.school)
        return qs

    def get_success_url(self):
        return safe_next_url(self.request) or f"{reverse_lazy('payment_list')}?month={self.get_object().for_month.strftime('%Y-%m')}"

# Employee Payouts CRUD Views
class EmployeePayoutListView(LoginRequiredMixin, OwnerOrDirectorOrInvestorMixin, CsvExportMixin, PageSizeMixin, ListView):
    model = EmployeePayout
    template_name = 'home/payouts/payout_list.html'
    context_object_name = 'payouts_list'
    export_filename = 'vyplaty_sotrudnikam'
    export_title = gettext_lazy('Выплаты сотрудникам')

    def get_export_rows(self, context):
        headers = [_('Дата выплаты'), _('За месяц'), _('Сотрудник'), _('Школа'), _('Сумма'), _('Примечание'), _('Сторно'), _('Причина сторно')]
        rows = [[p.payout_date, format_russian_month(p.for_month), p.employee.full_name, p.school.name, p.amount,
                 p.notes, p.is_reversed, p.reversal_reason] for p in self.object_list]
        return headers, rows

    def get_queryset(self):
        queryset = super().get_queryset().select_related('employee', 'school', 'reversed_by')
        if self.request.user.role == 'DIRECTOR':
            queryset = queryset.filter(school=self.request.user.school)
        return queryset.order_by('-payout_date', '-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['mode'] = 'summary' if self.request.GET.get('mode') == 'summary' else 'list'
        if context['mode'] == 'summary':
            context['summary'] = self.salary_summary()
        return context

    def salary_summary(self):
        """Зарплата одной суммой по месяцам: выплаты сотрудникам + расходы по статьям «зарплата»."""
        payouts = EmployeePayout.active.all()
        salary = Transaction.active.filter(category__is_salary=True)
        if self.request.user.role == 'DIRECTOR':
            payouts = payouts.filter(school=self.request.user.school)
            salary = salary.filter(school=self.request.user.school)
        rows = defaultdict(lambda: {'payouts': Decimal(0), 'lump': Decimal(0), 'people': set()})
        for p in payouts.only('for_month', 'amount', 'employee_id'):
            row = rows[p.for_month.replace(day=1)]
            row['payouts'] += p.amount
            row['people'].add(p.employee_id)
        for t in salary.only('date', 'amount'):
            rows[t.date.replace(day=1)]['lump'] += t.amount
        result = []
        for month in sorted(rows, reverse=True):
            row = rows[month]
            result.append({'month': month, 'label': format_russian_month(month), 'payouts': row['payouts'],
                           'people': len(row['people']), 'lump': row['lump'], 'total': row['payouts'] + row['lump']})
        return result

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
        qs = EmployeePayout.active.exclude(source=EmployeePayout.SOURCE_1C)
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
class TransactionListView(LoginRequiredMixin, OwnerOrDirectorOrInvestorMixin, CsvExportMixin, PageSizeMixin, ListView):
    model = Transaction
    template_name = 'home/transactions/transaction_list.html'
    context_object_name = 'transactions_list'
    export_filename = 'tranzakcii'
    export_title = gettext_lazy('Транзакции школы')

    def get_export_rows(self, context):
        headers = [_('Дата'), _('Школа'), _('Тип'), _('Статья'), _('Счёт'), _('Контрагент'), _('Сумма'), _('Примечание'),
                   _('Сторно'), _('Причина сторно')]
        rows = [[t.date, t.school.name, t.get_type_display(), t.category.display_name if t.category else '',
                 t.account.display_name if t.account else '', t.counterparty.display_name if t.counterparty else '',
                 t.amount, t.notes, t.is_reversed, t.reversal_reason] for t in self.object_list]
        return headers, rows

    def get_queryset(self):
        queryset = super().get_queryset().select_related('school', 'reversed_by', 'category', 'account', 'counterparty')
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
        qs = Transaction.active.exclude(source=Transaction.SOURCE_1C)
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


# Загрузка из Excel
class ExcelImportAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Загружать можно то же, что разрешено вносить вручную; директору и сотруднику нужна своя школа."""

    def test_func(self):
        kind = self.kwargs['kind']
        if kind not in excel_import.KINDS:
            raise Http404
        user = self.request.user
        return user.role in excel_import.KINDS[kind]['roles'] and (user.role == 'OWNER' or user.school_id)


class ExcelTemplateView(ExcelImportAccessMixin, View):
    def get(self, request, kind):
        content = excel_import.build_template(kind, request.user)
        response = HttpResponse(content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="{excel_import.KINDS[kind]["filename"]}.xlsx"'
        return response


class ExcelImportView(ExcelImportAccessMixin, TemplateView):
    """Загрузка файла → предпросмотр с ошибками по строкам → сохранение правильных строк."""
    template_name = 'home/import/excel_import.html'
    session_key = 'excel_import'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        kind = self.kwargs['kind']
        context.update({
            'kind': kind,
            'kind_title': excel_import.KINDS[kind]['title'],
            'list_url': excel_import.KINDS[kind]['list_url'],
            'columns': [(c, c.is_required(self.request.user)) for c in excel_import.columns_for(kind, self.request.user)],
            'max_rows': excel_import.MAX_ROWS,
            'form': kwargs.get('form') or ExcelUploadForm(),
        })
        return context

    def post(self, request, kind):
        action = request.POST.get('action')
        if action == 'cancel':
            request.session.pop(self.session_key, None)
            return redirect('excel_import', kind=kind)
        if action == 'confirm':
            return self.confirm(request, kind)

        form = ExcelUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        uploaded = form.cleaned_data['file']
        try:
            rows, ignored = excel_import.extract_rows(kind, request.user, excel_import.read_table(uploaded))
        except excel_import.ImportFileError as exc:
            form.add_error('file', str(exc))
            return self.render_to_response(self.get_context_data(form=form))

        results = excel_import.check_rows(kind, request.user, rows)
        token = uuid.uuid4().hex
        request.session[self.session_key] = {
            'kind': kind, 'token': token, 'filename': uploaded.name, 'rows': rows, 'ignored': ignored,
        }
        counts = {status: sum(1 for r in results if r.status == status) for status in ('ok', 'exists', 'skipped', 'error')}
        return self.render_to_response(self.get_context_data(
            preview=True, results=results, counts=counts, ignored=ignored, token=token, filename=uploaded.name,
        ))

    def confirm(self, request, kind):
        pending = request.session.get(self.session_key)
        if not pending or pending.get('kind') != kind or pending.get('token') != request.POST.get('token'):
            messages.error(request, _("Эта загрузка уже сохранена или устарела. Загрузите файл ещё раз."))
            return redirect('excel_import', kind=kind)
        # Проверяем заново: за время предпросмотра данные на платформе могли измениться
        rows = [(number, values) for number, values in pending['rows']]
        results = excel_import.check_rows(kind, request.user, rows)
        try:
            saved = excel_import.save_rows(kind, request.user, results, pending['filename'])
        except IntegrityError:
            messages.error(request, _("Не удалось сохранить: данные изменились во время загрузки. Загрузите файл ещё раз."))
            return redirect('excel_import', kind=kind)
        request.session.pop(self.session_key, None)
        skipped = len(results) - saved
        text = _("Из файла «%(file)s» загружено строк: %(saved)s.") % {'file': pending['filename'], 'saved': saved}
        if skipped:
            text += ' ' + _("Пропущено: %(skipped)s (ошибки или уже были на платформе).") % {'skipped': skipped}
        messages.success(request, text)
        return redirect(excel_import.KINDS[kind]['list_url'])


# Счета, переводы, справочники статей и контрагентов
class SchoolScopedMixin:
    """Директор видит только свою школу; у владельца — все."""
    school_field = 'school'

    def scope(self, qs):
        if self.request.user.role == 'DIRECTOR':
            return qs.filter(**{self.school_field: self.request.user.school})
        return qs


class AccountListView(LoginRequiredMixin, OwnerOrDirectorMixin, SchoolScopedMixin, ListView):
    template_name = 'home/finance/account_list.html'
    context_object_name = 'accounts'

    def get_queryset(self):
        return self.scope(Account.objects.select_related('school'))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        balances = account_balances(context['accounts'])
        totals = defaultdict(Decimal)
        for account in context['accounts']:
            account.balance = balances[account.pk]
            if account.is_active:
                totals[account.school.name] += account.balance
        context['school_totals'] = sorted(totals.items())
        context['today'] = timezone.localdate()
        return context


class AccountCreateView(LoginRequiredMixin, OwnerOrDirectorMixin, AuditMixin, CreateView):
    model = Account
    form_class = AccountForm
    template_name = 'home/finance/simple_form.html'
    success_url = reverse_lazy('account_list')
    extra_context = {'title': gettext_lazy('Новый счёт'), 'back_url': reverse_lazy('account_list')}

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), 'user': self.request.user}

    def form_valid(self, form):
        if self.request.user.role != 'OWNER':
            form.instance.school = self.request.user.school
        return super().form_valid(form)


class AccountUpdateView(LoginRequiredMixin, OwnerOrDirectorMixin, SchoolScopedMixin, AuditMixin, UpdateView):
    model = Account
    form_class = AccountForm
    template_name = 'home/finance/simple_form.html'
    success_url = reverse_lazy('account_list')
    extra_context = {'title': gettext_lazy('Изменить счёт'), 'back_url': reverse_lazy('account_list')}

    def get_queryset(self):
        return self.scope(Account.objects.all())

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), 'user': self.request.user}


class TransferListView(LoginRequiredMixin, OwnerOrDirectorMixin, SchoolScopedMixin, PageSizeMixin, ListView):
    template_name = 'home/finance/transfer_list.html'
    context_object_name = 'transfers'
    paginate_by = 25

    def get_queryset(self):
        return self.scope(Transfer.objects.select_related('school', 'from_account', 'to_account', 'reversed_by')) \
            .order_by('-date', '-created_at')


class TransferCreateView(LoginRequiredMixin, OwnerOrDirectorMixin, AuditMixin, CreateView):
    model = Transfer
    form_class = TransferForm
    template_name = 'home/finance/simple_form.html'
    success_url = reverse_lazy('transfer_list')
    extra_context = {'title': gettext_lazy('Перевод между счетами'), 'back_url': reverse_lazy('transfer_list')}

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), 'user': self.request.user}

    def form_valid(self, form):
        form.instance.school = form.cleaned_data['from_account'].school
        return super().form_valid(form)


class TransferReverseView(SchoolScopedMixin, BaseReverseView):
    model = Transfer
    success_url = reverse_lazy('transfer_list')

    def get_queryset(self):
        return self.scope(super().get_queryset())


class CategoryListView(LoginRequiredMixin, OwnerOnlyMixin, ListView):
    template_name = 'home/finance/category_list.html'
    context_object_name = 'categories'

    def get_queryset(self):
        return Category.objects.annotate(usage=models.Count('transactions'))


class CategoryCreateView(LoginRequiredMixin, OwnerOnlyMixin, AuditMixin, CreateView):
    model = Category
    form_class = CategoryForm
    template_name = 'home/finance/simple_form.html'
    success_url = reverse_lazy('category_list')
    extra_context = {'title': gettext_lazy('Новая статья'), 'back_url': reverse_lazy('category_list')}


class CategoryUpdateView(LoginRequiredMixin, OwnerOnlyMixin, AuditMixin, UpdateView):
    model = Category
    form_class = CategoryForm
    template_name = 'home/finance/simple_form.html'
    success_url = reverse_lazy('category_list')
    extra_context = {'title': gettext_lazy('Изменить статью'), 'back_url': reverse_lazy('category_list')}


class CounterpartyListView(LoginRequiredMixin, OwnerOnlyMixin, ListView):
    template_name = 'home/finance/counterparty_list.html'
    context_object_name = 'counterparties'

    def get_queryset(self):
        return Counterparty.objects.select_related('investor').annotate(usage=models.Count('transactions'))


class CounterpartyCreateView(LoginRequiredMixin, OwnerOnlyMixin, AuditMixin, CreateView):
    model = Counterparty
    form_class = CounterpartyForm
    template_name = 'home/finance/simple_form.html'
    success_url = reverse_lazy('counterparty_list')
    extra_context = {'title': gettext_lazy('Новый контрагент'), 'back_url': reverse_lazy('counterparty_list')}


class CounterpartyUpdateView(LoginRequiredMixin, OwnerOnlyMixin, AuditMixin, UpdateView):
    model = Counterparty
    form_class = CounterpartyForm
    template_name = 'home/finance/simple_form.html'
    success_url = reverse_lazy('counterparty_list')
    extra_context = {'title': gettext_lazy('Изменить контрагента'), 'back_url': reverse_lazy('counterparty_list')}



# Возвраты родителям и нераспознанные поступления
class RefundCreateView(LoginRequiredMixin, OwnerOrDirectorMixin, FormView):
    form_class = RefundForm
    template_name = 'home/finance/simple_form.html'

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            qs = contracts_with_active_payments(Contract.objects.all())
            if request.user.role == 'DIRECTOR':
                qs = qs.filter(school=request.user.school)
            self.contract = get_object_or_404(qs, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['contract'] = self.contract
        kwargs['instance'] = Payment(contract=self.contract, kind=Payment.KIND_REFUND, payment_date=timezone.localdate())
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = _("Возврат родителю: %(student)s, договор № %(number)s") % {
            'student': self.contract.student_full_name, 'number': self.contract.number}
        context['back_url'] = reverse_lazy('contract_detail', args=[self.contract.pk])
        return context

    def form_valid(self, form):
        refund = form.save(commit=False)
        refund.created_by = self.request.user
        refund.save()
        AuditLog.record(self.request.user, 'CREATE', refund, {'kind': 'REFUND', 'amount': str(refund.amount),
                                                              'reason': refund.notes})
        messages.success(self.request, _("Возврат записан."))
        return redirect('contract_detail', pk=self.contract.pk)


class ReceiptListView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeMixin, SchoolScopedMixin, PageSizeMixin, ListView):
    template_name = 'home/finance/receipt_list.html'
    context_object_name = 'receipts'
    paginate_by = 25

    def scope(self, qs):
        if self.request.user.role != 'OWNER':
            return qs.filter(school=self.request.user.school)
        return qs

    def get_queryset(self):
        qs = self.scope(UnidentifiedReceipt.objects.select_related('school', 'account', 'payment__contract', 'resolved_by'))
        status = self.request.GET.get('status', 'open')
        if status == 'open':
            qs = qs.filter(payment__isnull=True, is_reversed=False)
        elif status == 'resolved':
            qs = qs.filter(payment__isnull=False)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['status'] = self.request.GET.get('status', 'open')
        context['status_filters'] = [('open', _('Не разобраны')), ('resolved', _('Разобраны')), ('all', _('Все'))]
        return context


class ReceiptCreateView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeMixin, AuditMixin, CreateView):
    model = UnidentifiedReceipt
    form_class = ReceiptForm
    template_name = 'home/finance/simple_form.html'
    success_url = reverse_lazy('receipt_list')
    extra_context = {'title': gettext_lazy('Нераспознанное поступление'), 'back_url': reverse_lazy('receipt_list')}

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), 'user': self.request.user}

    def form_valid(self, form):
        form.instance.school = form.cleaned_data['account'].school
        return super().form_valid(form)


class ReceiptResolveView(LoginRequiredMixin, OwnerOrDirectorOrEmployeeMixin, FormView):
    form_class = ResolveReceiptForm
    template_name = 'home/finance/simple_form.html'

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            qs = UnidentifiedReceipt.active.filter(payment__isnull=True)
            if request.user.role != 'OWNER':
                qs = qs.filter(school=request.user.school)
            self.receipt = get_object_or_404(qs, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), 'receipt': self.receipt}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = _("Разобрать поступление: %(amount)s от %(date)s") % {
            'amount': format_money(self.receipt.amount), 'date': self.receipt.date.strftime('%d.%m.%Y')}
        context['back_url'] = reverse_lazy('receipt_list')
        return context

    def form_valid(self, form):
        with db_transaction.atomic():
            payment = self.receipt.resolve(self.request.user, form.cleaned_data['contract'], form.cleaned_data['for_month'])
            AuditLog.record(self.request.user, 'CREATE', payment, {'from_receipt': self.receipt.pk})
        messages.success(self.request, _("Поступление привязано к договору № %(number)s.") % {'number': payment.contract.number})
        return redirect('receipt_list')


class ReceiptReverseView(BaseReverseView):
    model = UnidentifiedReceipt
    success_url = reverse_lazy('receipt_list')

    def get_queryset(self):
        qs = super().get_queryset().filter(payment__isnull=True)
        if self.request.user.role == 'DIRECTOR':
            qs = qs.filter(school=self.request.user.school)
        return qs



# Справочник сотрудников (для зарплаты; входить на платформу им не обязательно)
class StaffListView(LoginRequiredMixin, OwnerOrDirectorMixin, SchoolScopedMixin, ListView):
    template_name = 'home/payouts/staff_list.html'
    context_object_name = 'staff_list'

    def get_queryset(self):
        qs = self.scope(Staff.objects.select_related('school', 'user'))
        return qs.annotate(paid_total=models.Sum('payouts__amount', filter=models.Q(payouts__is_reversed=False)),
                           last_payout=models.Max('payouts__payout_date', filter=models.Q(payouts__is_reversed=False))) \
            .order_by('-is_active', 'full_name')


class StaffCreateView(LoginRequiredMixin, OwnerOrDirectorMixin, AuditMixin, CreateView):
    model = Staff
    form_class = StaffForm
    template_name = 'home/finance/simple_form.html'
    success_url = reverse_lazy('staff_list')
    extra_context = {'title': gettext_lazy('Новый сотрудник'), 'back_url': reverse_lazy('staff_list')}

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), 'user': self.request.user}

    def form_valid(self, form):
        if self.request.user.role != 'OWNER':
            form.instance.school = self.request.user.school
        return super().form_valid(form)


class StaffUpdateView(LoginRequiredMixin, OwnerOrDirectorMixin, SchoolScopedMixin, AuditMixin, UpdateView):
    model = Staff
    form_class = StaffForm
    template_name = 'home/finance/simple_form.html'
    success_url = reverse_lazy('staff_list')
    extra_context = {'title': gettext_lazy('Изменить сотрудника'), 'back_url': reverse_lazy('staff_list')}

    def get_queryset(self):
        return self.scope(Staff.objects.all())

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), 'user': self.request.user}


# Отчёты
class ReportMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Фильтры отчётов: учебный год и школа (директор и сотрудник видят только свою школу)."""
    roles = ('OWNER', 'DIRECTOR', 'INVESTOR')
    report = ''

    def test_func(self):
        user = self.request.user
        return user.role in self.roles and (user.role not in ('DIRECTOR', 'EMPLOYEE') or user.school_id)

    def get_filters(self):
        year_id = self.request.GET.get('year', '')
        self.year = AcademicYear.objects.filter(pk=year_id).first() if year_id.isdigit() else None
        self.year = self.year or reports.current_academic_year()
        user = self.request.user
        if user.role in ('DIRECTOR', 'EMPLOYEE'):
            self.school = user.school
        else:
            school_id = self.request.GET.get('school', '')
            self.school = School.objects.filter(pk=school_id).first() if school_id.isdigit() else None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'report': self.report, 'years': AcademicYear.objects.all(), 'year': self.year, 'school': self.school,
            'schools': School.objects.order_by('name') if self.request.user.role in ('OWNER', 'INVESTOR') else [],
            'query': self.request.GET.urlencode(),
            'nav_query': '&'.join(f'{key}={obj.pk}' for key, obj in (('year', self.year), ('school', self.school)) if obj),
        })
        return context

    def get(self, request, *args, **kwargs):
        self.get_filters()
        context = self.get_context_data(**kwargs)
        self.context_for_export = context
        if request.GET.get('export') in ('csv', 'xlsx'):
            return self.export(context)
        return self.render_to_response(context)

    def export_subtitle(self, context):
        school = self.school.name if self.school else _('Вся сеть')
        year = (_('учебный год %(name)s') % {'name': self.year.name}) if self.year and self.report != 'founders' else ''
        month = ''
        data = context.get('data') or {}
        if isinstance(data, dict) and data.get('month') and data.get('month_options'):
            month = dict(data['month_options']).get(data['month'], '')
        stamp = _('выгружено %(date)s') % {'date': timezone.localtime().strftime('%d.%m.%Y %H:%M')}
        return ' · '.join(str(p) for p in (school, year, month, stamp) if p)

    def csv_response(self, filename, headers, rows):
        """Выгрузка отчёта: оформленный .xlsx (кнопка на странице) или простой CSV (?export=csv)."""
        if self.request.GET.get('export') == 'xlsx':
            return xlsx_response(filename, self.export_title, self.export_subtitle(self.context_for_export), headers, rows)
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        stamp = timezone.localdate().strftime('%Y-%m-%d')
        response['Content-Disposition'] = f'attachment; filename="{filename}_{stamp}.csv"'
        response.write('﻿')
        writer = csv.writer(response, delimiter=';')
        writer.writerow(headers)
        for row in rows:
            writer.writerow([_csv_value(v) for v in row])
        return response

    def matrix_csv(self, filename, data):
        headers = [''] + [format_russian_month(m) for m in data['months']] + [_('Итого')]
        rows = [[r.label] + (r.values + [r.total] if r.style != 'section' else []) for r in data['rows']]
        return self.csv_response(filename, headers, rows)


class ReportIndexView(LoginRequiredMixin, View):
    def get(self, request):
        if request.user.role == 'EMPLOYEE':
            return redirect('report_payment_control')
        if request.user.role in ('OWNER', 'DIRECTOR', 'INVESTOR'):
            return redirect('report_dashboard')
        raise Http404


class OldReportRedirectView(LoginRequiredMixin, View):
    """Старые адреса «Прибыль и убытки» / «Движение денег» — теперь это блоки дашборда."""
    def get(self, request):
        query = request.GET.urlencode()
        return redirect(reverse('report_dashboard') + (f'?{query}' if query else ''))


def _rows_csv(rows, labels):
    """Строки Row → строки выгрузки: подпись, значения по месяцам, итог (вид строки — как на странице)."""
    result = []
    for r in rows:
        result.append(Line([r.label] + (r.values + [r.total] if r.style != 'section' else []), r.style))
    return [''] + list(labels) + [_('Итого')], result


class DashboardReportView(ReportMixin, TemplateView):
    export_title = gettext_lazy('Дашборд')
    template_name = 'home/reports/dashboard.html'
    report = 'dashboard'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        investor = self.request.user if self.request.user.role == 'INVESTOR' else None
        context['data'] = reports.dashboard(self.year, self.school, investor)
        return context

    def export(self, context):
        data = context['data']
        section = lambda label: Line([label], 'section')
        rows = [section(_('Прибыль и убытки'))]
        rows += [Line([r['label'], r['amount'], (Percent(r['pct'] / 100) if r['pct'] is not None else None)], r['style']) for r in data['pl']]
        rows += [[], section(_('Доходы и расходы по месяцам')),
                 Line([_('Месяц'), _('Доход'), _('Расходы'), _('Прибыль'), _('Поступило денег')], 'subheader')]
        rows += [[m['label'], m['income'], m['expense'], m['profit'], m['inflow']] for m in data['monthly']]
        t = data['monthly_total']
        rows += [Line([_('Итого'), t['income'], t['expense'], t['profit'], t['inflow']], 'total')]
        rows += [[], section(_('Доходы по источникам'))] + [[r['label'], r['amount']] for r in data['sources']]
        rows += [Line([_('Всего поступило'), data['sources_total']], 'total')]
        rows += [[], section(_('Расходы по статьям'))] + [[r['label'], r['amount']] for r in data['articles']]
        rows += [Line([_('Всего выплачено'), data['articles_total']], 'total')]
        rows += [[], section(_('Движение денег по разделам'))]
        rows += [Line([('− ' if r['negative'] else '') + str(r['label'])] + ([] if r['style'] == 'section' else [r['amount']]),
                      'subheader' if r['style'] == 'section' else r['style']) for r in data['cash_flow']]
        rows += [[], section(_('Банк и касса')),
                 Line([_('Счёт'), _('На начало'), _('Приход'), _('Расход'), _('На конец')], 'subheader')]
        rows += [[r['label'], r['opening'], r['in'], r['out'], r['closing']] for r in data['bank_cash']['rows']]
        b = data['bank_cash']['total']
        rows += [Line([_('Итого'), b['opening'], b['in'], b['out'], b['closing']], 'total')]
        return self.csv_response('dashboard', [_('Показатель'), _('Сумма'), _('% от дохода')], rows)


class ContractSummaryReportView(ReportMixin, TemplateView):
    export_title = gettext_lazy('Свод')
    template_name = 'home/reports/summary.html'
    report = 'summary'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = reports.contract_summary(self.year, self.school, self.request.GET.get('month', '')) if self.year else None
        return context

    def export(self, context):
        data = context['data']
        if data is None:
            raise Http404
        headers, rows = _rows_csv(data['count'], data['labels'])
        _h, sums = _rows_csv(data['sum'], data['labels'])
        return self.csv_response('svod_dogovorov', headers, [Line([_('Количество договоров')], 'section')] + rows + [[],
                                 Line([_('Сумма договоров')], 'section')] + sums)


class CashByMethodReportView(ReportMixin, TemplateView):
    export_title = gettext_lazy('Движение денежных средств')
    template_name = 'home/reports/cash_methods.html'
    report = 'cash_methods'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = reports.cash_by_method(self.year, self.school)
        return context

    def export(self, context):
        headers, rows = _rows_csv(context['data']['rows'], context['data']['labels'])
        return self.csv_response('dvizhenie_ds', headers, rows)


class DebtsMonthReportView(ReportMixin, TemplateView):
    export_title = gettext_lazy('Задолженность за учёбу')
    template_name = 'home/reports/debts_month.html'
    report = 'debts'
    roles = ('OWNER', 'DIRECTOR', 'EMPLOYEE', 'INVESTOR')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = reports.debts_for_month(self.year, self.school, self.request.GET.get('month', '')) if self.year else None
        return context

    def export(self, context):
        data = context['data']
        if data is None:
            raise Http404
        personal = self.request.user.can_view_personal_data
        headers = [_('№ договора'), _('Ученик')] + ([_('Родитель'), _('Телефон')] if personal else []) + \
            [_('Платёж в месяц'), _('Начислено'), _('Оплачено'), _('Задолженность')]
        rows = []
        for r in data['rows']:
            c = r['contract']
            rows.append([c.number, c.student_full_name] + ([c.parents_full_name, c.phone_number] if personal else []) +
                        [c.total_amount, r['accrued'], r['paid'], r['debt']])
        return self.csv_response('zadolzhennost', headers, rows)


class TariffReportView(ReportMixin, TemplateView):
    export_title = gettext_lazy('Свод по актуальным договорам')
    template_name = 'home/reports/tariffs.html'
    report = 'tariffs'
    roles = ('OWNER', 'DIRECTOR', 'EMPLOYEE', 'INVESTOR')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = reports.active_by_tariff(self.year, self.school) if self.year else None
        return context

    def export(self, context):
        data = context['data']
        if data is None:
            raise Http404
        rows = [[r['amount'], r['count'], r['sum']] for r in data['rows']] + [[_('Итого'), data['count'], data['sum']]]
        return self.csv_response('svod_po_tarifam', [_('Сумма в месяц'), _('Договоров'), _('Сумма')], rows)


class MoneyPivotReportView(ReportMixin, TemplateView):
    export_title = gettext_lazy('Поступления и расходы')
    template_name = 'home/reports/money_pivots.html'
    report = 'pivots'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['data'] = reports.money_pivots(self.year, self.school)
        return context

    def export(self, context):
        data = context['data']
        headers, income = _rows_csv(data['income'], data['labels'])
        _h, expense = _rows_csv(data['expense'], data['labels'])
        return self.csv_response('postupleniya_i_rashody', headers, [Line([_('Поступления')], 'section')] + income + [[],
                                 Line([_('Расходы')], 'section')] + expense)


class PaymentControlReportView(ReportMixin, TemplateView):
    export_title = gettext_lazy('Контроль оплаты')
    template_name = 'home/reports/payment_control.html'
    report = 'control'
    roles = ('OWNER', 'DIRECTOR', 'EMPLOYEE', 'INVESTOR')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        status = self.request.GET.get('status', 'all')
        context['status'] = status if status in ('all', 'active', 'cancelled') else 'all'
        context['status_filters'] = [('all', _('Все')), ('active', _('Действующие')), ('cancelled', _('Отменённые'))]
        context['data'] = reports.payment_control(self.year, self.school, context['status']) if self.year else None
        return context

    def export(self, context):
        data = context['data']
        if data is None:
            raise Http404
        headers = [_('№ договора'), _('Ученик'), _('Школа'), _('Статус')]
        for label in data['labels']:
            headers += [f"{label}: {_('начислено')}", f"{label}: {_('оплачено')}", f"{label}: {_('остаток')}"]
        headers += [_('Начислено за год'), _('Оплачено за год'), _('Долг на сегодня')]
        rows = []
        for r in data['rows']:
            c = r['contract']
            row = [c.number, c.student_full_name, c.school.name if c.school else '', c.status()[1]]
            for cell in r['cells']:
                row += [cell['accrued'], cell['paid'], cell['balance']]
            rows.append(row + [r['accrued'], r['paid'], r['debt_now']])
        return self.csv_response('kontrol_oplaty', headers, rows)


class FounderReportView(ReportMixin, TemplateView):
    export_title = gettext_lazy('Расчёты с учредителями')
    template_name = 'home/reports/founders.html'
    report = 'founders'
    roles = ('OWNER', 'INVESTOR')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        investor = self.request.user if self.request.user.role == 'INVESTOR' else None
        context['data'] = reports.founder_settlements(self.school, investor)
        return context

    def export(self, context):
        headers = [_('Учредитель'), _('Вложено'), _('Выплачено'), _('Сальдо')]
        rows = [[r['counterparty'].display_name, r['invested'], r['paid'], r['balance']] for r in context['data']['rows']]
        return self.csv_response('raschety_s_uchreditelyami', headers, rows)


# Загрузка книги школы целиком
class WorkbookImportView(LoginRequiredMixin, OwnerOrDirectorMixin, TemplateView):
    """Книга школы (.xlsx) → проверка всех листов → сохранение → сверка итогов с книгой."""
    template_name = 'home/import/workbook_import.html'
    session_key = 'workbook_import'

    def test_func(self):
        return super().test_func() and (self.request.user.role == 'OWNER' or self.request.user.school_id)

    def form(self, data=None, files=None):
        return WorkbookUploadForm(data, files, user=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault('form', kwargs.get('form') or self.form())
        return context

    def post(self, request):
        action = request.POST.get('action')
        if action == 'cancel':
            request.session.pop(self.session_key, None)
            return redirect('workbook_import')
        if action == 'confirm':
            return self.confirm(request)
        form = self.form(request.POST, request.FILES)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        school = form.cleaned_data.get('school') or request.user.school
        year, prefix = form.cleaned_data['academic_year'], form.cleaned_data['prefix']
        uploaded = form.cleaned_data['file']
        try:
            sheets = workbook_import.read_workbook(uploaded)
        except excel_import.ImportFileError as exc:
            form.add_error('file', str(exc))
            return self.render_to_response(self.get_context_data(form=form))
        plan = workbook_import.build_plan(request.user, school, year, prefix, sheets)
        token = uuid.uuid4().hex
        request.session[self.session_key] = {'token': token, 'school': school.pk, 'year': year.pk, 'prefix': prefix,
                                             'filename': uploaded.name, 'sheets': sheets}
        counts = plan.counts()
        sheet_stats = [(_('Договоры'), counts['contracts']), (_('Поступления'), counts['income']), (_('Расходы'), counts['expense'])]
        return self.render_to_response(self.get_context_data(preview=True, plan=plan, token=token, filename=uploaded.name,
                                                             sheet_stats=sheet_stats))

    def confirm(self, request):
        pending = request.session.get(self.session_key)
        if not pending or pending.get('token') != request.POST.get('token'):
            messages.error(request, _("Эта загрузка уже сохранена или устарела. Загрузите файл ещё раз."))
            return redirect('workbook_import')
        school = get_object_or_404(School, pk=pending['school'])
        if request.user.role != 'OWNER' and school != request.user.school:
            raise Http404
        year = get_object_or_404(AcademicYear, pk=pending['year'])
        plan = workbook_import.build_plan(request.user, school, year, pending['prefix'], pending['sheets'])
        try:
            created = plan.save(pending['filename'])
        except IntegrityError:
            messages.error(request, _("Не удалось сохранить: данные изменились во время загрузки. Загрузите файл ещё раз."))
            return redirect('workbook_import')
        request.session.pop(self.session_key, None)
        recon = workbook_import.reconcile(plan)
        return self.render_to_response(self.get_context_data(
            result=True, plan=plan, created=created, recon=recon, filename=pending['filename'], school=school, year=year))
