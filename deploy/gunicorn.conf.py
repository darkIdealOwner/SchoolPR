# Настройки gunicorn — веб-сервера, который запускает Django на сервере.
import multiprocessing

bind = 'unix:/run/schoolpr/gunicorn.sock'
# 2 воркера на ядро + 1 — стандартная формула; для небольшого VPS этого достаточно
workers = min(multiprocessing.cpu_count() * 2 + 1, 5)
timeout = 120          # загрузка большого файла из 1С может идти дольше минуты
graceful_timeout = 30
max_requests = 1000    # периодически перезапускать воркеры — защита от утечек памяти
max_requests_jitter = 100
accesslog = '-'
errorlog = '-'
