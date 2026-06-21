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
    
    # We'll use phone_number instead of username for login if needed, 
    # but for now we'll keep both or just use phone_number for display.
    # The user didn't specify changing the login field.
    
    def __str__(self):
        return f"{self.first_name} - {self.get_role_display()}"
