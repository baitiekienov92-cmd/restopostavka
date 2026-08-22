# КрафтСнаб — приём заказов от ресторанов

MVP-система: рестораны оформляют заказы на овощи/продукты в личном кабинете,
ты видишь все заказы и сводный лист закупа в админке.

## Запуск локально

```bash
cd restopostavka
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python seed.py                  # создаёт БД + тестовые данные
python app.py
```

Открыть http://localhost:5000

**Тестовые логины** (создаются в seed.py):
- Админ: `admin` / `admin123`
- Ресторан: `77770000000` / `demo123`

## Деплой на Render (как Flow∞)

1. Залить проект на GitHub
2. Render → New → Web Service → подключить репозиторий
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Добавить PostgreSQL (Render → New → PostgreSQL), скопировать `Internal Database URL`
6. В переменных окружения web-сервиса задать:
   - `DATABASE_URL` = внутренний URL базы (Render иногда выдаёт `postgres://` — если ошибка, замени префикс на `postgresql://`)
   - `SECRET_KEY` = любая случайная строка
7. После первого деплоя один раз выполнить `python seed.py` через Render Shell — создаст таблицы и первого админа

## Что дальше (роадмап)

- Этап 2: сводный лист закупа под поставщиков — уже есть (`/admin/summary`), можно расширить экспортом в Excel
- Этап 3: статусы доставки — есть базово, можно добавить назначение курьера/маршрута
- Мобильная версия: API можно вынести отдельно (сейчас логика в app.py) — когда дойдём до этапа, интерфейс не трогаем, добавляем JSON-эндпоинты

## Структура проекта

```
app.py          — роуты и логика
models.py       — модели БД (рестораны, товары, заказы)
seed.py         — наполнение тестовыми данными
templates/      — HTML-шаблоны
static/css/     — стили
```
