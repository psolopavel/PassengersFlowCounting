# Дизайн: внешнее API выручки и детализации сессий

Статус: **черновик к согласованию** (код ещё не пишется).
Область: новый публичный API в этом FastAPI-проекте (`/api/v1`), считающий
выручку/пассажиров/километраж по завершённым сессиям подсчёта.

---

## 1. Цели и сценарии

Клиент на внешнем сайте передаёт список `imei` машин и диапазон дней и получает:

- **Type 1 — сводка (массовый, должен быть быстрым).**
  По каждой машине за **каждый день** отдельно: фактический километраж,
  число пассажиров за день, заработок в гривнах (только межгород).
  Пример нагрузки: 10 машин × 14 дней = до 140 «дней-машин».

- **Type 2 — детализация (тяжелее, точечный).** Только межгород.
  Для одной машины за один день — полный JSON сессии (`counting_sessions.data`)
  + список тарифов, актуальных в этот день, по маршрутам, где ездила машина.

Диапазон дат от клиента приходит как две даты `DD-MM-YYYY HH:MM` (начало и конец).
На уровне данных мы работаем **по календарным дням** (`count_day` — это `DATE`),
поэтому время в этих метках используется только для вычисления множества дней
`[from.date() .. to.date()]` (см. §9, открытый вопрос про таймзону).

---

## 2. Главный принцип: материализация на записи, а не парсинг на чтении

`counting_sessions.data` — это `LONGTEXT` с JSON на ~1700 строк. Парсить 140+
таких блобов на каждый запрос — дорого (и по CPU, и по объёму выборки из MySQL).

**Решение:** тяжёлый разбор делается один раз — когда сессия завершается
(`status → ready`) или при финальных автосейвах. Результат кладётся в компактную
таблицу-агрегат `counting_daily_summary`. Тогда Type 1 — это индексный `SELECT`
по диапазону дат, без чтения `data` вообще.

Что именно материализуем (не меняется после `ready`):
- `distance_km` — берём из `data.distance_km` (уже считается при сборке);
- `passengers_total` — город: `data.result.in_salon_total`; межгород: сумма посадок (см. §4);
- `trips_json` — межгород: результат FIFO-разбора `[{route_id, from_id, to_id, pax}]`.

**Деньги в агрегате НЕ фиксируем намертво.** Выручку считаем из `trips_json`
× тариф, актуальный на `count_day`. `trips_json` маленький (десятки записей),
поэтому пересчёт денег мгновенный и всегда отражает исторически верный тариф —
даже если цену в `tariffs.prices` поправят задним числом (см. §5).

---

## 3. Модель данных (что уже есть)

Все имена/поля подтверждены по коду.

- **`counting_sessions`** (БД `passengers_flow`): `id`, `counter_id`, `car_id`,
  `count_day` (`DATE`), `time_spent_sec`, `status` (`in_progress|ready|skipped`),
  `kind` (`city|intercity`), `data` (`LONGTEXT`, JSON-строка), `created_at`,
  `updated_at`. Уникальность `(counter_id, car_id, count_day)`.
- **`cars`** (`passengers_flow`): `id`, `imei` (unique), `route_kind`, ...
- **`tariffs`** (`passengers_flow`): `id`, `route_id`, `name`, `prices` (JSON).
  Город: `{"kind":"city","price": N}`; межгород: `{"kind":"intercity","pairs":{"<minId>-<maxId>": грн}}`.
- **`car_route_tariffs`** (`passengers_flow`): `id`, `car_id`, `route_id`,
  `tariff_id`, `start_date` (`DATE`). История «какой тариф с какой даты».
  Актуальный тариф на дату D = запись с максимальным `start_date <= D`.
- **`routes`/`stops`/`all_car_routes`** (БД `RoutesAndAdvertisement`, read-only).

`data.segments[i]` (межгород): `seg_key`, `route_id`, `route_name`, `occurrence`,
`from_stop{id,name,order,arrival_utc,departure_utc}`, `to_stop{...}`,
`period_start_utc`, `period_end_utc`, `count` (люди в салоне на плече), `distance_km`.
Сегменты в `data.segments` отсортированы по `period_start_utc`.

---

## 4. Алгоритм выручки межгорода (FIFO по дельтам)

`count` сегмента = число людей в салоне на этом плече. Посадки/высадки выводим
из разницы между соседними сегментами. Модель — очередь FIFO: при высадке
выходят те, кто сел раньше всех из ещё едущих.

