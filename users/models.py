from datetime import datetime

from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy as _


def shift_month(month_first, delta):
    """Сдвигает дату (1-е число месяца) на delta месяцев."""
    total = month_first.year * 12 + month_first.month - 1 + delta
    return month_first.replace(year=total // 12, month=total % 12 + 1, day=1)


class ExternalSyncMixin(models.Model):
    """Связь записи с объектом 1С.

    external_id — GUID объекта в 1С (Ref_Key). source показывает, где запись создана:
    записи из 1С на платформе только читаются, исправляются они в 1С и приходят при следующей синхронизации.
    """
    SOURCE_MANUAL = 'MANUAL'
    SOURCE_1C = '1C'
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, _('Платформа')),
        (SOURCE_1C, _('1С')),
    ]

    external_id = models.CharField(max_length=64, null=True, blank=True, unique=True, verbose_name=_("ID в 1С"))
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_MANUAL, verbose_name=_("Источник"))
    synced_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Синхронизировано"))

    class Meta:
        abstract = True

    @property
    def is_from_1c(self):
        return self.source == self.SOURCE_1C


class User(AbstractUser, ExternalSyncMixin):
    ROLE_CHOICES = [
        ('OWNER', _('Владелец')),
        ('DIRECTOR', _('Директор')),
        ('EMPLOYEE', _('Сотрудник')),
        ('INVESTOR', _('Инвестор')),
    ]

    phone_number = models.CharField(max_length=20, unique=True, verbose_name=_("Номер телефона"))
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name=_("Роль"))
    school = models.ForeignKey('School', on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("Школа"))

    # We'll use phone_number instead of username for login if needed,
    # but for now we'll keep both or just use phone_number for display.
    # The user didn't specify changing the login field.

    def __str__(self):
        return f"{self.first_name} - {self.get_role_display()}"

    @property
    def masked_phone(self):
        # Format: +998(xx)-***-**-xx
        p = self.phone_number
        if len(p) >= 12:
            return f"{p[:4]}({p[4:6]})-***-**-{p[-2:]}"
        return p

    @property
    def can_view_personal_data(self):
        # Инвесторы видят только финансовые показатели, без персональных данных учеников
        return self.role != 'INVESTOR'

    @property
    def role_priority(self):
        priorities = {
            'OWNER': 1,
            'DIRECTOR': 2,
            'EMPLOYEE': 3,
            'INVESTOR': 4,
        }
        return priorities.get(self.role, 5)

