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

### Groq

С этапа 9 используется только официальный SDK `groq`, модель по умолчанию —
`openai/gpt-oss-120b`. Префикс модели не означает обращение к OpenAI API.
Для существующей установки перенесите AI-настройки на `GROQ_*` по `.env.example`:
старый `OPENAI_API_KEY` не используется, требуется ключ Groq.

На Free цены `GROQ_INPUT_PRICE_PER_MILLION_USD` и
`GROQ_OUTPUT_PRICE_PER_MILLION_USD` равны нулю; токены учитываются. Мягкий бюджет
`GROQ_MONTHLY_BUDGET_USD` не ограничивает RPM/TPM/RPD/TPD. После перехода на платный
тариф нужно самостоятельно настроить актуальные цены. Исторические расходы сохраняются
в общем AI ledger; SQL-имя `openai_usage` оставлено для совместимости данных.

Классификация и перевод используют строгий JSON Schema. Ошибки повторяются через
очередь; `Retry-After` сохраняется в БД, перевод восстанавливается после рестарта
без повторной классификации. С этапа 10 перед каждым AI-вызовом PostgreSQL атомарно
резервирует общие `GROQ_RPM/RPD/TPM/TPD` для обеих операций. Состояние и 429 cooldown
переживают рестарт; defaults 24/800/6400/160000 оставляют запас относительно Free.
Все процессы организации с общей БД должны сохранять один `GROQ_QUOTA_SCOPE`.

Для обновления нужна `alembic upgrade head` до `0011_groq_shared_quota`; прежние
usage ledger/данные сохраняются. Изменения runtime/`.env` и production запуск
выполняются отдельно. Методика, реальные результаты и ограничения:
[приёмка этапа 10](docs/operations/groq-stage10-2026-10-21.md).

Read-only проверка всех шести тематических endpoint Terricon API:

```bash
uv run terricon-api-smoke
```

Команда использует только `BASE_URL`, не подключается к PostgreSQL и не изменяет данные.

## Первый пробный запуск

Для запуска нужны Docker с Compose и заполненный `.env`. Безопасный начальный режим —
`ACCESS_MODE=allowlist`; добавьте свой Telegram ID одновременно в `ADMIN_TELEGRAM_IDS` и
`ALLOWED_TELEGRAM_IDS`. `ADMIN_CHAT_ID` можно оставить равным личному Telegram ID администратора.
Обязательны действующие `TELEGRAM_BOT_TOKEN` и `GROQ_API_KEY`; preflight перечислит только
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

Production backup, проверка восстановления, daily cron и последовательность ручного rollout
описаны в [`docs/operations/deployment.md`](docs/operations/deployment.md).
