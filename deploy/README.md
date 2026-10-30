# Выкладка SchoolPR на сервер (ServerPlus, Ubuntu 24.04)

Инструкция рассчитана на облачный сервер ServerPlus (дата-центр в Ташкенте), но подойдёт для любого VPS с Ubuntu 24.04.
Команды в блоках копируются целиком. `example.uz` везде меняйте на свой домен, `IP_СЕРВЕРА` — на адрес сервера.

Что получится:

```
Интернет ──HTTPS──► nginx ──► gunicorn (Django) ──► PostgreSQL 18
                      │
                      └─ сертификат Let's Encrypt (бесплатный, продлевается сам)
Каждую ночь в 3:00 — резервная копия базы. Каждый час — обмен с 1С (когда включите).
```

---

## 0. Что подготовить заранее

1. **SSH-ключ** — «ключ от двери» сервера, без пароля. На своём компьютере (PowerShell):

   ```powershell
   ssh-keygen -t ed25519 -C "schoolpr"
   ```

   На все вопросы можно нажимать Enter. Появятся два файла в `C:\Users\User\.ssh\`:
   `id_ed25519` (секретный, никому не отдавать) и `id_ed25519.pub` (публичный — его отдаём ServerPlus).
   Показать публичный ключ:

   ```powershell
   Get-Content $HOME\.ssh\id_ed25519.pub
   ```

2. **Домен** (например, `schoolpr.uz`) — нужен для HTTPS. Купить у любого регистратора `.uz`.
   В настройках DNS домена создать запись **A** на IP сервера (появится после шага 1). Пока домена нет,
   сайт можно открыть по IP, но без HTTPS — только для проверки, не для работы.

## 1. Создать сервер в ServerPlus

В личном кабинете → Облачные серверы → Создать:

- Образ: **Ubuntu 24.04**
- Конфигурация: **2 vCPU, 2–4 ГБ RAM, 30 ГБ NVMe** (для старта хватит, потом можно увеличить без переустановки)
- Публичный IP: **да**
- SSH-ключ: вставить содержимое `id_ed25519.pub`

Оплата почасовая: сервер списывает баланс, пока существует, даже выключенный.

## 2. Подключиться

```powershell
ssh root@IP_СЕРВЕРА
```

(Если ServerPlus создал пользователя `ubuntu`, то `ssh ubuntu@IP_СЕРВЕРА`, а команды ниже выполнять через `sudo -i`.)

## 3. Установить программы

```bash
apt update && apt -y upgrade
apt -y install git nginx python3-venv python3-dev build-essential curl ufw certbot python3-certbot-nginx postgresql-common
# PostgreSQL 18 из официального репозитория (в Ubuntu по умолчанию 16 — не прочитает копию базы с вашего компьютера)
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
apt -y install postgresql-18
timedatectl set-timezone Asia/Tashkent
```

Брандмауэр — открыть только SSH и веб:

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
```

## 4. Пользователь и код

Сайт работает от отдельного пользователя `schoolpr` без прав администратора.

```bash
adduser --system --group --home /srv/schoolpr --shell /bin/bash schoolpr
sudo -u schoolpr git clone -b master https://github.com/darkIdealOwner/SchoolPR /srv/schoolpr/app
sudo -u schoolpr python3 -m venv /srv/schoolpr/venv
sudo -u schoolpr /srv/schoolpr/venv/bin/pip install -r /srv/schoolpr/app/requirements.txt
chmod +x /srv/schoolpr/app/deploy/*.sh
```

Если репозиторий на GitHub **приватный**, `git clone` попросит доступ. Проще всего — «deploy key»:
`sudo -u schoolpr ssh-keygen -t ed25519 -f /srv/schoolpr/.ssh/id_ed25519 -N ""`, содержимое
`/srv/schoolpr/.ssh/id_ed25519.pub` добавить на GitHub (репозиторий → Settings → Deploy keys, только чтение),
и клонировать по адресу `git@github.com:darkIdealOwner/SchoolPR.git`.