class School(ExternalSyncMixin, models.Model):
    name = models.CharField(max_length=255, verbose_name=_("Название школы"))
    address = models.CharField(max_length=255, verbose_name=_("Адрес"), blank=True)
    director = models.OneToOneField(
        'User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_school',
        verbose_name=_("Директор")
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def employee_count(self):
        return self.user_set.filter(role='EMPLOYEE').count()

    def __str__(self):
        return self.name


class AcademicYear(models.Model):
    """Учебный год сети (у всех школ одинаковый): обычно сентябрь–июнь.

    Договор подписывают заранее (летом), а начисления идут с первого месяца учебного года.
    """
    name = models.CharField(max_length=20, unique=True, verbose_name=_("Название"))
    start_month = models.DateField(verbose_name=_("Первый месяц"))
    end_month = models.DateField(verbose_name=_("Последний месяц"))

    class Meta:
        ordering = ['-start_month']
        verbose_name = _("Учебный год")
        verbose_name_plural = _("Учебные годы")

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.start_month = self.start_month.replace(day=1)
        self.end_month = self.end_month.replace(day=1)
        super().save(*args, **kwargs)

    @property
    def months_count(self):
        return (self.end_month.year - self.start_month.year) * 12 + self.end_month.month - self.start_month.month + 1

    @classmethod
    def for_date(cls, day):
        """Учебный год для договора от этой даты: идущий сейчас или начинающийся в ближайшие 3 месяца.

        Договор от июня относится к году, который начнётся в сентябре.
        """
        month = day.replace(day=1)
        return cls.objects.filter(start_month__lte=shift_month(month, 3), end_month__gte=month).order_by('-start_month').first()


class Contract(ExternalSyncMixin, models.Model):
    MONTH_CHOICES = [
        ('Январь', _('Январь')), ('Февраль', _('Февраль')), ('Март', _('Март')),
        ('Апрель', _('Апрель')), ('Май', _('Май')), ('Июнь', _('Июнь')),
        ('Июль', _('Июль')), ('Август', _('Август')), ('Сентябрь', _('Сентябрь')),
        ('Октябрь', _('Октябрь')), ('Ноябрь', _('Ноябрь')), ('Декабрь', _('Декабрь')),
    ]

    SPECIALIZATION_CHOICES = [
        ('Русский язык', _('Русский язык')),
        ('Узбекский язык', _('Узбекский язык')),
    ]

    number = models.CharField(max_length=100, unique=True, verbose_name=_("№ договора"))
    date = models.DateField(default=timezone.now, verbose_name=_("Дата"))
    month = models.CharField(max_length=20, choices=MONTH_CHOICES, default='Январь', verbose_name=_("Месяц"))
    student_full_name = models.CharField(max_length=255, default="", verbose_name=_("ФИО ученика"))
    student_birth_year = models.IntegerField(default=2000, verbose_name=_("Год рождения"))
    student_class = models.CharField(max_length=50, default="", verbose_name=_("Класс"))
    duration_months = models.IntegerField(default=12, verbose_name=_("Срок, мес"))
    parents_full_name = models.CharField(max_length=255, default="", verbose_name=_("ФИО родителей"))
    passport_data = models.CharField(max_length=100, default="", verbose_name=_("Серия и номер паспорта"))
    inps = models.CharField(max_length=50, default="", verbose_name=_("ИНПС"))
    address = models.CharField(max_length=255, default="", verbose_name=_("Адрес"))
    phone_number = models.CharField(max_length=20, default="", verbose_name=_("№ телефона"))
    specialization = models.CharField(max_length=50, choices=SPECIALIZATION_CHOICES, default='Русский язык', verbose_name=_("Направление"))
    tuition_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_("Стоимость учебы"))
    discount_percentage = models.IntegerField(default=0, verbose_name=_("Скидка, %"))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_("Скидка, сум"))
    food_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_("Стоимость питания"))
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_("Сумма"))
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, null=True, blank=True,
                                      related_name='contracts', verbose_name=_("Учебный год"))
    billing_start = models.DateField(null=True, blank=True, verbose_name=_("Первый месяц обучения"))
    is_cancelled = models.BooleanField(default=False, verbose_name=_("Отметка об отмены договора"))
    cancelled_at = models.DateField(null=True, blank=True, verbose_name=_("Дата отмены договора"))
    notes = models.TextField(blank=True, verbose_name=_("Примечание"))

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='contracts', verbose_name=_("Создал"))
    school = models.ForeignKey(School, on_delete=models.PROTECT, null=True, blank=True, related_name='contracts', verbose_name=_("Школа"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        from decimal import Decimal
        # Calculate discount amount and total amount automatically
        # Ensure we use Decimal for calculations with DecimalFields
        discount_rate = Decimal(self.discount_percentage) / Decimal(100)
        self.discount_amount = self.tuition_fee * discount_rate
        self.total_amount = (self.tuition_fee - self.discount_amount) + self.food_cost
        # Дата отмены может быть неизвестна — тогда начисления идут до последнего оплаченного месяца
        if not self.is_cancelled:
            self.cancelled_at = None
        if isinstance(self.date, datetime):
            # default=timezone.now даёт дату со временем
            self.date = timezone.localtime(self.date).date() if timezone.is_aware(self.date) else self.date.date()
        if self.pk is None and self.academic_year_id is None and self.date:
            self.academic_year = AcademicYear.for_date(self.date)
        if self.billing_start:
            self.billing_start = self.billing_start.replace(day=1)
        elif self.date:
            self.billing_start = self.default_billing_start()
        super().save(*args, **kwargs)

    def default_billing_start(self):
        """Позднее из «начало учебного года» и «месяц договора»: договор от июня → с сентября."""
        month = self.date.replace(day=1)
        if self.academic_year:
            return max(month, self.academic_year.start_month)
        return month

    @property
    def first_billing_month(self):
        return self.billing_start or self.default_billing_start()

    @property
    def last_paid_month(self):
        """Последний месяц, за который есть действующая оплата (учитывает prefetch оплат)."""
        if self.pk is None:
            return None
        months = [p.for_month.replace(day=1) for p in self.payments.all() if not p.is_reversed]
        return max(months) if months else None

    @property
    def cancellation_date_unknown(self):
        return self.is_cancelled and not self.cancelled_at

    @property
    def last_billing_month(self):
        """Последний месяц (1-е число), за который начисляется оплата, или None, если начислений нет.

        Начисления идут duration_months месяцев с первого месяца обучения.
        При отмене договора месяц отмены и следующие уже не начисляются;
        если дата отмены неизвестна — начисления заканчиваются последним оплаченным месяцем.
        """
        if self.duration_months <= 0:
            return None
        last = shift_month(self.first_billing_month, self.duration_months - 1)
        if self.is_cancelled:
            if self.cancelled_at:
                last = min(last, shift_month(self.cancelled_at.replace(day=1), -1))
            else:
                paid = self.last_paid_month
                if paid is None:
                    return None
                last = min(last, paid)
            if last < self.first_billing_month:
                return None
        return last

    def status(self, today=None):
        """(код, подпись): active — действует, cancelled — отменён, finished — срок закончился."""
        month = (today or timezone.localdate()).replace(day=1)
        if self.is_cancelled:
            return ('cancelled', gettext('Отменён'))
        last = self.last_billing_month
        if last is not None and last < month:
            return ('finished', gettext('Завершён'))
        return ('active', gettext('Действует'))

    def is_billable_in(self, month_first):
        last = self.last_billing_month
        return last is not None and self.first_billing_month <= month_first <= last

    def __str__(self):
        return f"{self.number} - {self.student_full_name}"


class ActiveManager(models.Manager):
    """Только действующие (не сторнированные) записи."""
    def get_queryset(self):
        return super().get_queryset().filter(is_reversed=False)


class FinancialRecord(ExternalSyncMixin, models.Model):
    """Общие поля денежных записей: кто внёс и сторно вместо удаления."""
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name=_("Кто внёс"))
    is_reversed = models.BooleanField(default=False, verbose_name=_("Сторнировано"))
    reversed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Дата сторно"))
    reversed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name=_("Кто сторнировал"))
    reversal_reason = models.CharField(max_length=255, blank=True, verbose_name=_("Причина сторно"))
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager()
    active = ActiveManager()

    class Meta:
        abstract = True

    def reverse(self, user, reason):
        self.is_reversed = True
        self.reversed_at = timezone.now()
        self.reversed_by = user
        self.reversal_reason = reason
        self.save(update_fields=['is_reversed', 'reversed_at', 'reversed_by', 'reversal_reason', 'updated_at'])


