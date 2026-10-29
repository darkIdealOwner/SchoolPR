from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone


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
        (SOURCE_MANUAL, 'Платформа'),
        (SOURCE_1C, '1С'),
    ]

    external_id = models.CharField(max_length=64, null=True, blank=True, unique=True, verbose_name="ID в 1С")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_MANUAL, verbose_name="Источник")
    synced_at = models.DateTimeField(null=True, blank=True, verbose_name="Синхронизировано")

    class Meta:
        abstract = True

    @property
    def is_from_1c(self):
        return self.source == self.SOURCE_1C


class User(AbstractUser, ExternalSyncMixin):
    ROLE_CHOICES = [
        ('OWNER', 'Владелец'),
        ('DIRECTOR', 'Директор'),
        ('EMPLOYEE', 'Сотрудник'),
        ('INVESTOR', 'Инвестор'),
    ]

    phone_number = models.CharField(max_length=20, unique=True, verbose_name="Номер телефона")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name="Роль")
    school = models.ForeignKey('School', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Школа")

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
    name = models.CharField(max_length=255, verbose_name="Название школы")
    address = models.CharField(max_length=255, verbose_name="Адрес", blank=True)
    director = models.OneToOneField(
        'User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_school',
        verbose_name="Директор"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def employee_count(self):
        return self.user_set.filter(role='EMPLOYEE').count()

    def __str__(self):
        return self.name

class Contract(ExternalSyncMixin, models.Model):
    MONTH_CHOICES = [
        ('Январь', 'Январь'), ('Февраль', 'Февраль'), ('Март', 'Март'),
        ('Апрель', 'Апрель'), ('Май', 'Май'), ('Июнь', 'Июнь'),
        ('Июль', 'Июль'), ('Август', 'Август'), ('Сентябрь', 'Сентябрь'),
        ('Октябрь', 'Октябрь'), ('Ноябрь', 'Ноябрь'), ('Декабрь', 'Декабрь'),
    ]

    SPECIALIZATION_CHOICES = [
        ('Русский язык', 'Русский язык'),
        ('Узбекский язык', 'Узбекский язык'),
    ]

    number = models.CharField(max_length=100, unique=True, verbose_name="№ договора")
    date = models.DateField(default=timezone.now, verbose_name="Дата")
    month = models.CharField(max_length=20, choices=MONTH_CHOICES, default='Январь', verbose_name="Месяц")
    student_full_name = models.CharField(max_length=255, default="", verbose_name="ФИО ученика")
    student_birth_year = models.IntegerField(default=2000, verbose_name="Год рождения")
    student_class = models.CharField(max_length=50, default="", verbose_name="Класс")
    duration_months = models.IntegerField(default=12, verbose_name="Срок, мес")
    parents_full_name = models.CharField(max_length=255, default="", verbose_name="ФИО родителей")
    passport_data = models.CharField(max_length=100, default="", verbose_name="Серия и номер паспорта")
    inps = models.CharField(max_length=50, default="", verbose_name="ИНПС")
    address = models.CharField(max_length=255, default="", verbose_name="Адрес")
    phone_number = models.CharField(max_length=20, default="", verbose_name="№ телефона")
    specialization = models.CharField(max_length=50, choices=SPECIALIZATION_CHOICES, default='Русский язык', verbose_name="Направление")
    tuition_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Стоимость учебы")
    discount_percentage = models.IntegerField(default=0, verbose_name="Скидка, %")
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Скидка, сум")
    food_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Стоимость питания")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Сумма")
    is_cancelled = models.BooleanField(default=False, verbose_name="Отметка об отмены договора")
    cancelled_at = models.DateField(null=True, blank=True, verbose_name="Дата отмены договора")
    notes = models.TextField(blank=True, verbose_name="Примечание")

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='contracts', verbose_name="Создал")
    school = models.ForeignKey(School, on_delete=models.PROTECT, null=True, blank=True, related_name='contracts', verbose_name="Школа")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        from decimal import Decimal
        # Calculate discount amount and total amount automatically
        # Ensure we use Decimal for calculations with DecimalFields
        discount_rate = Decimal(self.discount_percentage) / Decimal(100)
        self.discount_amount = self.tuition_fee * discount_rate
        self.total_amount = (self.tuition_fee - self.discount_amount) + self.food_cost
        if self.is_cancelled and not self.cancelled_at:
            self.cancelled_at = timezone.localdate()
        if not self.is_cancelled:
            self.cancelled_at = None
        super().save(*args, **kwargs)

    @property
    def first_billing_month(self):
        return self.date.replace(day=1)

    @property
    def last_billing_month(self):
        """Последний месяц (1-е число), за который начисляется оплата, или None, если начислений нет.

        Начисления идут duration_months месяцев с месяца договора.
        При отмене договора месяц отмены и следующие уже не начисляются.
        """
        if self.duration_months <= 0:
            return None
        last = shift_month(self.first_billing_month, self.duration_months - 1)
        if self.is_cancelled:
            if not self.cancelled_at:
                return None
            last = min(last, shift_month(self.cancelled_at.replace(day=1), -1))
            if last < self.first_billing_month:
                return None
        return last

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
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name="Кто внёс")
    is_reversed = models.BooleanField(default=False, verbose_name="Сторнировано")
    reversed_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата сторно")
    reversed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name="Кто сторнировал")
    reversal_reason = models.CharField(max_length=255, blank=True, verbose_name="Причина сторно")
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
        ('CASH', 'Наличные'),
        ('CARD', 'Терминал / карта'),
        ('CLICK', 'Click'),
        ('PAYME', 'Payme'),
        ('UZUM', 'Uzum'),
        ('BANK', 'Перечисление на счёт'),
    ]

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name='payments', verbose_name="Договор")
    for_month = models.DateField(verbose_name="За месяц (1-е число)")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Сумма оплаты")
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='CASH', verbose_name="Способ оплаты")
    payment_date = models.DateField(default=timezone.now, verbose_name="Дата оплаты")
    notes = models.TextField(blank=True, verbose_name="Примечание")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Оплата"
        verbose_name_plural = "Оплаты"
        ordering = ['-payment_date']

    def __str__(self):
        return f"Оплата {self.contract.student_full_name} за {self.for_month.strftime('%m/%Y')}"

