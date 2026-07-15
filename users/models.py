from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone

class User(AbstractUser):
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
    def role_priority(self):
        priorities = {
            'OWNER': 1,
            'DIRECTOR': 2,
            'EMPLOYEE': 3,
            'INVESTOR': 4,
        }
        return priorities.get(self.role, 5)

class School(models.Model):
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

    @property
    def employee_count(self):
        return self.user_set.filter(role='EMPLOYEE').count()

    def __str__(self):
        return self.name

class Contract(models.Model):
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

    number = models.CharField(max_length=100, verbose_name="№ договора")
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
    notes = models.TextField(blank=True, verbose_name="Примечание")
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='contracts', verbose_name="Пользователь")
    school = models.ForeignKey(School, on_delete=models.SET_NULL, null=True, blank=True, related_name='contracts', verbose_name="Школа")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        from decimal import Decimal
        # Calculate discount amount and total amount automatically
        # Ensure we use Decimal for calculations with DecimalFields
        discount_rate = Decimal(self.discount_percentage) / Decimal(100)
        self.discount_amount = self.tuition_fee * discount_rate
        self.total_amount = (self.tuition_fee - self.discount_amount) + self.food_cost
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.number} - {self.student_full_name}"

class Payment(models.Model):
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name='payments', verbose_name="Договор")
    for_month = models.DateField(verbose_name="За месяц (1-е число)")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Сумма оплаты")
    payment_date = models.DateField(default=timezone.now, verbose_name="Дата оплаты")
    notes = models.TextField(blank=True, verbose_name="Примечание")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Выплата"
        verbose_name_plural = "Выплаты"
        ordering = ['-payment_date']

    def __str__(self):
        return f"Оплата {self.contract.student_full_name} за {self.for_month.strftime('%B %Y')}"
