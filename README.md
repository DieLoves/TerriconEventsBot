# Terricon Events Bot

Неофициальный Telegram-бот для каталога мероприятий Terricon Valley и уведомлений о
новых событиях. Требования зафиксированы в
[`docs/specs/terricon-events-bot.md`](docs/specs/terricon-events-bot.md), порядок реализации —
в [`docs/plans/terricon-events-bot-v1.md`](docs/plans/terricon-events-bot-v1.md).

## Локальная разработка

Требуется `uv`; Python 3.13 и зависимости устанавливаются автоматически.

```bash
cp .env.example .env
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Секреты хранятся только в `.env`, который исключён из Git.

Read-only проверка всех шести тематических endpoint Terricon API:

```bash
uv run terricon-api-smoke
```

Команда использует только `BASE_URL`, не подключается к PostgreSQL и не изменяет данные.

## Первый пробный запуск

Для запуска нужны Docker с Compose и заполненный `.env`. Безопасный начальный режим —
`ACCESS_MODE=allowlist`; добавьте свой Telegram ID одновременно в `ADMIN_TELEGRAM_IDS` и
`ALLOWED_TELEGRAM_IDS`. `ADMIN_CHAT_ID` можно оставить равным личному Telegram ID администратора.
Обязательны действующие `TELEGRAM_BOT_TOKEN` и `OPENAI_API_KEY`; preflight перечислит только
имена отсутствующих или неверных полей и не напечатает их значения.

`DATABASE_URL` из `.env` внутри Compose переопределяется адресом контейнера PostgreSQL. Пароль БД
для пробного локального запуска можно задать переменной `POSTGRES_PASSWORD`; без неё используется
локальное значение по умолчанию, а порт PostgreSQL наружу не публикуется.

```bash
docker compose build
docker compose run --rm migrate
docker compose run --rm --no-deps app terricon-preflight
docker compose up -d app
docker compose logs -f app
```

После сообщения `Bot polling started` откройте бота и отправьте `/start`. Для проверки
административной части используйте `/admin` и `/status`.

Остановка без удаления данных:

```bash
docker compose down
```

Не используйте `docker compose down -v`, если данные пробного запуска нужно сохранить.

### Что запускается в фоне

- синхронизация при старте и затем через `SYNC_INTERVAL_HOURS`;
- classification queue, пользовательский outbox и служебные рассылки;
- доставка накопленного в тихие часы после наступления доступного времени;
- admin-alerts, очистка старых feedback-тикетов и heartbeat.

Очереди и прогресс хранятся в PostgreSQL. Повторный запуск приложения продолжает незавершённые
доставки. Healthcheck проверяет соединение с БД, актуальную миграцию и свежий heartbeat без
публичного HTTP-порта.

Перед запуском без Docker можно отдельно проверить конфигурацию и схему:

```bash
uv run alembic upgrade head
uv run terricon-preflight
```