class Payment(FinancialRecord):
    METHOD_CHOICES = [
        ('CASH', _('Наличные')),
        ('CARD', _('Терминал / карта')),
        ('CLICK', 'Click'),
        ('PAYME', 'Payme'),
        ('UZUM', 'Uzum'),
        ('BANK', _('Перечисление на счёт')),
    ]

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name='payments', verbose_name=_("Договор"))
    for_month = models.DateField(verbose_name=_("За месяц (1-е число)"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_("Сумма оплаты"))
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='CASH', verbose_name=_("Способ оплаты"))
    payment_date = models.DateField(default=timezone.now, verbose_name=_("Дата оплаты"))
    notes = models.TextField(blank=True, verbose_name=_("Примечание"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name=_("Оплата")
        verbose_name_plural=_("Оплаты")
        ordering = ['-payment_date']

    def __str__(self):
        return gettext("Оплата %(student)s за %(month)s") % {
            'student': self.contract.student_full_name, 'month': self.for_month.strftime('%m/%Y')}

class EmployeePayout(FinancialRecord):
    employee = models.ForeignKey(User, on_delete=models.PROTECT, limit_choices_to={'role': 'EMPLOYEE'}, related_name='employee_payouts', verbose_name=_("Сотрудник"))
    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name='employee_payouts', verbose_name=_("Школа"))
    for_month = models.DateField(verbose_name=_("За месяц (1-е число)"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_("Сумма"))
    payout_date = models.DateField(default=timezone.now, verbose_name=_("Дата выплаты"))
    notes = models.TextField(blank=True, verbose_name=_("Примечание"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name=_("Выплата сотруднику")
        verbose_name_plural=_("Выплаты сотрудникам")
        ordering = ['-payout_date']

    def __str__(self):
        return gettext("%(employee)s - %(amount)s за %(month)s") % {
            'employee': self.employee.first_name, 'amount': self.amount, 'month': self.for_month.strftime('%m/%Y')}

class Transaction(FinancialRecord):
    TYPE_CHOICES = [
        ('INCOME', _('Приход / Инвестиция')),
        ('EXPENSE', _('Расход / Непредвиденные траты')),
    ]
    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name='transactions', verbose_name=_("Школа"))
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, verbose_name=_("Тип операции"))
    category = models.CharField(max_length=100, blank=True, verbose_name=_("Категория/Назначение"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_("Сумма"))
    date = models.DateField(default=timezone.now, verbose_name=_("Дата"))
    notes = models.TextField(blank=True, verbose_name=_("Примечание / Описание / От кого"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name=_("Транзакция")
        verbose_name_plural=_("Транзакции")
        ordering = ['-date']

    def __str__(self):
        return f"{self.get_type_display()} - {self.amount} ({self.date})"


class AuditLog(models.Model):
    ACTION_CHOICES = [
        ('CREATE', _('Создание')),
        ('UPDATE', _('Изменение')),
        ('REVERSE', _('Сторно')),
        ('DELETE', _('Удаление')),
    ]
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs', verbose_name=_("Пользователь"))
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, verbose_name=_("Действие"))
    model_name = models.CharField(max_length=100, verbose_name=_("Объект"))
    object_id = models.CharField(max_length=64, verbose_name=_("ID объекта"))
    object_repr = models.CharField(max_length=255, blank=True, verbose_name=_("Описание"))
    changes = models.JSONField(default=dict, blank=True, verbose_name=_("Изменения"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Когда"))

    class Meta:
        verbose_name=_("Журнал действий")
        verbose_name_plural=_("Журнал действий")
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_action_display()} {self.model_name} #{self.object_id}"

    @classmethod
    def record(cls, user, action, obj, changes=None):
        return cls.objects.create(
            user=user if user and user.is_authenticated else None,
            action=action,
            model_name=obj._meta.verbose_name,
            object_id=str(obj.pk),
            object_repr=str(obj)[:255],
            changes=changes or {},
        )
