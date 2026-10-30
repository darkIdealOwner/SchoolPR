#!/usr/bin/env bash
# Обновление сайта до последней версии из GitHub. Запускать на сервере: sudo /srv/schoolpr/app/deploy/update.sh
set -euo pipefail

APP_DIR=/srv/schoolpr/app
as_app() { sudo -u schoolpr -H bash -c "cd $APP_DIR && $1"; }

echo "1/6 Резервная копия базы перед обновлением"
sudo -u schoolpr "$APP_DIR/deploy/backup_db.sh"
echo "2/6 Забираю код из GitHub"
as_app "git pull --ff-only"
echo "3/6 Ставлю зависимости"
as_app "/srv/schoolpr/venv/bin/pip install -q -r requirements.txt"
echo "4/6 Обновляю базу"
as_app "/srv/schoolpr/venv/bin/python manage.py migrate --noinput"
echo "5/6 Собираю статику"
as_app "/srv/schoolpr/venv/bin/python manage.py collectstatic --noinput"
echo "6/6 Перезапускаю сайт"
systemctl reload schoolpr
sleep 2
if curl -fsS --unix-socket /run/schoolpr/gunicorn.sock http://localhost/healthz >/dev/null; then
    echo "Готово, сайт работает."
else
    echo "Сайт не отвечает! Смотрите логи: journalctl -u schoolpr -n 50" >&2
    exit 1
fi
