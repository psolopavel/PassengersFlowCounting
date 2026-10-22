# Passengers Flow Counting

Система для подсчёта пассажиров в салоне: панель менеджера и интерфейс считальщиков.

**Стек:** HTML + CSS + Vanilla JS → nginx → FastAPI (Python) → MySQL

## Структура проекта

```
PassengersFlowCounting/
├── app/
│   ├── main.py            # точка входа FastAPI, health-check
│   ├── config.py          # настройки из .env
│   ├── database.py        # подключение к двум БД (app + routes)
│   ├── models/            # SQLAlchemy-модели (далее)
│   ├── routers/           # API-эндпоинты (далее)
│   ├── services/          # бизнес-логика, демоны (далее)
│   ├── templates/         # HTML (Jinja2)
│   │   └── index.html
│   └── static/            # CSS / JS
│       ├── css/style.css
│       └── js/app.js
├── deploy/
│   ├── nginx.conf         # конфиг nginx (порт 8083)
│   └── passengersflow.service   # systemd unit
├── requirements.txt
├── .env.example
└── README.md
```

Две БД:
- **passengers_flow** — новая БД (пользователи, машины, расписание, тарифы, подсчёты).
- **RoutesAndAdvertisement** — старая БД маршрутов (только чтение, связь по `imei` / `route_id`).

---

## Локальный запуск (разработка)

```bash
# 1. Виртуальное окружение
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# 2. Зависимости
pip install -r requirements.txt

# 3. Настройки
cp .env.example .env      # (Windows: copy .env.example .env)
# отредактируй .env — впиши доступы к MySQL

# 4. Запуск dev-сервера
uvicorn app.main:app --reload --host 0.0.0.0 --port 8083
```

Открой http://localhost:8083 — увидишь страницу с проверкой соединения с БД.
Swagger-документация API: http://localhost:8083/docs

---

## Развёртывание на Debian сервере (порт 8083)

Архитектура: **nginx слушает публичный порт 8083** и проксирует запросы на
gunicorn, который крутит FastAPI локально на `127.0.0.1:8000`.
Так безопаснее (Python не торчит наружу) и nginx сам отдаёт статику.

### 1. Установить пакеты

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx
```

### 2. Разместить проект

```bash
sudo mkdir -p /var/www/PassengersFlowCounting
# скопируй сюда файлы проекта (scp / git clone)
cd /var/www/PassengersFlowCounting

sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

# создать и заполнить .env
sudo cp .env.example .env
sudo nano .env          # впиши доступы к MySQL и SECRET_KEY

# права для www-data
sudo chown -R www-data:www-data /var/www/PassengersFlowCounting
```

> В `.env` можно оставить `APP_PORT`, он для dev. На проде порт слушает nginx (8083),
> а gunicorn биндится на 127.0.0.1:8000 (см. systemd unit).

### 3. Настроить systemd (автозапуск приложения)

```bash
sudo cp deploy/passengersflow.service /etc/systemd/system/passengersflow.service
sudo systemctl daemon-reload
sudo systemctl enable --now passengersflow
sudo systemctl status passengersflow      # должно быть active (running)
```

### 3.1. Демон отправки клиентских отчётов (SendLayer)

После `ready` городского/межгородского подсчёта (count_type `full_day`) в таблицу
`report_deliveries` ставится отчёт; через `REPORT_SEND_DELAY_MIN` минут отдельный
демон собирает xlsx и отправляет его на адреса владельца машины из
`gps.user_settings` (`type = 1`, `params` — адреса через запятую).
Пересчитанный день уходит как «версія 2, 3…».

```bash
# таблица очереди
mysql -u <user> -p < sql/report_deliveries.sql

# в .env
REPORT_EMAILS_ENABLED=true
SENDLAYER_API_KEY=<ключ из SendLayer>
# REPORT_FROM_EMAIL=info@mail.vcpserver.net.ua   (по умолчанию)
# REPORT_BCC=me@example.com                      (копия каждого отчёта)

# проверить отчёт без отправки: xlsx + письмо в папку report_preview/
sudo .venv/bin/python -m app.workers.report_mailer --preview <car_id> 2026-10-20

sudo cp deploy/passengersflow-mailer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now passengersflow-mailer
sudo journalctl -u passengersflow-mailer -f   # журнал отправки
```

После изменения `.env` перезапустить оба сервиса (`passengersflow` и
`passengersflow-mailer`).

### 4. Настроить nginx на порт 8083

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/passengersflow
sudo ln -s /etc/nginx/sites-available/passengersflow /etc/nginx/sites-enabled/
sudo nginx -t            # проверка конфига
sudo systemctl reload nginx
```

### 5. Открыть порт 8083 в фаерволе

```bash
sudo ufw allow 8083/tcp   # если используется ufw
```

Готово. Приложение доступно по `http://<IP_сервера>:8083`.
Проверка БД: `http://<IP_сервера>:8083/api/health`.

### Обновление кода после изменений

```bash
cd /var/www/PassengersFlowCounting
# git pull / scp новых файлов
sudo .venv/bin/pip install -r requirements.txt   # если менялись зависимости
sudo systemctl restart passengersflow passengersflow-mailer
```

---

## Дальнейшие шаги (по плану)

1. SQLAlchemy-модели под созданные таблицы.
2. Аутентификация (почта/пароль, роли: менеджер view/full, считальщик).
3. CRUD API: пользователи, машины, расписание, тарифы.
4. Логика подсчёта + автосохранение раз в 10 сек.
5. Фоновые задачи (генерация записей по расписанию).
6. Интерфейсы: панель менеджера, интерфейс считальщика.
7. «Розрахувати» — расчёт по треку машины.
