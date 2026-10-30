#!/usr/bin/env bash
# Резервная копия базы SchoolPR. Запускается каждую ночь таймером schoolpr-backup.timer.
# Копии лежат в /srv/schoolpr/backups и хранятся KEEP_DAYS дней.
set -euo pipefail

APP_DIR=/srv/schoolpr/app
BACKUP_DIR=/srv/schoolpr/backups
KEEP_DAYS=${KEEP_DAYS:-14}

# Берём параметры подключения из .env приложения
env_value() { grep -E "^$1=" "$APP_DIR/.env" | tail -n1 | cut -d= -f2-; }
export PGPASSWORD="$(env_value DB_PASSWORD)"
DB_NAME="$(env_value DB_NAME)"
DB_USER="$(env_value DB_USER)"
DB_HOST="$(env_value DB_HOST)"
DB_PORT="$(env_value DB_PORT)"

mkdir -p "$BACKUP_DIR"
file="$BACKUP_DIR/schoolpr-$(date +%Y-%m-%d_%H%M).dump"
pg_dump --format=custom --no-owner -h "${DB_HOST:-localhost}" -p "${DB_PORT:-5432}" -U "$DB_USER" -f "$file.tmp" "$DB_NAME"
mv "$file.tmp" "$file"
chmod 600 "$file"
find "$BACKUP_DIR" -name 'schoolpr-*.dump' -mtime +"$KEEP_DAYS" -delete
echo "Резервная копия готова: $file ($(du -h "$file" | cut -f1))"
