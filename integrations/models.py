from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class SyncRun(models.Model):
    """Один запуск обмена с 1С: что загружали, сколько записей и какие были ошибки."""
    ENTITY_CHOICES = [
        ('payments', _('Оплаты родителей')),
        ('payouts', _('Выплаты сотрудникам')),
        ('transactions', _('Прочие приходы и расходы')),
        ('schools', _('Связка школ')),
        ('employees', _('Связка сотрудников')),
        ('contracts', _('Связка договоров')),
    ]
    CHANNEL_CHOICES = [
        ('file', _('Файл')),
        ('odata', _('1С OData')),
        ('api', _('API (1С отправила сама)')),
    ]
    STATUS_CHOICES = [
        ('RUNNING', _('Выполняется')),
        ('SUCCESS', _('Успешно')),
        ('PARTIAL', _('С ошибками')),
        ('FAILED', _('Ошибка')),
    ]

    entity = models.CharField(max_length=20, choices=ENTITY_CHOICES, verbose_name=_("Данные"))
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, verbose_name=_("Канал"))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='RUNNING', verbose_name=_("Статус"))
    dry_run = models.BooleanField(default=False, verbose_name=_("Пробный запуск"))
    started_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Начало"))
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Окончание"))
    total = models.PositiveIntegerField(default=0, verbose_name=_("Записей"))
    created = models.PositiveIntegerField(default=0, verbose_name=_("Создано"))
    updated = models.PositiveIntegerField(default=0, verbose_name=_("Обновлено"))
    reversed = models.PositiveIntegerField(default=0, verbose_name=_("Сторнировано"))
    skipped = models.PositiveIntegerField(default=0, verbose_name=_("Без изменений"))
    failed = models.PositiveIntegerField(default=0, verbose_name=_("С ошибками"))
    errors = models.JSONField(default=list, blank=True, verbose_name=_("Ошибки"))
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+', verbose_name=_("Запустил"))

    class Meta:
        verbose_name=_("Обмен с 1С")
        verbose_name_plural=_("Обмены с 1С")
        ordering = ['-started_at']

    def __str__(self):
        return f"{self.get_entity_display()} · {self.get_channel_display()} · {self.started_at:%d.%m.%Y %H:%M}"