### Группировка по ходкам
FIFO считается **внутри одной ходки** = группа сегментов с одинаковыми
`(route_id, occurrence)`, упорядоченная по `period_start_utc`. В конце ходки все
оставшиеся пассажиры выходят на последней `to_stop` ходки.
> Причина: тарифные пары `pairs` заданы для остановок одного маршрута; переносить
> очередь между разными маршрутами/ходками нельзя.

### Точка высадки
При падении `count` между сегментами `i-1 → i` высадка происходит на границе,
т.е. на `from_stop(seg_i)` (человек уже не посчитан в `seg_i`). Это ровно логика
согласованного примера.

### Псевдокод (на одну ходку)
```
prev = 0
queue = []                      # FIFO: [ [boarding_stop_id, remaining_pax], ... ]
trips = []                      # результат: {route_id, from_id, to_id, pax}
boardings = 0                   # пассажиры за ходку

for seg in segments_of_run_sorted_by_time:
    c = seg.count               # предполагается int (сессия ready)
    delta = c - prev
    if delta > 0:               # посадка на seg.from_stop
        queue.append([seg.from_stop.id, delta])
        boardings += delta
    elif delta < 0:             # высадка на seg.from_stop
        need = -delta
        while need > 0 and queue:
            board_stop, cnt = queue[0]
            take = min(cnt, need)
            trips.append({route_id, from_id: board_stop, to_id: seg.from_stop.id, pax: take})
            cnt -= take; need -= take
            if cnt == 0: queue.pop(0)
            else: queue[0][1] = cnt
    prev = c

# конец ходки: оставшиеся выходят на последней to_stop
last_to = segments_of_run_sorted_by_time[-1].to_stop.id
for board_stop, cnt in queue:
    trips.append({route_id, from_id: board_stop, to_id: last_to, pax: cnt})
```

### Проверка на согласованном примере (одна ходка)
Сегменты по `count`: `0,0,1,13,12,3`.
- seg3 +1 → посадка 1 @ from(seg3); boardings=1
- seg4 +12 → посадка 12 @ from(seg4); boardings=13
- seg5 −1 → 1 чел (с seg3) выходит @ from(seg5): trip `from(seg3) → from(seg5)`, pax=1
- seg6 −9 → 9 чел (с seg4) выходят @ from(seg6): trip `from(seg4) → from(seg6)`, pax=9
- конец: остаток 3 (с seg4) выходят @ last `to_stop`: trip `from(seg4) → last_to`, pax=3

**Пассажиров за день = сумма посадок = 13** (по всем ходкам суммарно).

### Деньги
Для каждого `trip`:
```
key   = f"{min(from_id,to_id)}-{max(from_id,to_id)}"
price = tariff_prices_for(route_id, count_day)["pairs"].get(key)
если price есть:   revenue += pax * price
иначе:             warnings += "no_pair_price:<route_id>:<key>"
```
`tariff_prices_for(route_id, day)` = найти в `car_route_tariffs` для
`(car_id, route_id)` запись с максимальным `start_date <= day` → `tariff_id` →
`tariffs.prices`. Нет активного тарифа → поездка в `warnings`, вклад 0.

### Город
Тарифов не считаем вообще (по требованию). `passengers_total = in_salon_total`,
`revenue_uah = null`.

---

## 5. Заморозка vs пересчёт выручки

- `trips_json`, `passengers_total`, `distance_km` — **замораживаются** при `ready`
  (зависят только от `count`, которые после `ready` не меняются).
- `revenue_uah` — **пересчитывается** из `trips_json` на чтении (или фоновым
  джобом), чтобы всегда использовать исторически актуальный тариф. Пересчёт
  дешёвый — гигантский `data` не трогается.

Опционально для ещё большей скорости Type 1 можно кэшировать `revenue_uah` в
таблице с полем `tariff_rev` (версия/хэш применённых тарифов) и пересчитывать
только при изменении тарифов. На старте — считаем на чтении, кэш добавим при
необходимости.

---

## 6. Новая таблица `counting_daily_summary`

БД `passengers_flow`. Одна строка на «день-машину».