class EmployeePayout(FinancialRecord):
    employee = models.ForeignKey(User, on_delete=models.PROTECT, limit_choices_to={'role': 'EMPLOYEE'}, related_name='employee_payouts', verbose_name="Сотрудник")
    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name='employee_payouts', verbose_name="Школа")
    for_month = models.DateField(verbose_name="За месяц (1-е число)")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Сумма")
    payout_date = models.DateField(default=timezone.now, verbose_name="Дата выплаты")
    notes = models.TextField(blank=True, verbose_name="Примечание")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Выплата сотруднику"
        verbose_name_plural = "Выплаты сотрудникам"
        ordering = ['-payout_date']

    def __str__(self):
        return f"{self.employee.first_name} - {self.amount} за {self.for_month.strftime('%m/%Y')}"

class Transaction(FinancialRecord):
    TYPE_CHOICES = [
        ('INCOME', 'Приход / Инвестиция'),
        ('EXPENSE', 'Расход / Непредвиденные траты'),
    ]
    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name='transactions', verbose_name="Школа")
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, verbose_name="Тип операции")
    category = models.CharField(max_length=100, blank=True, verbose_name="Категория/Назначение")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Сумма")
    date = models.DateField(default=timezone.now, verbose_name="Дата")
    notes = models.TextField(blank=True, verbose_name="Примечание / Описание / От кого")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Транзакция"
        verbose_name_plural = "Транзакции"
        ordering = ['-date']

    def __str__(self):
        return f"{self.get_type_display()} - {self.amount} ({self.date})"


class AuditLog(models.Model):
    ACTION_CHOICES = [
        ('CREATE', 'Создание'),
        ('UPDATE', 'Изменение'),
        ('REVERSE', 'Сторно'),
        ('DELETE', 'Удаление'),
    ]
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs', verbose_name="Пользователь")
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, verbose_name="Действие")
    model_name = models.CharField(max_length=100, verbose_name="Объект")
    object_id = models.CharField(max_length=64, verbose_name="ID объекта")
    object_repr = models.CharField(max_length=255, blank=True, verbose_name="Описание")
    changes = models.JSONField(default=dict, blank=True, verbose_name="Изменения")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Когда")

    class Meta:
        verbose_name = "Журнал действий"
        verbose_name_plural = "Журнал действий"
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