## 5. База данных

Придумать пароль для базы (или сгенерировать):

```bash
openssl rand -hex 24
```

Создать пользователя и базу (вместо `ПАРОЛЬ_БАЗЫ` — сгенерированный пароль):

```bash
sudo -u postgres psql -c "CREATE ROLE schoolpr LOGIN PASSWORD 'ПАРОЛЬ_БАЗЫ';"
sudo -u postgres psql -c "CREATE DATABASE schoolpr OWNER schoolpr ENCODING 'UTF8' TEMPLATE template0;"
```

## 6. Настройки (.env)

Сгенерировать секретный ключ Django:

```bash
/srv/schoolpr/venv/bin/python -c "import secrets; print(secrets.token_urlsafe(50))"
```

Создать файл настроек:

```bash
sudo -u schoolpr nano /srv/schoolpr/app/.env
```

Содержимое (подставить свои значения):

```
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=сгенерированный_ключ
DJANGO_ALLOWED_HOSTS=example.uz,www.example.uz,localhost
DJANGO_CSRF_TRUSTED_ORIGINS=https://example.uz,https://www.example.uz

# Пока нет HTTPS (шаг 9) — эти две строки оставить, после certbot — удалить
DJANGO_SSL_REDIRECT=False
DJANGO_SECURE_COOKIES=False

DB_ENGINE=django.db.backends.postgresql
DB_NAME=schoolpr
DB_USER=schoolpr
DB_PASSWORD=ПАРОЛЬ_БАЗЫ
DB_HOST=localhost
DB_PORT=5432

# Интеграция с 1С (когда будет готова, см. docs/1c-integration.md)
# ONEC_ODATA_URL=
# ONEC_USER=
# ONEC_PASSWORD=
# ONEC_API_TOKEN=
```

Сохранить: `Ctrl+O`, Enter, `Ctrl+X`. Закрыть файл от чужих глаз:

```bash
chmod 600 /srv/schoolpr/app/.env
```

Если домена ещё нет — в `DJANGO_ALLOWED_HOSTS` вместо домена указать IP сервера.

## 7. Данные: перенести с компьютера или начать с нуля

### Вариант А — перенести текущую базу с компьютера (рекомендуется)

На **своём компьютере** (PowerShell, в папке проекта) — снять копию локальной базы. Пароль пользователя
`schoolpr` лежит в локальном `.env` (`DB_PASSWORD`):

```powershell
& "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" --format=custom --no-owner -h localhost -U schoolpr -f schoolpr.dump schoolpr
scp schoolpr.dump root@IP_СЕРВЕРА:/tmp/
Remove-Item schoolpr.dump
```

На **сервере** — восстановить и применить новые миграции:

```bash
sudo -u postgres pg_restore --no-owner --role=schoolpr -d schoolpr /tmp/schoolpr.dump
rm /tmp/schoolpr.dump
cd /srv/schoolpr/app
sudo -u schoolpr /srv/schoolpr/venv/bin/python manage.py migrate --noinput
```

### Вариант Б — пустая база

```bash
cd /srv/schoolpr/app
sudo -u schoolpr /srv/schoolpr/venv/bin/python manage.py migrate --noinput
```

### ⚠️ В любом варианте: пароли

Тестовые пользователи (`owner`, `director`, `employee`, `investor`) создаются миграцией с паролем, который
записан в коде на GitHub. **До открытия сайта людям** войдите владельцем и в «Управление пользователями»
задайте всем новые надёжные пароли (или удалите ненужных тестовых пользователей). Своему пользователю —
через «Профиль → Сменить пароль».

## 8. Запуск сайта

