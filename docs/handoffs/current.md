# Handoff

## Objective

Продолжать реализацию Terricon Events Bot v1 строго по утверждённой спецификации и техническому плану, выполняя по одному этапу и останавливаясь после каждого для подтверждения пользователя.

## Current state

- Этапы 1 и 2 технического плана реализованы.
- Проект использует Python 3.13/uv, типизированную конфигурацию, SQLAlchemy models и пять последовательных Alembic-миграций.
- В корне есть пользовательский `.env`; его содержимое нужно сохранять, не выводить и не коммитить.
- Следующий этап после подтверждения пользователя — импорт Terricon API.

## Completed

- Исследован публичный Terricon API для комбинаций `lang=ru|kz` и `theme=it|business|marketing`.
- Согласованы продуктовые, UX, инфраструктурные, privacy и failure-mode решения.
- Сохранена полная спецификация v1.
- Сохранён decision-complete технический план реализации.
- Подтверждено, что существующей архитектуры, конфликтующей со спецификацией, нет.
- Завершён этап 1: scaffold, configuration/composition root, async SQLAlchemy/UoW, Alembic, доменные enum, RU/KZ locales, asset resolver и безопасное логирование.
- Завершён этап 2: полная PostgreSQL-схема для users/events/sync/subscriptions/outbox/feedback/broadcast/operations, разбитая на пять миграций.
- Добавлены unit schema tests и opt-in PostgreSQL integration tests для миграций, ограничений подписок и идемпотентности delivery.

## Key decisions

- Источники истины: сначала спецификация, затем технический план; при расхождении приоритет у спецификации.
- Стек: Python 3.13, aiogram 3, async SQLAlchemy/PostgreSQL, Alembic, httpx, OpenAI Responses API, APScheduler, uv и Docker Compose.
- Production — один long-polling экземпляр без Redis; durable state, FSM, outbox и очереди находятся в PostgreSQL.
- GPT используется только для классификации и аварийного перевода отсутствующей локали события; статический интерфейс и admin-рассылки не переводятся GPT.
- Первая поставка начинается в allowlist beta; KZ locale содержит редактируемые пользователем TODO с RU fallback.
- Взаимоисключение `subscribe_all` и категорий закреплено составным FK в PostgreSQL; переключение должно выполняться одной транзакцией.
- Физические очереди classification, notification delivery/outbox и broadcast созданы на этапе схемы, но их сервисная логика относится к последующим этапам.

## Relevant files

- `docs/specs/terricon-events-bot.md` — утверждённая спецификация.
- `docs/plans/terricon-events-bot-v1.md` — утверждённый технический план и порядок этапов.
- `src/terricon_events_bot/infrastructure/models/` — SQLAlchemy-схема.
- `alembic/versions/` — пять миграций этапа 2.
- `tests/integration/test_postgres_schema.py` — opt-in PostgreSQL-проверки.
- `.env` — пользовательская конфигурация; не раскрывать и не перезаписывать.

## Repository state

- Корень проекта: `/home/dev/workspace/projects/terriconparser`.
- Git-репозиторий находится на ветке `main`, remote `origin` указывает на `git@github.com:DieLoves/TerriconEventsBot.git`.
- Этап 1 сохранён коммитом `2720fb1` и отправлен в `origin/main`.
- On-disk `AGENTS.md` в проекте или его родительских каталогах не найден; применялись пользовательские инструкции текущей сессии.

## Validation

- Python 3.13.15 установлен через uv; локальный gate выполняется в `.venv`.
- Этап 2: 36 unit-тестов проходят; четыре PostgreSQL integration-теста корректно пропускаются без `TEST_DATABASE_URL`.
- `ruff check`, `ruff format --check`, `mypy` и `alembic heads` проходят.
- Alembic upgrade `base → 0005_operations` и downgrade `0005_operations → base` успешно скомпилированы в offline PostgreSQL SQL.
- Docker CLI/Compose установлен, но текущая среда запрещает доступ к `/var/run/docker.sock`, поэтому живой PostgreSQL integration gate ещё не выполнен.

## Known issues / blockers

- Нужен доступный PostgreSQL либо `TEST_DATABASE_URL`, чтобы выполнить четыре integration-теста и `alembic check` против живой схемы. SQLite fallback не создавать.
- Docker daemon текущему процессу недоступен, несмотря на наличие Docker CLI.
- Не раскрывать и не сохранять credentials вне `.env`.
- Перед публичным KZ-релизом пользователь должен заполнить и проверить статические KZ-строки.

## Remaining work

- Выполнить этапы 3–8 `docs/plans/terricon-events-bot-v1.md` по порядку.
- При появлении PostgreSQL запустить `TEST_DATABASE_URL=... uv run pytest -m postgres` и не заявлять о live migration validation до этого.
- После полной реализации и приёмки пометить технический план завершённым.

## Next recommended step

После прямого указания пользователя начать этап 3: DTO фактического Terricon API, HTTP client/retry policy, нормализация, транзакционный sync/baseline/missing streak/hash/diff и read-only smoke-команда. Не переходить к OpenAI-адаптеру этапа 4 до отдельного подтверждения.
