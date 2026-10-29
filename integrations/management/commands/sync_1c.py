import json
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from integrations.importers import run_import, DOCUMENTS, LINKS
from integrations.onec import OneCError, fetch_records


class Command(BaseCommand):
    help = "Загрузка данных из 1С: из JSON-файла или напрямую через OData."

    def add_arguments(self, parser):
        parser.add_argument('entity', choices=sorted(set(DOCUMENTS) | set(LINKS)),
                            help="Что загружать: payments, payouts, transactions, schools, employees, contracts")
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument('--file', help="JSON-файл со списком записей (формат — docs/1c-integration.md)")
        source.add_argument('--odata', action='store_true', help="Забрать документы из 1С через OData")
        parser.add_argument('--since', help="Для --odata: документы начиная с даты YYYY-MM-DD (по умолчанию 60 дней назад)")
        parser.add_argument('--dry-run', action='store_true', help="Проверить без сохранения")

    def handle(self, *args, entity, file=None, odata=False, since=None, dry_run=False, **options):
        if file:
            try:
                with open(file, encoding='utf-8-sig') as fh:
                    records = json.load(fh)
            except (OSError, ValueError) as exc:
                raise CommandError(f"Не удалось прочитать {file}: {exc}")
            channel = 'file'
        else:
            if since:
                try:
                    since_date = datetime.strptime(since, '%Y-%m-%d').date()
                except ValueError:
                    raise CommandError("--since ожидает дату в формате YYYY-MM-DD")
            else:
                since_date = timezone.localdate() - timedelta(days=60)
            try:
                records = fetch_records(entity, since=since_date)
            except OneCError as exc:
                raise CommandError(str(exc))
            channel = 'odata'

        try:
            run = run_import(entity, records, channel=channel, dry_run=dry_run)
        except ValueError as exc:
            raise CommandError(str(exc))

        prefix = "[ПРОБНЫЙ ЗАПУСК, ничего не сохранено] " if dry_run else ""
        self.stdout.write(
            f"{prefix}{run.get_entity_display()}: всего {run.total}, создано {run.created}, обновлено {run.updated}, "
            f"сторнировано {run.reversed}, без изменений {run.skipped}, ошибок {run.failed}"
        )
        for error in run.errors[:20]:
            self.stdout.write(self.style.WARNING(f"  строка {error.get('row')}: {error.get('error')}"))
        if run.failed > 20:
            self.stdout.write(self.style.WARNING(f"  ...и ещё {run.failed - 20}, полный список — в журнале обменов"))
