# Handoff

## Objective

По явному указанию пользователя следующая сессия должна сначала проверить, закоммитить и
отправить в `origin/main` **все** текущие изменения. Затем нужно отдельно подтвердить, есть ли в
репозитории временные заглушки пробного запуска, удалить только реально временные элементы и
продолжить оставшуюся часть этапа 8 по `docs/plans/terricon-events-bot-v1.md`.

Этот handoff создан навыком `$handoff`; поэтому commit, push и реализация в текущем ходе не
выполнялись.

## Current state

- Этапы 1–6 находятся в `main`; HEAD и локальный `origin/main` указывают на
  `21091d4 feat: add catalog subscriptions and reliable delivery`.
- Этап 7 завершён, но вместе с уже реализованной частью этапа 8 остаётся в незакоммиченном
  рабочем дереве.
- Этап 8 фактически уже начат: runtime, scheduler, lifecycle, heartbeat, healthcheck и Docker
  Compose реализованы. Не начинать его заново; продолжать с оставшихся operations-задач.
- `.env` игнорируется Git. Его актуальные значения в этом handoff не проверялись; не выводить,
  не коммитить и не перезаписывать секреты.
- Пользователь уже пробовал Docker-запуск и получил `InvalidPasswordError` для PostgreSQL.
  Причина была диагностирована как несовпадение нового `POSTGRES_PASSWORD` с паролем в ранее
  инициализированном persistent volume. Текущее состояние контейнеров и разрешение этой ошибки
  после последующих действий пользователя не проверялись.

## Completed

- Этап 7: DB-backed feedback, privacy deletion, admin-команды и restart-safe локализованные
  broadcasts с lease, per-message acknowledgement и итоговым отчётом. См. application/Telegram
  файлы и миграцию `alembic/versions/0009_stage7_broadcast_state.py`.
- Текущая часть этапа 8: `terricon-bot`, `terricon-preflight`, `terricon-healthcheck`, aiogram
  polling, APScheduler jobs, PostgreSQL advisory locks, graceful shutdown, heartbeat/admin alerts.
- Добавлены multi-stage non-root `Dockerfile`, `compose.yaml`, `.dockerignore`, migration service,
  persistent PostgreSQL volume и container healthcheck.
- Конфигурация валидирует Telegram token и PostgreSQL URL до создания клиентов; ошибки settings
  перечисляют поля без значений секретов.
- README содержит порядок первого Docker-запуска и остановки без удаления volume.

## Key decisions

- Очереди и прогресс остаются в PostgreSQL; APScheduler только запускает обработчики.
- Sync, classification и broadcast защищены advisory locks и `max_instances=1`.
- Compose переопределяет `DATABASE_URL` адресом сервиса `db` и строит его из
  `POSTGRES_PASSWORD`. Смена переменной не меняет пароль в уже созданном PostgreSQL volume.
- Не выполнять `docker compose down -v` без явного подтверждения потери данных.
- По поиску репозитория временных кодовых заглушек пробного запуска не обнаружено. Временные
  credentials использовались только как process-local environment overrides для container
  preflight и в файлы не записывались.
- `replace-me`/примерные ID в `.env.example` — документированные placeholders, а `TODO` в
  `locales/kz.yaml` — предусмотренный спецификацией RU fallback/readiness gate. Не удалять их как
  «тестовые заглушки» без отдельного решения пользователя.

## Relevant files

- Спецификация: `docs/specs/terricon-events-bot.md`.
- План: `docs/plans/terricon-events-bot-v1.md`.
- Runtime/operations: `src/terricon_events_bot/runtime.py`,
  `src/terricon_events_bot/application/operations.py`.
- Composition/config: `src/terricon_events_bot/composition.py`,
  `src/terricon_events_bot/config.py`.
- Этап 7: `src/terricon_events_bot/application/{admin,broadcasts,feedback}.py`,
  `src/terricon_events_bot/telegram/{admin,broadcasts,feedback}.py`.
- Containers: `Dockerfile`, `compose.yaml`, `.dockerignore`, `.env.example`.
- Operations tests: `tests/integration/test_operations_service.py`,
  `tests/unit/test_runtime.py`.

## Repository state

- Root: `/home/dev/workspace/projects/terriconparser`.
- Branch: `main`, tracking `origin/main`; inspected ahead/behind count: `0/0`.
- HEAD: `21091d4`.
- Рабочее дерево существенно изменено: 23 tracked-файла modified и новые файлы этапов 7–8
  (migration, application/Telegram services, Docker/runtime и tests). Перед staging использовать
  `git status --short` как источник истины и просмотреть полный diff.
- `.env` не отображается в status и не должен попасть в commit.

## Validation

Фактически выполненные проверки до создания этого handoff:

- полный PostgreSQL 17 gate: `186 passed`;
- Ruff check и Ruff format check: passed;
- mypy strict: passed;
- `uv lock --offline --check`: passed;
- `git diff --check`: passed, в том числе повторно при подготовке handoff;
- Alembic: одна head `0009_stage7_broadcast_state`; clean migration прошла;
- `docker compose config --quiet` и multi-stage image build: passed;
- container preflight с временными environment overrides: `Preflight OK`;
- реальные Telegram, OpenAI и Terricon вызовы в автоматических проверках не выполнялись.

Новый полный тестовый прогон специально для handoff не запускался.

## Known issues / blockers

- Перед commit нужно просмотреть весь diff: изменения этапов 7 и 8 накоплены вместе и ещё ни разу
  не зафиксированы после `21091d4`.
- Состояние локального `.env` неизвестно; прежнее утверждение о конкретно отсутствующих полях могло
  устареть после ручной настройки пользователем.
- PostgreSQL volume может по-прежнему содержать пароль от первоначальной инициализации. Сначала
  проверить текущее состояние, не удалять volume автоматически.
- KZ locale содержит предусмотренные спецификацией `TODO`; public KZ mode должен оставаться
  заблокированным readiness gate.
- Этап 8 ещё не завершён: отсутствуют host `pg_dump`, ротация семи архивов, restore smoke и
  production rollout runbook.

## Remaining work

1. Проверить diff/status, убедиться в отсутствии секретов, закоммитить все изменения и выполнить
   `git push origin main` — пользователь явно запросил push.
2. После push провести адресный аудит «заглушек». Не считать временными `.env.example` и KZ TODO
   автоматически; сначала сопоставить каждый кандидат со спецификацией.
3. Продолжить оставшуюся часть этапа 8: backup/rotation, restore smoke и deploy/rollout docs.
4. После каждого логического шага запускать соответствующие проверки; в конце этапа — полный gate
   и остановка перед дальнейшей работой.

## Next recommended step

Начать с `git status --short` и полного staged/unstaged diff-аудита. Если секретов и случайных
файлов нет, добавить все изменения, ещё раз просмотреть staged diff, создать commit(ы) этапов 7 и
текущей части этапа 8 и выполнить разрешённый пользователем `git push origin main`. Только после
успешного push переходить к аудиту заглушек.
