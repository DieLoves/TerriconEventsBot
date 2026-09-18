# Handoff

## Objective

Этап 6 Terricon Events Bot v1 завершён. Следующий разрешённый этап после отдельного указания
пользователя — этап 7: feedback и admin-функции. До такого указания не начинать этап 7 и не
добавлять scheduler/deploy из этапа 8.

## Current state

- Этапы 1–6 завершены.
- Этап 4 закоммичен локально как `f2e7a8e feat: implement OpenAI event processing`.
- Полностью проверенные изменения этапов 5 и 6 остаются в рабочем дереве и не закоммичены.
- Этап 6 добавил атомарные подписки, материализацию domain changes, quiet-hours digest,
  restart-safe outbox worker и Telegram gateway.
- `.env` игнорируется Git. Не выводить, не перезаписывать и не коммитить его содержимое.

## Completed in stage 6

- `SubscriptionService.replace()` меняет `all|categories|none` под блокировкой пользователя и
  одной транзакцией; UI позволяет выбрать «Все» либо любое множество категорий.
- Миграция `0008_stage6_delivery_state` добавляет `domain_changes.materialized_at`, partial index
  необработанных changes и индекс claimable outbox. Это закрывает случай без получателей: старое
  изменение не будет доставлено пользователю, подписавшемуся позже.
- `OutboxService.materialize()` конкурентно выбирает changes через `FOR UPDATE SKIP LOCKED`,
  объединяет old/new categories, создаёт идемпотентные delivery rows и отмечает change
  материализованным в той же транзакции.
- В тихие часы 22:00–09:00 `Asia/Almaty` deliveries одного пользователя объединяются в один
  отложенный logical batch. Событие внутри digest дедуплицируется, включая совпадение нескольких
  категорий.
- Большой digest разбивается на Telegram-safe chunks. Все chunks имеют общий `batch_id`, а payload
  хранит точное соответствие chunk → delivery IDs.
- `DeliveryWorker` получает outbox через `FOR UPDATE SKIP LOCKED`, восстанавливает stale
  `processing` lease, сохраняет каждый подтверждённый message ID и продолжает с первого
  неподтверждённого chunk.
- Повторяются только transient Telegram failures. Permanent failure закрывается как `failed`.
  Telegram 403 деактивирует пользователя и переводит все его незавершённые outbox/deliveries в
  `blocked`.
- `Clock` и `TelegramGateway` являются протоколами application layer; aiogram изолирован в
  `AiogramTelegramGateway`.
- Composition root создаёт subscription/outbox/delivery services, но scheduler jobs намеренно не
  регистрируются до этапа 8.

## Key decisions

- Исходной схеме этапа 2 не хватало durable-признака обработки change без получателей.
  `materialized_at` добавлен отдельной линейной миграцией вместо попытки выводить обработанность по
  отсутствующим delivery rows.
- Подтверждение сохраняется после каждого успешно отправленного chunk; уже зафиксированные chunks
  не отправляются повторно после штатного рестарта или transient retry.
- Весь внешний event title отправляется plain text, без HTML parse mode. Digest ограничивается
  4000 символами при Telegram limit 4096.
- Получатели important change выбираются по объединению `old_categories` и `new_categories`.
  `subscribe_all` и category rows остаются взаимоисключающими на уровне FK/constraint.
- Outbox отправляется только активным пользователям с действующим доступом. PostgreSQL остаётся
  единственным источником истины.

## Relevant files

- Подписки: `src/terricon_events_bot/application/subscriptions.py`.
- Materialization/digest/worker: `src/terricon_events_bot/application/delivery.py`.
- Aiogram gateway: `src/terricon_events_bot/telegram/delivery.py`.
- UI callbacks/router/views: `src/terricon_events_bot/telegram/`.
- Delivery models: `src/terricon_events_bot/infrastructure/models/delivery.py`.
- Migration: `alembic/versions/0008_stage6_delivery_state.py`.
- Composition: `src/terricon_events_bot/composition.py`.
- Stage-6 tests: `tests/unit/test_delivery.py`, `tests/unit/test_telegram_delivery.py`,
  `tests/integration/test_subscription_service.py`, `tests/integration/test_delivery_service.py`.

## Repository state

- Root: `/home/dev/workspace/projects/terriconparser`.
- Branch: `main`, tracking `origin/main`, ahead by 4 commits.
- HEAD: `f2e7a8e`.
- Изменения этапов 5 и 6 находятся в одном незакоммиченном рабочем дереве; не откатывать и не
  отделять их без отдельного решения пользователя.
- Push и commit в ходе этапа 6 не выполнялись.

## Validation

- Полный PostgreSQL 17 gate: `164 passed`.
- В него вошли Alembic `base → head → base`, `alembic check`, schema/enum round trips,
  конкурентная материализация и concurrent workers.
- Ruff check: passed.
- Ruff format check: passed.
- mypy strict: passed.
- `git diff --check`: passed.
- Alembic: одна head `0008_stage6_delivery_state`.
- `uv lock --offline --check`: passed.
- Реальные Telegram, OpenAI и Terricon вызовы не выполнялись.

## Known issues / blockers

- Известных блокеров этапа 6 нет.
- KZ UI по-прежнему содержит TODO и использует RU fallback; public KZ mode блокируется readiness
  gate.
- Decorative assets по-прежнему отсутствуют, кроме `.gitkeep`; fallback без изображений штатный.

## Remaining work

- Этап 7: feedback wizard, privacy deletion, admin tickets/commands and broadcasts.
- Этап 8: scheduler job registration, polling/lifecycle, heartbeat, Docker/deploy and backup tools.

## Next recommended step

После отдельного указания пользователя начать этап 7 с аудита существующих feedback/broadcast/FSM
таблиц и миграции `0004_feedback_broadcasts.py`. До этого остановиться.
