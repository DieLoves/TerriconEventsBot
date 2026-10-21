# Handoff

## Groq — текущее дополнение 2026-10-21

Этап 9 завершён и опубликован: `openai` SDK полностью удалён; используется `groq==0.37.1`,
`AsyncGroq`/Chat Completions и `openai/gpt-oss-120b`, strict JSON Schema + Pydantic.
Все AI-настройки теперь `GROQ_*`; цены Free по умолчанию 0/0, usage сохраняется.
Физические SQL-имена `openai_usage`/`openai_operation` сохранены для совместимости;
новых миграций не требуется. Runtime и admin используют нейтральные AI-типы.

`Retry-After` сохраняется в очереди; 429 останавливает batch. Перевод имеет durable
retry/lease на completed classification и восстанавливается без переклассификации
и дублей уведомлений. Официальная локаль и 24-часовой deadline прекращают retry.
Malformed ответы нормализуются, SDK debug dumps исключены из application logs.

Финальный gate: **235 passed** с отдельной PostgreSQL 17; Ruff/format/mypy/lock/diff,
package build и Docker build PASS. В venv и проверочном Docker-образе OpenAI SDK
отсутствует. Независимое review выявило 3 Important; все воспроизведены и исправлены,
после чего suite повторён; повторное независимое review не выполнялось.

С разрешения пользователя выполнены 3 реальных Groq-запроса: RU/KZ classification,
RU → KZ translation, HTTP 200, 1230 токенов, 334–607 мс. Ключ вводился скрыто только
в процесс и не сохранялся. `.env` не менялся. Для запуска отдельно настроить
`GROQ_API_KEY`; старые `OPENAI_*` не используются. Бот не запускался.

Этап 10 открыт: 20–30 размеченных реальных событий, оба направления перевода,
полная проверка квот и общий throttling. Первые 3 synthetic smoke этого не заменяют.
Истина: `docs/specs/groq-migration.md`, `docs/plans/groq-migration.md`,
`docs/operations/groq-smoke-2026-10-21.md`. По запросу пользователя от 2026-10-21
Groq и все прежние audit/operations-изменения опубликованы в `origin/main`.
Статус публикации указан ниже; production rollout не выполнялся.

## Objective (исходные этапы 1–8)

Спецификация `docs/specs/terricon-events-bot.md` и основной план
`docs/plans/terricon-events-bot-v1.md` полностью сопоставлены с кодом, миграциями, тестами,
Docker/runtime и operations-документацией. Найденные compliance- и hardening-расхождения
устранены; оба постреализационных аудита завершены.

## Git state — публикация 2026-10-21

- Исходная база: `1f0b955 feat: add administration and production runtime`.
- Пользователь разрешил commit/push в существующий `origin/main`:
  `git@github.com:DieLoves/TerriconEventsBot.git`. Перед публикацией выполнен fetch;
  изменений на remote поверх исходной базы не было.
- В публикацию входят operations-дополнения этапа 8, compliance/hardening, migration
  `0010_hardening_queue_state`, assets, документация и Groq этап 9.
- Код опубликован коммитом `e175f1512455d47d3ee2ada89534cd7ba0b865a8`
  (`feat: migrate event AI to Groq and complete v1 hardening`), 85 файлов.
  Push выполнен без force; SHA `refs/heads/main` независимо подтверждён через
  `git ls-remote`. После code push рабочее дерево было чистым.
- Этот handoff и статусы планов уточнены отдельным последующим коммитом документации;
  его SHA определяется текущим HEAD. Старые аудиты и smoke-отчёт сохраняют исторические
  результаты исходной приёмки.
- `.env` игнорируется Git; его содержимое не выводилось и не менялось.

## Завершённые operations/audit-изменения после исходной базы

- Добавлены host backup/rotation и isolated restore smoke, daily cron и production rollout
  runbook.
- Исправлена конкурентная блокировка broadcast claims: `FOR UPDATE SKIP LOCKED` блокирует
  только `broadcast_deliveries`.
- Feedback preview теперь показывает имя и `@username`; ответы администратора и закрытие тикета
  используют locale пользователя. Список тикетов отражает текущую feedback-блокировку.
- `/status` показывает состояние, число последовательных ошибок, последний успех и код ошибки
  каждого Terricon endpoint.
- Итоговый отчёт рассылки считает получателей, для которых потребовался transient retry.
- Зарегистрирован отдельный cron job выпуска накопленного ночного дайджеста во время окончания
  quiet hours.
