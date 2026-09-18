# Handoff

## Objective

Этап 3 Terricon Events Bot v1 — импорт публичного Terricon API — завершён по
`docs/specs/terricon-events-bot.md` и `docs/plans/terricon-events-bot-v1.md`. Остановиться и
ждать отдельного разрешения пользователя перед этапом 4 (OpenAI-адаптером).

## Current state

- Этапы 1–2 завершены и находятся в `origin/main`; этап 3 полностью реализован в рабочем
  дереве, но ещё не закоммичен.
- Реализованы DTO/нормализация, `TerriconClient`, транзакционный `SyncService`, hashes/diff,
  baseline/partial failure/missing streak, domain changes, admin alerts и read-only smoke.
- Фактический live API принимает все 406 записей шести endpoint без invalid records.
- `.env` игнорируется Git; его содержимое нельзя выводить, перезаписывать или коммитить.

## Completed

- Этап 1: commit `2720fb1` — scaffold, конфигурация, composition root, async SQLAlchemy/UoW,
  Alembic, enum, RU/KZ locale, assets и структурированные логи.
- Этап 2: commits `1a54793`, `211649f` — PostgreSQL-схема, пять исходных миграций и исправление
  соответствия миграций metadata.
- Read-only аудит актуального API подтвердил единый контракт всех шести endpoint, полное
  совпадение RU/KZ-наборов ID внутри каждой темы и отсутствие пересечений ID между темами.
  Поэтому один `events.source_theme` оставлен без усложнения схемы.
- Выявлено, что верхнеуровневый API `url` локализован сегментом `ru|kz`, а
  `card_info.url_live` является общей ссылкой. Добавлена миграция
  `0006_localized_details_url`, переносящая `details_url` из `events` в
  `event_localizations` с сохранением данных при upgrade и детерминированным RU→KZ fallback
  при downgrade.
- Исправлена утечка `DATABASE_URL` из PostgreSQL session-fixture в unit-тесты, которая
  проявлялась только при совместном полном запуске.
- `Redactor` теперь рекурсивно очищает вложенные mapping/sequence/set, числовые Telegram ID,
  ключи mapping и строковые представления произвольных объектов; циклические контейнеры
  обрабатываются без падения форматтера. Добавлены unit-тесты всех этих путей.
- Добавлены обезличенные fixtures всех `ru|kz × it|business|marketing`, строгие Pydantic DTO и
  нормализация UTC-дат, HTTP(S)-URL, форматов, языков, статуса и локализованных полей.
- `TerriconClient` выполняет initial request + 3 retry, exponential backoff с jitter,
  `Retry-After` (seconds и HTTP-date), timeout и semaphore; отдельные невалидные записи
  пропускаются, но распознанные source ID остаются observed для защиты missing streak.
- `SyncService` объединяет локали с RU-приоритетом, сохраняет цикл одной транзакцией,
  поддерживает partial failure и baseline только после 6/6 endpoint, скрывает событие после
  двух полных missing-циклов и восстанавливает при повторном появлении.
- Semantic/important hashes разделены; structured diff и domain changes идемпотентны и
  создаются только после baseline. Endpoint/common mismatch alerts дедуплицируются.
- Добавлена read-only команда `terricon-api-smoke`, подключение импорта к composition root и
  корректное закрытие HTTP-клиента/DB engine.
- Live integration-тесты выявили и исправили старый ORM-дефект: PostgreSQL enum ранее
  возвращался как `str` после reload. `enum_type` теперь делает round-trip в доменный enum без
  изменения SQL-схемы.
- Post-implementation аудит закрыт hardening-проходом: полностью невалидный непустой payload
  теперь считается endpoint failure; HTTP body и `Retry-After` ограничены; URL проверяются
  строже; описания сохраняют переносы строк; исчезнувшие source alerts закрываются после
  полного подтверждённого цикла; скрытый архив исключён из missing-выборки; rollback основной
  DB-транзакции оставляет отдельную failed audit-запись `sync_runs`.

## Key decisions

- Приоритет: спецификация → технический план → текущая реализация. Scope v1 не расширять.
- Всегда запрашивать ровно шесть тематических endpoint; endpoint без `theme` не использовать.
- Общие поля объединять по source ID с приоритетом RU, затем KZ; реальные расхождения
  дедуплицировать через admin alert. Локализованный верхнеуровневый `url` расхождением не
  является.
- Baseline завершается только после полностью успешного цикла из шести endpoint.
- Частичный сбой не портит сохранённые данные и не увеличивает missing streak.
- Невалидная запись с распознаваемым положительным ID считается observed и не может ложно
  приблизить событие к скрытию.
- Semantic hash и important hash/diff имеют разные назначения; повторный импорт должен быть
  идемпотентным, а domain changes создаются только после baseline.

## Relevant files

- `docs/specs/terricon-events-bot.md` — требования, особенно разделы 3, 4, 5, 9 и 10.
- `docs/plans/terricon-events-bot-v1.md` — этап 3 и testing gate; содержит уточнение о
  локализованном API `url`.
- `docs/plans/stage-3-import-hardening.md` — завершённый план исправлений post-stage audit.
- `src/terricon_events_bot/infrastructure/terricon/` — DTO, нормализация, HTTP-клиент и smoke.
- `src/terricon_events_bot/application/sync.py` — orchestration и транзакционный merge/upsert.
- `src/terricon_events_bot/domain/events.py`, `domain/event_changes.py` — импортируемые данные,
  semantic/important hashes и structured diff.
- `src/terricon_events_bot/composition.py` — wiring и lifecycle ресурсов импорта.
- `alembic/versions/0006_localized_details_url.py` — новая, пока untracked миграция.
- `tests/fixtures/terricon/` — шесть обезличенных API fixtures.
- `tests/integration/test_sync_service.py` — baseline, partial/total failure, missing/recovery,
  idempotency, RU priority и alerts.

## Repository state

- Root: `/home/dev/workspace/projects/terriconparser`.
- Branch: `main`, tracking `origin/main`.
- HEAD: `211649f fix: align PostgreSQL migrations with metadata`.
- Рабочее дерево содержит все незакоммиченные изменения подготовительных подэтапов и полной
  реализации этапа 3; подробный список доступен через `git status --short`.
- Коммит и push текущего подэтапа не выполнялись.
- On-disk `AGENTS.md` в проекте и родительском каталоге не найден; применять инструкции,
  переданные пользователем в сессии.

## Validation

- Полный gate на одноразовом PostgreSQL 17: `89 passed`; unit и integration тесты прошли одним
  процессом, включая миграции `base → 0006 → base`, schema drift, enum round-trip и все
  SyncService-сценарии, в том числе rollback audit и исключение скрытых событий из повторных
  обновлений.
- `ruff check`, `ruff format --check`, `mypy`, `git diff --check` и
  `uv lock --offline --check` прошли.
- Read-only live smoke: `ru/kz × it/business/marketing` — 6/6 успешно, 406 валидных записей,
  каждая комбинация завершилась с первой попытки.
- Одноразовый PostgreSQL-контейнер остановлен и автоматически удалён (`--rm`).

## Known issues / blockers

- Блокеров для следующего подэтапа нет.
- KZ UI содержит ожидаемые TODO до публичного KZ-релиза.

## Remaining work

- В этапе 3 обязательных работ не осталось.
- Этап 4: OpenAI-адаптер и durable classification queue — только после разрешения пользователя.

## Next recommended step

Остановиться. Следующий разрешённый шаг — этап 4, только по отдельному указанию пользователя.