```bash
cd /srv/schoolpr/app
sudo -u schoolpr /srv/schoolpr/venv/bin/python manage.py collectstatic --noinput
sudo -u schoolpr /srv/schoolpr/venv/bin/python manage.py check --deploy

cp deploy/schoolpr.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now schoolpr

cp deploy/nginx-schoolpr.conf /etc/nginx/sites-available/schoolpr
sed -i 's/example.uz/ВАШ_ДОМЕН/g' /etc/nginx/sites-available/schoolpr
ln -s /etc/nginx/sites-available/schoolpr /etc/nginx/sites-enabled/schoolpr
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

Проверка: `curl -s http://localhost/healthz -H "Host: ВАШ_ДОМЕН"` должен ответить `{"status": "ok"}`.
Открыть в браузере `http://ВАШ_ДОМЕН` (или `http://IP_СЕРВЕРА`, если домена нет).

## 9. HTTPS

Когда DNS-запись домена указывает на сервер:

```bash
certbot --nginx -d example.uz -d www.example.uz --redirect -m ваша@почта --agree-tos
```

Затем в `.env` **удалить** строки `DJANGO_SSL_REDIRECT=False` и `DJANGO_SECURE_COOKIES=False` и перезапустить:

```bash
systemctl restart schoolpr
```

Сертификат продлевается автоматически. Через неделю-другую стабильной работы можно добавить в `.env`
`DJANGO_HSTS_SECONDS=31536000` — браузеры будут всегда ходить на сайт только по HTTPS.

## 10. Резервные копии

```bash
cp /srv/schoolpr/app/deploy/schoolpr-backup.service /srv/schoolpr/app/deploy/schoolpr-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now schoolpr-backup.timer
systemctl start schoolpr-backup.service      # первая копия прямо сейчас
ls -lh /srv/schoolpr/backups
```

Копии хранятся 14 дней в `/srv/schoolpr/backups`. Совет: раз в неделю скачивайте свежую копию к себе
(`scp root@IP_СЕРВЕРА:/srv/schoolpr/backups/<файл>.dump .`) или настройте выгрузку в S3-хранилище ServerPlus —
копия на том же сервере не спасёт, если пропадёт сам сервер.

Восстановление из копии:

```bash
systemctl stop schoolpr
sudo -u postgres dropdb schoolpr
sudo -u postgres createdb -O schoolpr schoolpr
sudo -u postgres pg_restore --no-owner --role=schoolpr -d schoolpr /srv/schoolpr/backups/<файл>.dump
systemctl start schoolpr
```

## 11. Обмен с 1С по расписанию (когда настроите OData)

После того как в `.env` заполнены `ONEC_ODATA_URL`, `ONEC_USER`, `ONEC_PASSWORD` и пробная загрузка прошла
(`sudo -u schoolpr /srv/schoolpr/venv/bin/python manage.py sync_1c payments --odata --dry-run`):

```bash
cp /srv/schoolpr/app/deploy/schoolpr-sync-1c.service /srv/schoolpr/app/deploy/schoolpr-sync-1c.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now schoolpr-sync-1c.timer
```

Обмен будет идти каждый час с 7:05 до 22:05. Результаты — на странице «Интеграция с 1С» и в
`journalctl -u schoolpr-sync-1c`.

## Обновление сайта

После того как новые изменения отправлены на GitHub:

```bash
sudo /srv/schoolpr/app/deploy/update.sh
```

Скрипт сам сделает резервную копию, заберёт код, поставит зависимости, обновит базу, соберёт статику,
перезапустит сайт и проверит, что он отвечает.

## Если что-то не работает

| Что видно | Где смотреть |
|---|---|
| 502 Bad Gateway | сайт не запущен: `systemctl status schoolpr`, `journalctl -u schoolpr -n 50` |
| 400 Bad Request | домена или IP нет в `DJANGO_ALLOWED_HOSTS` |
| 403 CSRF при входе | адреса сайта нет в `DJANGO_CSRF_TRUSTED_ORIGINS` (с `https://`) |
| Сайт без стилей | не выполнен `collectstatic` (шаг 8) |
| Бесконечная переадресация | HTTPS ещё не настроен, а `DJANGO_SSL_REDIRECT=False` удалён из `.env` |
| Ошибки nginx | `nginx -t`, `tail -n 50 /var/log/nginx/error.log` |
