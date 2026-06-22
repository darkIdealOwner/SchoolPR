from django.db import models
from django.contrib.auth.models import AbstractUser

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
    number = models.CharField(max_length=100, verbose_name="Номер договора")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='contracts', verbose_name="Пользователь")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.number