```sql
CREATE TABLE counting_daily_summary (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  session_id       BIGINT UNSIGNED NOT NULL,
  car_id           BIGINT UNSIGNED NOT NULL,
  imei             VARCHAR(32)     NOT NULL,
  count_day        DATE            NOT NULL,
  kind             ENUM('city','intercity') NOT NULL,
  status           ENUM('in_progress','ready','skipped') NOT NULL,
  distance_km      DECIMAL(10,2)   NOT NULL DEFAULT 0,
  passengers_total INT UNSIGNED    NOT NULL DEFAULT 0,
  trips_json       LONGTEXT        NULL,       -- межгород: FIFO-разбор
  computed_at      DATETIME        NOT NULL,
  UNIQUE KEY uq_car_day (car_id, count_day),
  KEY idx_imei_day (imei, count_day),
  KEY idx_day (count_day)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Заметки:
- `imei` денормализуем в строку, чтобы Type 1 фильтровал по `imei` без join к `cars`.
- `UNIQUE (car_id, count_day)` — одна каноническая строка на день-машину
  (см. правило выбора сессии в §9).
- Миграция — новый файл `sql/revenue_summary.sql` (в стиле существующих в `sql/`).

---

## 7. Компоненты кода

```
app/services/revenue_service.py     # FIFO-разбор, расчёт денег, резолв тарифа по дате
app/services/summary_service.py     # материализация session -> counting_daily_summary
app/models/counting_daily_summary.py# ORM-модель
app/routers/api_v1.py               # публичные эндпоинты + проверка ключа
app/security_api.py                 # dependency: require_api_key (статический ключ)
scripts/backfill_summaries.py       # разовый бэкофилл ready-сессий
sql/revenue_summary.sql             # DDL
docs/revenue_api_design.md          # этот документ
```

Точки интеграции (хуки материализации) в `app/routers/panel.py` /
`app/services/counting_session_service.py`:
- `set_status(..., "ready")`,
- `count_city_save` (финал города),
- `count_intercity_save` (финал межгорода),
- пересбор/удаление сессии (`reset`, `rebuild`, `delete`, `skip`) —
  обновить/убрать строку summary.

Регистрация роутера в `app/main.py`: `app.include_router(api_v1.router)`.

---

## 8. Контракты API

Версионирование: префикс `/api/v1`. Аутентификация: статический ключ из настроек
(`Settings.revenue_api_key`, читается из `.env`), передаётся заголовком
`X-API-Key`. Dependency `require_api_key` возвращает `401`, если ключ неверный.
`get_settings()` уже есть; добавим поле `revenue_api_key: str = ""`.

### 8.1 Type 1 — сводка выручки

`POST /api/v1/revenue`
```jsonc
// request
{
  "imeis": ["350612070000000", "350612070000001"],
  "from": "01-09-2026 00:00",
  "to":   "14-09-2026 23:59"
}
```
```jsonc
// response 200
{
  "from": "2026-09-01",
  "to":   "2026-09-14",
  "days": [
    {
      "imei": "350612070000000",
      "day": "2026-09-03",
      "kind": "intercity",
      "distance_km": 126.62,
      "passengers": 143,
      "revenue_uah": 5820,
      "warnings": []            // напр. ["no_pair_price:280:2855-2870"]
    },
    {
      "imei": "350612070000000",
      "day": "2026-09-04",
      "kind": "city",
      "distance_km": 88.10,
      "passengers": 402,
      "revenue_uah": null       // город: деньги не считаем
    }
  ],
  "missing_imeis": ["350612070000001"]  // imei без единого ready-дня в диапазоне
}
```
Реализация: один `SELECT` из `counting_daily_summary` по `imei IN (...)` и
`count_day BETWEEN ...`, `status='ready'`. Затем для межгорода — резолвим
`car_route_tariffs` + `tariffs.prices` и считаем `revenue_uah` из `trips_json`.
`data` не читаем.

`missing_imeis` — только те imei из запроса, по которым в диапазоне нет ни одного
ready-дня (нет данных / неизвестный imei). Пропуски по отдельным дням не
перечисляем: без расписаний нельзя отличить «не по графику» от «не подсчитано» —
клиент видит фактические дни в `days`.

### 8.2 Type 2 — детализация дня

`GET /api/v1/session/{imei}/{day}` (`day` в `YYYY-MM-DD`)
```jsonc
// response 200
{
  "imei": "350612070000000",
  "day": "2026-09-03",
  "kind": "intercity",
  "data": { /* полный counting_sessions.data */ },
  "tariffs": [                 // актуальные на этот день по маршрутам из route_periods
    {
      "route_id": 280,
      "route_name": "Єгорівка - Одеса",
      "tariff_id": 12,
      "tariff_name": "Стандарт 2026",
      "start_date": "2026-08-01",
      "prices": { "kind": "intercity", "pairs": { "2855-2856": 10, ... } }
    }
  ]
}
```
Реализация: одна сессия (одна строка `data`) + резолв тарифов по `route_id` из
`data.route_periods` на `count_day`. Дёшево.

### 8.3 Выгрузка в xlsx

Ответ — файл (`Content-Type` xlsx, имя в `Content-Disposition: filename*=UTF-8''…`),
ошибки — как у JSON-эндпоинтов (`{"detail": ...}`).

- `POST /api/v1/revenue/xlsx` — общий отчёт: тело как у 8.1 плюс необязательные
  `title` (название аккаунта) и `names` (`{imei: название}`; иначе `cars.name`).
  Один лист «Виручка»: авто × дни (км, пассажиры, выручка, тип), итоги по авто
  и общий итог пассажиров/выручки, список авто без данных. Порядок авто — как в `imeis`.
- `GET /api/v1/session/{imei}/{day}/xlsx` — полный отчёт дня (тот же, что
  отправляется клиенту письмом, `report_xlsx_service.build_xlsx`). Номер версии
  согласован с отправленными письмами.

---

## 9. Крайние случаи и правила

1. **Несколько сессий на день-машину** (разные `counter_id`): в summary берём
   `status='ready'`; при нескольких — с максимальным `updated_at`.
2. **Не готовые сессии** (`in_progress`/`skipped`, есть `count=null`) в summary не
   попадают; в Type 1 они уходят в `missing`.
3. **Нет цены пары / нет активного тарифа**: поездка → `warnings`, вклад 0.
4. **Битые данные тарифа** (в примере встречается `"2859-2869": 4040`): не
   «чиним», считаем как есть; можно добавить sanity-warning при аномальной цене.
5. **`count=null` в ready-сессии** (по факту не должно случаться): трактуем как
   `count=0` и добавляем `warnings: "segment_not_counted:<seg_key>"`.
6. **Таймзона диапазона**: `count_day` — календарный день как есть. Множество
   дней = `from.date()..to.date()` без конвертации таймзоны (клиент мыслит
   календарными днями). Решение по Q1.
7. **Пустой трек/сегменты**: `distance_km=0`, `passengers=0`, `revenue=0`.
8. **Идемпотентность материализации**: `upsert` по `(car_id, count_day)`.

---

## 10. Производительность

- Type 1: индексный скан `idx_imei_day`; N строк по числу день-машин. Нет чтения
  `LONGTEXT data`. Расчёт денег из `trips_json` (десятки записей) + пакетная
  выборка тарифов. Ожидаемо десятки мс на сотни день-машин.
- Type 2: одна строка `data` + резолв тарифов. Десятки мс.
- Тяжёлый FIFO-разбор `data` — только при завершении сессии (амортизирован).

---

## 11. Тестирование

- Юнит-тест FIFO на согласованном примере (`0,0,1,13,12,3` → trips + boardings=13).
- Тест резолва тарифа по дате (границы периодов `car_route_tariffs`).
- Тест денег: trips × pairs с отсутствующей парой (warning), с несколькими ходками.
- Тест материализации: `ready` → строка summary; `reset/rebuild/delete` → обновление/удаление.
- Тест API: неверный ключ → 401; диапазон → `days` + `missing`.
- Тест бэкофилла на нескольких реальных `data`.

---

## 12. План внедрения (после согласования)

1. `sql/revenue_summary.sql` + ORM-модель `CountingDailySummary`.
2. `revenue_service.py`: FIFO-разбор + расчёт денег + резолв тарифа по дате (+тесты).
3. `summary_service.py`: `build_summary(session)` + `upsert`.
4. Хуки материализации в panel/session-service.
5. `scripts/backfill_summaries.py` — бэкофилл существующих `ready`.
6. `security_api.py` (`revenue_api_key` в конфиг) + `api_v1.py` (оба эндпоинта).
7. Регистрация роутера, README/пример вызова.

---

## 13. Согласованные решения (Q1–Q6)

- **Q1 ✓.** `count_day` — календарный день. День входит в ответ, если он в
  `[from.date() .. to.date()]` (без конвертации таймзоны). (§9.6)
- **Q2 ✓.** `passengers` города = `data.result.in_salon_total` (всего подсчитанных
  пассажиров за день).
- **Q3 ✓.** `count=null` в межгороде → трактуем как `0` + `warnings`. (§9.5)
- **Q4 ✓.** Разрез по маршрутам/тарифам в Type 1 НЕ нужен — только суммарные
  `distance_km`, `passengers`, `revenue_uah` за день.
- **Q5 ✓.** Кэш `revenue_uah` не делаем — считаем на чтении из `trips_json`.
- **Q6 ✓.** `day` в Type 2 = `YYYY-MM-DD`.
