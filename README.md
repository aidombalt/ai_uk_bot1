# 🏠 Балтийский Дом — AI-бот для чатов ЖК в мессенджере Max

AI-ассистент управляющей компании: классифицирует обращения жильцов, отвечает на типовые вопросы, эскалирует сложные случаи управляющему. Веб-панель для администраторов.

**Стек:** Python 3.11, [maxapi](https://pypi.org/project/maxapi/), YandexGPT 5.1 Pro, FastAPI, SQLite.

---

## Содержание

- [Что умеет бот](#что-умеет-бот)
- [Локальный запуск (быстрый старт)](#локальный-запуск-быстрый-старт)
- [Подробная пошаговая инструкция](#подробная-пошаговая-инструкция)
  - [Шаг 1. Создание бота в Max](#шаг-1-создание-бота-в-max)
  - [Шаг 2. Получение API-ключа Yandex Cloud](#шаг-2-получение-api-ключа-yandex-cloud)
  - [Шаг 3. Установка проекта](#шаг-3-установка-проекта)
  - [Шаг 4. Настройка .env](#шаг-4-настройка-env)
  - [Шаг 5. Настройка config.yaml](#шаг-5-настройка-configyaml)
  - [Шаг 6. Первый запуск](#шаг-6-первый-запуск)
  - [Шаг 7. Добавление бота в чат ЖК](#шаг-7-добавление-бота-в-чат-жк)
  - [Шаг 8. Узнать chat\_id](#шаг-8-узнать-chat_id)
  - [Шаг 9. Вход в веб-панель](#шаг-9-вход-в-веб-панель)
- [Деплой на сервер (production)](#деплой-на-сервер-production)
  - [Вариант А. VPS с Docker (рекомендуется)](#вариант-а-vps-с-docker-рекомендуется)
  - [Вариант Б. Render.com](#вариант-б-rendercom)
- [Что делать если что-то сломалось](#что-делать-если-что-то-сломалось)
- [Архитектура](#архитектура)

---

## Что умеет бот

В групповом чате ЖК бот:
- Принимает сообщения жильцов
- Через YandexGPT понимает **тему** (авария, ЖКХ, охрана, и т.д.), **срочность** и **тип** (вопрос, жалоба, агрессия)
- На типовые обращения **отвечает сам** (стиль ответа УК, без «мы/наша компания»)
- Сложное **пересылает управляющему** в личный диалог с двумя кнопками: «Одобрить автоответ» / «Игнорировать»
- На **агрессию** и **провокации** не отвечает публично — только пересылает управляющему
- На **аварии** — короткое «принято» в чат + срочная эскалация

Веб-панель показывает очередь эскалаций, ленту всех обращений, статистику, позволяет редактировать список ЖК и системные промты без перезапуска.

---

## Локальный запуск (быстрый старт)

Если уже знаком с Python и Docker:

```bash
git clone <repo> && cd balt-dom-bot
cp .env.example .env             # вставить свои токены
cp config.example.yaml config.yaml  # заполнить ЖК
docker compose up --build
```

Откройте http://localhost:8000, логин `admin` / `admin`.

Дальше — для тех, кому нужна детальнее инструкция.

---

## Подробная пошаговая инструкция

### Шаг 1. Создание бота в Max

1. Установите мессенджер [Max](https://max.ru) на телефон или компьютер.
2. Откройте диалог с **MasterBot** — официальный бот платформы. Найти его можно через поиск в приложении или по [этой ссылке](https://max.ru/masterbot).
3. Напишите MasterBot команду `/start` → `Создать нового бота`.
4. MasterBot спросит:
   - **Имя бота** — отображается в чате (например, «УК Балтийский Дом»)
   - **Username** — уникальный логин (например, `balt_dom_bot`)
   - **Аватар** — картинка (необязательно)
5. Когда бот создан, MasterBot пришлёт **токен** — длинная строка вида `f9LH...4`. Скопируйте её и **никому не показывайте** — это пароль вашего бота.

> ⚠️ Если потеряли токен — попросите MasterBot его пересоздать.

### Шаг 2. Получение API-ключа Yandex Cloud

Бот использует YandexGPT 5.1 Pro для классификации и генерации ответов. Это платный сервис (~0.80 ₽ за 1000 токенов).

1. Зарегистрируйтесь в [Yandex Cloud](https://cloud.yandex.ru) — нужен аккаунт Яндекс.
2. Создайте **платёжный аккаунт** (нужна банковская карта; есть пробный грант).
3. В консоли создайте **Каталог** (folder) — это контейнер для ваших ресурсов. Скопируйте его **ID** (вида `bpfdkeumak5lphbrnif2`).
4. В каталоге создайте **сервисный аккаунт** с ролью `ai.languageModels.user`.
5. Создайте для этого аккаунта **API-ключ** — короткая строка (вида `ajei...g`). Сохраните её.

Подробная инструкция: https://yandex.cloud/ru/docs/foundation-models/quickstart/yandexgpt

### Шаг 3. Установка проекта

#### Вариант А: через Docker (проще)

Установите [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/Mac) или `docker` + `docker-compose` (Linux).

```bash
git clone <ваш-репозиторий> balt-dom-bot
cd balt-dom-bot
```

#### Вариант Б: напрямую Python (для разработки)

Нужен **Python 3.11 или новее**. Проверьте: `python3 --version`.

```bash
git clone <ваш-репозиторий> balt-dom-bot
cd balt-dom-bot

python3 -m venv .venv
source .venv/bin/activate          # на Windows: .venv\Scripts\activate
pip install -e .
```

### Шаг 4. Настройка .env

Скопируйте шаблон:

```bash
cp .env.example .env
```

Откройте `.env` в любом редакторе и заполните:

```
MAX_BOT_TOKEN=<токен от MasterBot из шага 1>
YANDEX_FOLDER_ID=<ID каталога из шага 2>
YANDEX_API_KEY=<API-ключ из шага 2>

GUI_ADMIN_LOGIN=admin
GUI_ADMIN_PASSWORD=<придумайте надёжный пароль>
GUI_SECRET_KEY=<случайная строка ≥32 символов; см. ниже>
```

**Как сгенерировать GUI_SECRET_KEY:**
- Linux/Mac: `openssl rand -hex 32`
- Онлайн: https://djecrety.ir/ или https://1password.com/password-generator/

> Если оставите дефолтные значения, при старте бот напишет в логах **WARNING** — это защита от случайной публикации.

### Шаг 5. Настройка config.yaml

Скопируйте шаблон:

```bash
cp config.example.yaml config.yaml
```

В `config.yaml` менять обычно не нужно — все ЖК добавляются через **веб-панель**. На старте YAML используется как seed (первоначальное наполнение). Если запускаете впервые — оставьте раздел `complexes:` пустым:

```yaml
complexes: []
default_manager_chat_id: 0
```

ЖК добавите потом через GUI.

### Шаг 6. Первый запуск

#### Через Docker:

```bash
docker compose up --build
```

#### Напрямую:

```bash
source .venv/bin/activate
python -m balt_dom_bot
```

В логах увидите:
```
[info] db.migrating from_=0 to=3
[info] db.connected
[info] users.created login=admin role=admin
[info] app.built ...
[info] main.starting mode=polling gui=True
```

Если видите `main.dry_run` — токен не задан, бот в режиме «всё проверено, но к Max не подключаюсь».

### Шаг 7. Добавление бота в чат ЖК

1. В Max найдите групповой чат жильцов.
2. Добавьте туда вашего бота (через поиск по username из шага 1).
3. **Обязательно** дайте боту права **администратора** — без этого бот не получит сообщения.

### Шаг 8. Узнать chat_id

Чтобы бот понимал, какой чат к какому ЖК относится, нужен **chat_id** — числовой идентификатор чата в Max. Узнать его проще всего так:

1. Бот уже в чате (после шага 7).
2. Напишите в чат любое сообщение (например, «привет»).
3. Откройте логи бота — там увидите строку:
   ```
   [info] messages.received chat_id=-1234567890 user_id=... mid=...
   ```
4. Скопируйте число `chat_id` (со знаком минус, если он есть).

То же самое для **личного чата управляющего**: попросите управляющего написать боту в личку (или сами напишите от его имени), посмотрите `chat_id` в логах. Это и есть `manager_chat_id`.

### Шаг 9. Вход в веб-панель

Откройте в браузере: **http://localhost:8000** (если локально) или `http://<IP-сервера>:8000` (если на сервере).

Логин/пароль из `.env`: `GUI_ADMIN_LOGIN` / `GUI_ADMIN_PASSWORD`.

Что делать в панели:

1. **ЖК** → «Добавить» → заполните:
   - **ID** — короткий идентификатор (например, `avrora-1`)
   - **Название** — «ЖК Аврора-1»
   - **Адрес** — «ул. Коллонтай, 5 к.1»
   - **chat_id** — из шага 8 (со знаком минус)
   - **ID управляющего** — `chat_id` управляющего из шага 8
   - **Активен** — галочка

2. **Промты** — посмотрите дефолтные системные промты для классификатора и генератора ответов. Можно править под свой стиль.

3. **Эскалации** — пусто пока никто не написал.

4. **Лента** — все входящие сообщения с разметкой темы/срочности/типа.

5. **Статистика** — графики за 30 дней.

Готово! Теперь жильцы могут писать в чат, бот будет отвечать.

---

## Деплой на сервер (production)

### Вариант А. VPS с Docker (рекомендуется)

Подходит: Selectel, Timeweb Cloud, Beget, любой VPS с Ubuntu/Debian.

1. **Закажите VPS** — минимум 1 vCPU, 1 ГБ RAM, 10 ГБ диск. Для пилота с 2-3 чатами хватит.
2. **Установите Docker** на сервер:
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER  # перелогиньтесь после
   ```
3. **Загрузите проект** на сервер (через `git clone` или `scp`).
4. Настройте `.env` и `config.yaml` (см. шаги 4–5 выше).
5. **Запустите**:
   ```bash
   docker compose up -d --build
   ```
   Флаг `-d` = в фоне.
6. **Проверьте**:
   ```bash
   docker compose logs -f bot
   curl http://localhost:8000/healthz
   ```
7. **Откройте порт 8000** в файрволле (если есть). На Ubuntu:
   ```bash
   sudo ufw allow 8000
   ```
8. **Бэкапы**: SQLite-файл лежит в `./data/bot.sqlite`. Настройте регулярное копирование:
   ```bash
   # crontab -e
   0 3 * * * cp /path/to/balt-dom-bot/data/bot.sqlite /backup/bot-$(date +\%F).sqlite
   ```

#### Защита GUI HTTPS-ом (рекомендуется)

Поднимите перед ботом **nginx** или **Caddy** с автоматическим SSL от Let's Encrypt:

```nginx
# /etc/nginx/sites-available/balt-dom
server {
    listen 80;
    server_name yourdomain.ru;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl http2;
    server_name yourdomain.ru;
    ssl_certificate /etc/letsencrypt/live/yourdomain.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.ru/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_buffering off;  # важно для SSE-ленты
        proxy_read_timeout 86400;
    }
}
```

Запустите `certbot` для автоматического сертификата:
```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.ru
```

После этого панель доступна по https://yourdomain.ru, бот общается с Max в фоне.

### Вариант Б. Render.com

Render умеет запускать Docker-контейнеры из git-репозитория.

1. Залейте проект на **GitHub/GitLab**.
2. На [render.com](https://render.com) → **New** → **Web Service** → подключите репозиторий.
3. Настройки:
   - **Environment**: Docker
   - **Plan**: Starter ($7/мес) или выше
   - **Health Check Path**: `/healthz`
4. Во вкладке **Environment** добавьте все переменные из `.env`:
   `MAX_BOT_TOKEN`, `YANDEX_FOLDER_ID`, `YANDEX_API_KEY`, `GUI_ADMIN_PASSWORD`, `GUI_SECRET_KEY`, и т.д.
5. **Disk** → создайте Persistent Disk на 1 GB и смонтируйте в `/app/data` — иначе SQLite будет теряться при каждом редеплое.
6. **Deploy**.

После деплоя Render даст публичный URL вида `https://balt-dom-bot.onrender.com` — это и будет адрес GUI.

> ⚠️ На Free-плане Render **усыпляет** сервис без активности — бот не будет получать сообщения. Нужен платный план.

---

## Что делать если что-то сломалось

### Бот не отвечает в чате
1. Проверьте, что бот в чате с **правами администратора**.
2. Логи: `docker compose logs bot | tail -50` — ищите ошибки.
3. Проверьте, что `chat_id` чата добавлен в **ЖК** через GUI и **активен**.
4. Если в логе `main.dry_run` — токен заглушка, проверьте `.env`.

### YandexGPT отвечает 403/401
- Проверьте `YANDEX_API_KEY` и `YANDEX_FOLDER_ID` в `.env`.
- Сервисный аккаунт должен иметь роль `ai.languageModels.user`.
- В Yandex Cloud проверьте, что платёжный аккаунт активен.

### LLM упал — бот всё равно отвечает шаблонами
Так и задумано: при ошибке LLM срабатывает FAQ-fallback или нейтральный «обращение принято». Жилец не остаётся без ответа.

### Не помню пароль в GUI
Откройте `.env`, поставьте новый `GUI_ADMIN_PASSWORD`, удалите файл `data/bot.sqlite` (потеряете историю!), перезапустите бота — будет создан новый admin.

Менее радикально — открыть БД и обновить хеш вручную:
```bash
docker compose exec bot python -c "
import asyncio
from balt_dom_bot.storage.db import Database
from balt_dom_bot.storage.users_repo import UsersRepo
async def m():
    db = Database('/app/data/bot.sqlite'); await db.connect()
    u = UsersRepo(db)
    import bcrypt
    h = bcrypt.hashpw(b'НОВЫЙ_ПАРОЛЬ', bcrypt.gensalt()).decode()
    await db.conn.execute('UPDATE users SET password_hash=? WHERE login=?', (h, 'admin'))
    await db.conn.commit()
asyncio.run(m())
"
```

### База повреждена
Восстановите из бэкапа: остановите бота, замените `data/bot.sqlite` свежей копией, перезапустите.

### Хочу поменять промт
GUI → **Промты** → отредактируйте `classifier_system` или `responder_system` → «Сохранить». Применится в течение 30 секунд.

### Бот ответил неподобающе
В GUI → **Эскалации** одобряйте/правьте автоответы вручную. Постепенно настройте промт.

### Жалоба «бот вместо живого человека»
Это нормально для пилота. Бот не скрывает свою природу. Управляющий через GUI всегда может вмешаться и ответить лично.

---

## Архитектура

```
Max (чат ЖК)
    ↓ long-polling
[Бот] ── классификатор (YandexGPT) ──┐
    ↓                                 │
SQLite ←── pipeline ──→ генератор ────┤
    ↑                                 │  (FAQ → LLM → fallback)
    │     ↓ если эскалация            │
    │  карточка в личку управляющему ←┘
    │     ↑
    └── веб-панель FastAPI (одобрить/игнор/CRUD ЖК/промты)
```

Весь код — `src/balt_dom_bot/`. Ключевые модули:

| Модуль | Что делает |
|---|---|
| `services/classifier.py` | Stub + LLM + Safety-net (мат всегда AGGRESSION) |
| `services/responder.py` | FAQ → LLM → safe fallback + санитайзер «мы/наша УК» |
| `services/pipeline.py` | classify → decide (reply/escalate/silent) → log |
| `services/escalation.py` | Двухфазная отправка карточки с откатом |
| `gui/app.py` | FastAPI: эскалации/лента/статистика/ЖК/промты |
| `storage/*` | SQLite (escalations, messages, replies, prompts, complexes, users) |
| `prompts/*` | Дефолтные системные промты (БД может переопределить) |

Подробности по каждому модулю — в его докстринге.

---

## Лицензия

Internal use, ООО «Балтийский Дом», Санкт-Петербург, 2026.