- Долгие GPT/outbox/broadcast-сбои агрегируются и дедуплицируются в `admin_alerts`.
- Повторяющаяся недоступность PostgreSQL вызывает сгруппированный прямой runtime-alert, так как
  сохранить его в недоступной БД невозможно.
- Добавлены нейтральные общие bitmap-assets главного меню и всех девяти категорий без логотипов,
  текста или утверждения официального статуса.
- Materialization уведомлений ждёт завершения классификации, поэтому важное изменение выбирает
  подписчиков по финальному объединению старых и новых категорий без гонки с GPT worker.
- Feedback preview гарантированно укладывается в Telegram text limit после HTML-экранирования,
  не обрезая сохраняемый текст обращения.
- Первый runtime sync получает audit-trigger `startup`, последующие — `scheduled`.
- Получатели broadcast материализуются атомарным `INSERT ... SELECT` без загрузки всех user ID
  в память приложения.
- Из финального Docker image удалён неиспользуемый `pip` и его уязвимый vendored-код.
- Корректирующий план сохранён в
  `docs/plans/terricon-events-bot-v1-compliance-audit.md` и отмечен завершённым.
- Hardening-план сохранён в `docs/plans/terricon-events-bot-v1-hardening-audit.md` и отмечен
  завершённым.

## Validation (до перехода на Groq, историческая проверка)

- Полный PostgreSQL 17 test suite: `209 passed`.
- Ruff check и Ruff format check: passed (`112 files`).
- mypy strict: passed (`53 source files`).
- `uv lock --offline --check`, `bash -n` и `git diff --check`: passed.
- `pip-audit` production dependency set: известных уязвимостей нет.
- Trivy scan фактического runtime rootfs: 0 исправляемых HIGH/CRITICAL находок для Debian 13.7
  и Python-пакетов.
- Docker Compose config и application image build: passed; runtime работает без `pip`.
- Container migrations, preflight и healthcheck: passed; healthcheck вернул database/schema/
  heartbeat = true.
- Реальный backup/restore smoke: revision `0010_hardening_queue_state`, 25 public tables.
- Read-only live Terricon smoke: шесть endpoint успешны; на момент проверки каждый вернул
  0 записей и 0 невалидных записей с первой попытки.
- Временный backup и одноразовый PostgreSQL-контейнер после smoke удалены.

## Проверка перед публикацией 2026-10-21

- Полный suite повторён на отдельной PostgreSQL 17: **235 passed**, без пропусков.
- Ruff check/format (116 Python-файлов), mypy strict (53 source files), lock,
  `bash -n scripts/backup.sh scripts/restore-smoke.sh` и diff check — PASS.
- Ранее в этой сессии собран и проверен Docker-образ с Groq SDK без OpenAI SDK;
  после этого production-код не менялся. Security scans и backup/restore из
  исторической секции перед этим push повторно не запускались.
- В публикации отсутствуют `.env`, venv, backups и сборочные артефакты.
  Автоматический поиск известных форматов API-ключей/private keys по кандидатам
  коммита не нашёл совпадений; память вне проекта не публиковалась.

## Expected launch gates

- `locales/kz.yaml` намеренно содержит TODO-шаблоны и использует RU fallback в закрытой beta.
  Public mode остаётся заблокирован автоматической проверкой до пользовательской верификации KZ.
- Реальные Telegram-вызовы не выполнялись. Groq: только smoke 3/3; полная живая
  приёмка качества и квот этапа 10 ещё не выполнена.
- Production rollout не выполнялся; для него нужны отдельное разрешение и доступ к VPS.

## Runtime state

- Локальная Compose-БД запущена и healthy; application container не запускался в polling mode.
- На проверке 2026-10-21 контейнер `terricon-events-bot-db-1` healthy; приложение
  Terricon не запущено. Старого `terricon-hardening-test-db` больше нет.
- Проверочный образ `terricon-events-bot:groq-test` собран; production-контейнеры
  не пересоздавались. Одноразовая PostgreSQL публикационного gate удалена после тестов.
- Остальные проекты и их контейнеры не менялись.

## Next step

Продолжить этап 10 по `docs/plans/groq-migration.md`: качество реальных событий,
обратный KZ → RU перевод и общие квоты. Commit/push выполнены по запросу пользователя
2026-10-21; production rollout требует отдельного поручения.
