# План: гибридный источник и программы (2026-10-26)

Статус: завершён и выкачен 2026-10-26 (`b4102d9`).
Спецификация: [`docs/specs/hybrid-source-programs.md`](../specs/hybrid-source-programs.md).

## 1. Схема — миграция `0012_hybrid_source`

- Enum `sync_source` (`stage`, `legacy`).
- `events.event_type TEXT NULL` (нижний регистр `eventType`; у существующих
  строк заполнится первым циклом).
- `sync_runs.source sync_source NULL` (исторические циклы — NULL).
- `sync_state.fallback_since TIMESTAMPTZ NULL` — начало текущего отката.
- `sync_endpoint_states` и `sync_run_endpoints`: колонка `source sync_source NOT NULL
  DEFAULT 'legacy'`, `source_theme` становится NULL для `stage`. У
  `sync_endpoint_states` составной PK заменяется identity `id` +
  `UNIQUE NULLS NOT DISTINCT (source, locale, source_theme)`; у `sync_run_endpoints`
  уникальность расширяется тем же образом (PostgreSQL 17).
- Downgrade удаляет `stage`-строки endpoint и возвращает прежние ключи.
- Модели, `test_schema_metadata`, `test_postgres_schema`, restore-smoke
  (`0012_hybrid_source`, таблиц столько же) и runbook.
- Вариантозависимые колонки (раздел 7) добавляются в эту же миграцию.

## 2. DTO и нормализация

- `TerriconEventDTO.type_text` необязателен.
- `_is_stub()` для `-`, `/`, `- -`; применяется к ссылкам (без `invalid_url`),
  адресу, аудитории, описанию, спикеру, языкам.
- `normalize_event(dto, locale, expected_theme: SourceTheme | None)`: при `None`
  тема из `type`, для `default`/неизвестного — из `tags` (it > business > marketing,
  иначе it), без `theme_mismatch`.
- `SourceEvent.event_type`, `domain/program_kind.py`: `ProgramKind` и `kind_of()`.
- `details_url`: страница темы → `#uid`; иная страница `terricon.kz` → `#form`;
  внешняя — как есть.
- `unknown_status`: без issue; `registration_url` — каноническая страница заявки
  (`/kz/` → `/ru/`, `#form`) или внешний сайт; `recording_url` — ссылка действия.
- Unit-тесты на обезличенной выборке живого ответа
  (`tests/fixtures/terricon/events_by_stage_{ru,kz}.json`: по 1–2 записи каждого
  вида + 2 обычных события).

## 3. Клиент

- `TerriconClient.fetch_stage(locale) -> EndpointPayload`: тот же цикл повторов,
  ответ — объект с массивами `eventsNew`/`eventsPreparation` (иначе
  `invalid_response_shape`), записи обеих групп, дедуп по `id`.
- `_fetch_records` параметризуется URL и параметрами запроса.
- `STAGE_URL` (необязательный; по умолчанию сосед `BASE_URL`) и
  `SOURCE_FALLBACK_ALERT_HOURS=24` в `config.py`, `.env.example`, composition.

## 4. SyncService

- `run()`: `stage` для RU/KZ; при любом неуспехе — шесть `legacy` запросов.
  Аудит `SyncRunEndpoint` пишется для всех выполненных запросов (с `source`).
- Источник цикла в `SyncRun.source`; `SyncState.fallback_since` ставится при первом
  `legacy`-цикле и очищается успешным `stage`.
- Пропадание: `stage` (оба языка) — все доступные события, `complete_themes`
  заменяется флагом «источник полный»; `legacy` — счётчик не растёт.
- Новое событие с прошедшим днём (`Asia/Almaty`) — `notify_new=False`.
- Alerts: сбой `stage` при успешном резерве — без `sync_endpoint_failure`;
  `sync_source_fallback` при `now - fallback_since >= порог`, закрытие при `stage`.
- `event_type` — новое общее поле (не влияет на semantic/important hash).
- Integration-тесты по разделу 7 спецификации.

## 5. Админ и smoke

- `AdminStatus`: источник последнего цикла, `fallback_since`; endpoint с меткой
  источника. `/status` показывает «Источник: eventsByStage» или
  «Резерв getAllEvents с …».
