from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction

from users.models import Contract, EmployeePayout, Payment, School, Transaction, User

DEMO_PREFIX = 'DEMO-'


class Command(BaseCommand):
    help = ("Убирает пробные данные из samples/1c: удаляет оплаты, выплаты и транзакции с кодом DEMO-… "
            "и отвязывает DEMO-коды от школ, сотрудников и договоров. Журнал обменов остаётся как история. "
            "Без --yes только показывает, что будет сделано.")

    def add_arguments(self, parser):
        parser.add_argument('--yes', action='store_true', help="Выполнить очистку")

    def handle(self, *args, yes=False, **options):
        documents = [(Payment, 'Оплаты'), (EmployeePayout, 'Выплаты сотрудникам'), (Transaction, 'Транзакции')]
        links = [(School, 'Школы'), (User, 'Сотрудники'), (Contract, 'Договоры')]

        for model, label in documents:
            count = model.objects.filter(external_id__startswith=DEMO_PREFIX).count()
            self.stdout.write(f"{label}: удалить {count}")
        for model, label in links:
            count = model.objects.filter(external_id__startswith=DEMO_PREFIX).count()
            self.stdout.write(f"{label}: отвязать от 1С {count}")

        if not yes:
            self.stdout.write(self.style.WARNING("Ничего не изменено. Запустите с --yes, чтобы выполнить очистку."))
            return

        with db_transaction.atomic():
            for model, _ in documents:
                model.objects.filter(external_id__startswith=DEMO_PREFIX).delete()
            for model, _ in links:
                model.objects.filter(external_id__startswith=DEMO_PREFIX).update(
                    external_id=None, source=model.SOURCE_MANUAL, synced_at=None)
        self.stdout.write(self.style.SUCCESS("Пробные данные 1С удалены."))
