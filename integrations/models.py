from django.conf import settings
from django.db import models


class SyncRun(models.Model):
    """Один запуск обмена с 1С: что загружали, сколько записей и какие были ошибки."""
    ENTITY_CHOICES = [
        ('payments', 'Оплаты родителей'),
        ('payouts', 'Выплаты сотрудникам'),
        ('transactions', 'Прочие приходы и расходы'),
        ('schools', 'Связка школ'),
        ('employees', 'Связка сотрудников'),
        ('contracts', 'Связка договоров'),
    ]
    CHANNEL_CHOICES = [
        ('file', 'Файл'),
        ('odata', '1С OData'),
        ('api', 'API (1С отправила сама)'),
    ]
    STATUS_CHOICES = [
        ('RUNNING', 'Выполняется'),
        ('SUCCESS', 'Успешно'),
        ('PARTIAL', 'С ошибками'),
        ('FAILED', 'Ошибка'),
    ]

    entity = models.CharField(max_length=20, choices=ENTITY_CHOICES, verbose_name="Данные")
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, verbose_name="Канал")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='RUNNING', verbose_name="Статус")
    dry_run = models.BooleanField(default=False, verbose_name="Пробный запуск")
    started_at = models.DateTimeField(auto_now_add=True, verbose_name="Начало")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Окончание")
    total = models.PositiveIntegerField(default=0, verbose_name="Записей")
    created = models.PositiveIntegerField(default=0, verbose_name="Создано")
    updated = models.PositiveIntegerField(default=0, verbose_name="Обновлено")
    reversed = models.PositiveIntegerField(default=0, verbose_name="Сторнировано")
    skipped = models.PositiveIntegerField(default=0, verbose_name="Без изменений")
    failed = models.PositiveIntegerField(default=0, verbose_name="С ошибками")
    errors = models.JSONField(default=list, blank=True, verbose_name="Ошибки")
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+', verbose_name="Запустил")

    class Meta:
        verbose_name = "Обмен с 1С"
        verbose_name_plural = "Обмены с 1С"
        ordering = ['-started_at']

    def __str__(self):
        return f"{self.get_entity_display()} · {self.get_channel_display()} · {self.started_at:%d.%m.%Y %H:%M}"