- `terricon-api-smoke`: 2 `stage` + 6 `legacy`, число записей и невалидных.

## 6. Показ программ (вариант C)

- `CatalogFilter.scope` (`events`/`programs`) и `program_kind`; `CatalogQuery`
  исключает программы из `events` и показывает только их в `programs`.
  `EventSummary`/`EventDetails` получают `event_type`.
- `CatalogStateStore`: активное состояние + «припаркованное» состояние другого
  раздела в той же строке `fsm_states` (старый формат читается как `events`).
  `switch(user, scope)` вызывается из меню.
- Callbacks: `NavigationView.PROGRAMS`, `ProgramKindCallback`,
  `ProgramKindAllCallback`, `SubscriptionProgramsCallback`.
- Views: меню `[Мероприятия][Программы]`, список программ с переключателем вида,
  бейджи, карточка по разделу 5 спецификации, тумблер подписки.
- Подписка: `users.program_notifications BOOLEAN NOT NULL DEFAULT false`
  (миграция 0012); `SubscriptionSummary.programs`, `toggle_programs()`.
- `OutboxService._recipient_ids`: событие-программа → только пользователи с
  `program_notifications`; иначе прежняя логика. `DigestEntry.event_type` → бейдж.
- RU/KZ-строки; тесты views/router/catalog/subscriptions/delivery.

## 7. Gate

- `ruff check`, `ruff format --check`, `mypy`, `uv lock --check`.
- Полный `pytest` на одноразовом PostgreSQL 17 (`TEST_DATABASE_URL`), Alembic
  upgrade/downgrade/upgrade.
- `docker compose build`, живой `terricon-api-smoke` (8 endpoint).
- Остановка: отчёт пользователю, коммит только после его «ок».

## 8. Выкатка (по runbook) и проверка

1. Backup + `restore-smoke` (до миграции, ожидается `0011`).
2. `docker compose build`; `docker compose run --rm migrate` (0011 → 0012).
3. Preflight; `docker compose up -d app` — выполняет пользователь (`!`-команда).
4. Healthcheck true/true/true, `terricon-api-smoke`, логи.
5. Проверка в БД (read-only): последний `sync_runs.source='stage'`, ~290 событий,
   `event_type` заполнен, domain changes за цикл только по будущим записям;
   `/status` показывает источник.
6. Память: `projects/terricon-events-bot.md`, `CACHE.md`, статус плана.

## Итог реализации (локально, до коммита)

- Gate: ruff/format/mypy/lock/diff clean; **320 passed** на одноразовом PostgreSQL 17
  (+26 тестов: нормализация/клиент/smoke `eventsByStage`, гибридный sync, каталог и
  парковка состояния разделов, подписка и получатели программ, экраны, роутер, `/status`).
- Alembic 0011 → 0012 → 0011 → 0012 на копии production-данных; `alembic check` чист.
- Пробный цикл на свежей read-only копии production (миграция 0012, живой API, без
  Telegram/Groq): `source=stage`, 580 записей, +81 событие, 0 изменённых существующих,
  0 domain changes; `NEW_EVENT` ожидает только неконференция 30.10; повторный цикл — 0/0.
  Каталог: «Мероприятия» — 3 актуальных (вкл. неконференцию), «Программы» — 0 актуальных,
  22 в архиве.
- `terricon-api-smoke`: 8/8 ok (6×`getAllEvents` 209 записей, 2×`eventsByStage` по 290).
- Docker-образ собран под отдельным тегом `terricon-events-bot-app:hybrid-check`.
- Новые KZ-строки (меню, программы, виды, подписка) переведены Claude — нужна проверка.

## Выкатка 2026-10-26

Backup `backups/terricon-20261026T172920255094783Z-213021.dump` (restore smoke OK на
0011, 27 таблиц) → `docker compose build` → migrate 0011 → 0012 → preflight OK →
`up -d app` (пользователь). Healthcheck true/true/true, api-smoke 8/8, 0 ошибок в логах.
Первый цикл (sync_run 25): `stage`/`succeeded`/580 записей, 290 событий с `event_type`.
Единственная domain change — `NEW_EVENT` неконференции 30.10 после её классификации.
