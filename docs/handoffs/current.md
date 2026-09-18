# Handoff

## Objective

Этап 4 Terricon Events Bot v1 завершён. В следующей сессии начинать только этап 5 — каталог и
Telegram UI — и только после отдельного указания пользователя. После этапа 5 снова остановиться.

## Current state

- Этапы 1–4 завершены; этап 5 не начат.
- Реализованы OpenAI Responses adapter, строгие Pydantic Structured Outputs для классификации и
  перевода, durable classification queue, usage/budget ledger, retry/fallback и интеграция с
  импортом.
- Новое post-baseline событие больше не создаёт `NEW_EVENT` при импорте: domain change появляется
  только после `completed` или финального `fallback` классификации.
- Машинная локаль создаётся только при отсутствии RU/KZ и помечается `machine`; следующий успешный
  импорт официальной локали детерминированно заменяет её. Изменение исходной официальной локали
  инвалидирует устаревший машинный перевод перед повторной классификацией.
- Post-audit этапа 4 завершён: устранены преждевременные lease для batch, гонка создания budget
  alert, запоздалое обнаружение пересечения бюджета и преждевременные domain changes для ещё не
  опубликованного события.
- Реальные OpenAI-вызовы не выполнялись; все contract-тесты используют mock.
- `.env` игнорируется Git. Не выводить, не перезаписывать и не коммитить его содержимое.

## Completed

- Этап 1: `2720fb1` — фундамент проекта.
- Этап 2: `1a54793`, `211649f` — PostgreSQL-схема.
- Этап 3: `9948ec7`, `788dd07`, `f4daccb` — устойчивый импорт и документация.
- Этап 4 (рабочее дерево, коммит ещё не создан):
  - `OpenAIEventAdapter` на `AsyncOpenAI.responses.parse`, `store=False`,
    `reasoning.effort=low`, Pydantic strict schema и повторная валидация;
  - отдельные `classify_event` и `translate_missing_locale`;
  - категория 1–3, уникальность и исключительность `other`;
  - queue `pending → completed|retry_wait|fallback`, `FOR UPDATE SKIP LOCKED`, lease от повторного
    платного вызова и 24-часовой fallback; batch резервирует задачи непосредственно перед вызовом,
    поэтому поздние задачи не теряют lease в ожидании предыдущих;
  - immediate `other` при ошибке и delayed `NEW_EVENT` до финального результата;
  - semantic-only requeue: дата, URL и постер не сбрасывают завершённую классификацию;
  - monthly usage ledger, configurable pricing, soft budget и дедуплицированный admin alert;
  - race-safe PostgreSQL upsert budget alert и немедленный alert, если последний платный вызов
    пересёк мягкий лимит;
  - учёт usage даже для refusal/schema-invalid платных ответов;
  - машинный перевод отсутствующей локали с provenance и защитой official-over-machine;
  - классификация использует только официальные локализации; машинная локаль не влияет на semantic
    и important hashes, а устаревшая версия удаляется при изменении исходного текста;
  - `max_output_tokens`, ограничение входных полей и конечные числовые значения конфигурации
    ограничивают стоимость и исключают некорректные `NaN`/`Infinity`;
  - composition root и корректное закрытие OpenAI-клиента.

## Key decisions

- Официальная документация 2026-09-18 подтверждает для `gpt-5.6-luna`: Responses API,
  Structured Outputs, `reasoning.effort=low`, $0.20/1M input и $1.20/1M output.
- SDK 2.54.0 поддерживает async `responses.parse(..., text_format=PydanticModel)`; встроенные
  retry отключены (`max_retries=0`), потому что retry state хранится в PostgreSQL.
- Classification job резервируется установкой lease в `next_attempt_at`; после падения worker-а
  задание снова доступно через 10 минут. Worker резервирует по одной задаче, чтобы lease не истёк,
  пока задача ждёт обработки внутри большого batch.
- 24-часовое окно начинается при постановке задания, поэтому исчерпанный бюджет не может навечно
  удерживать событие без уведомления.
- Existing `NUMERIC(12,6)` технически не мог хранить стоимость одного входного токена Luna
  ($0.0000002). Миграция `0007_openai_cost_precision` меняет обе стоимости на `NUMERIC(18,9)`.
- Semantic hash строится только по официальным локализациям: добавление машинного перевода не
  вызывает ложную переклассификацию.
- Пока post-baseline событие ожидает свой первый `NEW_EVENT`, его промежуточные изменения и
  временное скрытие не создают `IMPORTANT_CHANGE`/`CANCELLATION`; после восстановления уведомление
  выпускается только с актуальной ревизией и категориями.
- Budget — мягкий: один завершившийся вызов может слегка превысить лимит; следующие вызовы
  приостанавливаются до нового месяца/изменения лимита.

## Relevant files

- `src/terricon_events_bot/domain/ai.py` — строгие request/output DTO и результаты.
- `src/terricon_events_bot/infrastructure/openai_adapter.py` — Responses adapter.
- `src/terricon_events_bot/application/classification.py` — queue worker, budget, ledger,
  categories, fallback, translation и release domain changes.
- `src/terricon_events_bot/application/sync.py` — enqueue/requeue и official-over-machine.
- `alembic/versions/0007_openai_cost_precision.py` — точность стоимости.
- `src/terricon_events_bot/config.py`, `composition.py`, `.env.example` — pricing/timeout/wiring.
- `tests/unit/test_openai_adapter.py`, `tests/unit/test_classification.py` — contract/unit tests.
- `tests/integration/test_classification_worker.py`, `test_sync_service.py` — PostgreSQL state
  machine и интеграция импорта.

## Repository state

- Root: `/home/dev/workspace/projects/terriconparser`.
- Branch: `main`, tracking `origin/main`, до этапа 4 была ahead на 3 коммита.
- HEAD: `f4daccb docs: finalize stage 3 handoff`.
- Изменения этапа 4 находятся в рабочем дереве и ещё не закоммичены/не отправлены.
- Временный symlink `None`, созданный только из-за ошибочного writable-root sandbox, удалён и в
  Git не попал.

## Validation

- Полный gate на PostgreSQL 17: `123 passed`, включая миграции `base → 0007 → base`, schema
  drift, lease/batch concurrency, race-safe budget alert, paid-error usage, 24-hour fallback,
  invalidation машинного перевода и delayed `NEW_EVENT`.
- `ruff check`, `ruff format --check`, `mypy`, `git diff --check`, Alembic head и
  `uv lock --offline --check` прошли.
- `pip-audit` по production-зависимостям из `uv.lock`: известных уязвимостей не найдено
  (локальный пакет проекта ожидаемо не публикуется на PyPI).
- Реальные OpenAI и Telegram вызовы не выполнялись.

## Known issues / blockers

- Блокеров этапа 4 не выявлено.
- Scheduler запуска classification worker только на этапе 8; сам worker уже подключён к
  composition root и доступен для будущего job registration.
- Доставка admin alerts появится в admin-функциях следующих этапов; сейчас alert надёжно и
  дедуплицированно сохраняется в БД.
- KZ UI всё ещё содержит ожидаемые TODO до публичного KZ-релиза; это не относится к этапу 4.

## Remaining work

- Этап 5: каталог и Telegram UI строго по спецификации и плану.
- Не начинать подписки/outbox (этап 6), admin/feedback (этап 7) или scheduler/deploy (этап 8)
  раньше соответствующего этапа.

## Next recommended step

После подтверждения пользователя прочитать разделы 2 и 5 спецификации и этап 5 плана, затем
сопоставить будущий `CatalogQuery`/renderer с текущими моделями, локализациями и image resolver.
