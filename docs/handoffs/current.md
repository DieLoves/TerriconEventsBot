# Handoff

## Текущий статус — независимое ревью, 2026-10-21

**Ревью завершено локально; изменения не закоммичены, push и production rollout не выполнялись.**
Подробности: [`docs/plans/terricon-events-bot-v1-review-2026-10-21.md`](../plans/terricon-events-bot-v1-review-2026-10-21.md).

- Исправлено 12 пунктов: спам изменений/отмен по прошедшим событиям, перехват команд
  админа ожиданием ответа/рассылки (+ `/cancel`), команды и длинный текст в мастере
  обратной связи, картинки меню 4,6 → 0,42 МБ + кэш `file_id`, кнопка «Подробнее» для
  актуального события без регистрации, однократный middleware, ограничение резерва Groq
  ёмкостью квоты, сохранение категорий при сбое переклассификации, сверка доступа при
  старте, приоритет актуальных событий в очереди, ссылки в дайджесте, 10 CVE зависимостей.
- Схема БД не менялась (по-прежнему `0011_groq_shared_quota`); новые env-переменные не нужны.
- Gate: 280 passed на одноразовой PostgreSQL 17, Ruff/format/mypy/lock/diff, `pip-audit` 0,
  Docker build + миграции + `terricon-preflight` из образа `terricon-events-bot:review-2026-10-21`.
- KZ-строки интерфейса заполнены (0 TODO, public readiness-gate проходит). Перевод выполнен
  агентом по запросу пользователя; вычитка носителем языка желательна до public mode.

## Этап 10, 2026-10-21

**Техническая часть и инструментальная приёмка выполнены. Языковая часть KZ открыта.**
Работа по `docs/specs/groq-migration.md` и `docs/plans/groq-migration.md`.
Полный отчёт: [Groq этап 10](../operations/groq-stage10-2026-10-21.md).

- PostgreSQL guard до HTTP: общие RPM/TPM/RPD/TPD классификации и перевода,
  rolling 60 с / 24 ч, атомарные резервы, actual usage reconciliation, unknown reserves,
  account headers с учётом in-flight requests и durable 429 cooldown.
- Новая миграция **0011_groq_shared_quota**, две quota-таблицы. Usage ledger и его
  прежние физические SQL-имена сохранены. Runtime preflight/restore обновлены до 0011.
- Defaults: `GROQ_RPM=24`, `GROQ_RPD=800`, `GROQ_TPM=6400`, `GROQ_TPD=160000`,
  общий `GROQ_QUOTA_SCOPE=groq:default`. Account RPD 1000 / TPM 8000 подтверждены headers;
  RPM 30 / TPD 200000 пользователь сверил на странице Limits. Запас 20% не гарантирует
  доступность при произвольном расходе других приложений организации.
- Classification prompt v3 определяет категории по предмету, а не аудитории.
  Output allowance 512/2048 подобран по измерениям, reasoning учтён внутри output.
  `json_validate_failed` безопасно преобразуется в `invalid_output` для retry.
- Translation v4 точно сохраняет speaker и исходные null description/audience/speaker.
  Свободный текст описания всё ещё требует языковой проверки: замечено изменение
  написания имени внутри description, не устранённое копированием speaker.
- Queue fallback 24 h имеет приоритет перед свежими quota denials. Первое ожидание
  квоты не перезапускает срок queued задачи. Initial import 30 jobs, recovery/fallback
  и настоящий restart отдельной PostgreSQL проверены.

## Живая приёмка

- Terricon API: 6/6 endpoint, 208 событий на локаль, invalid 0. Сохранены 24 реальных
  события с метками **до inference**, RU-only/KZ-only/bilingual по 8 случаев. Автор
  разметки — агент; независимая человеческая gold-оценка не заявляется.
- Baseline v2: 18/24; четыре семантических расхождения и два HTTP 400. Сохранён отдельно.
- V3: 23/24 с первой попытки, все 23 успешных ответа совпали с метками; один provider
  `json_validate_failed`. Отдельный recovery дал 24/24 после одного повтора. Исходный
  отказ сохранён, не считается успехом с первой попытки.
- RU↔KZ: 8/8 валидных ответов, включая самый длинный реальный кейс; 5/5 synthetic edges
  (instructions, long, null, URL/newline). После находки speaker сделан translation v4:
  два живых направления и unit RED→GREEN подтвердили точное сохранение поля.
- Median/p95 classification: 523/723 мс, translation: 766,5/1973 мс. За текущие 67 запросов
  известный usage 64 ответов: 30557 input + 7392 output = 37949 tokens; reasoning 4254 внутри
  output. У трёх HTTP 400 usage неизвестен, резерв сохранён. Реальный 429 не провоцировался.
- По измеренному среднему импорт 208 с обеими официальными локалями оценивается
  примерно в 140300 токенов; это прогноз, не AI-прогон всего архива. Если 208 событиям
  нужен перевод, суммарно около 279600 токенов — выше дневного Free. Free не объявлен
  достаточным для произвольных повторных импортов/неизвестной внешней нагрузки.

## Проверки финального diff

**266 passed** на отдельной PostgreSQL 17, без пропусков; Ruff/format 123 Python files,
strict mypy 56 source files, offline lock, wheel/sdist, shell/diff — PASS.
Docker `terricon-events-bot:stage10-test` пересобран после Python-исправлений,
его preflight PASS с фиктивными внешними ключами на временной БД. Реальный restore
подтвердил revision 0011 и 27 public tables; SQL после restore не менялся.

Независимое read-only review нашло Important 1: headers не учитывали другие in-flight
резервы. Два RED (tokens/requests) → GREEN; дополнительно исправлены starvation,
сброс 24 h deadline, json_validate_failed и speaker/null. Последний полный suite 266 PASS.
Review minors fingerprint/resume и oversized calibration учтены. Повторного
независимого review после исправлений не было.

## Текущее рабочее дерево и runtime

- Этап 9 и прежние audits опубликованы в origin/main: code e175f15, docs 5c6c290.
  По отдельному разрешению пользователя этап 10 опубликован в origin/main коммитом
  `511e11dd237067625865ed786de78bc6b16eab4e`
  (`feat: add durable Groq quotas and stage 10 acceptance`): 30 файлов.
  Push без force; SHA независимо подтверждён `git ls-remote`, дерево после code push
  чистое. Этот статус уточнён последующим коммитом документации; его SHA — текущий HEAD.
  Production rollout не выполнялся.
- Ключ, предоставленный пользователем для приёмки, вводился скрыто только в процессы,
  в `.env`, код, отчёты или память не записан. `.env` не менялся.
- Рабочая Compose-БД не менялась, миграция 0011 к ней не применялась; polling Terricon
  не запускался. Нельзя запускать новый код на старой схеме без upgrade head.
- Одноразовая PostgreSQL этапа 10 удалена после приёмки; проверочный Docker-образ
  оставлен. Другие проекты/контейнеры не менялись. Ниже — историческая публикация
  и исходные operations/audit результаты; актуальные изменения этапа 10 описаны выше.

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

- `locales/kz.yaml` до ревью 2026-10-21 содержал TODO-шаблоны с RU fallback; теперь заполнен.
  Public mode остаётся заблокирован автоматической проверкой до пользовательской верификации KZ.
- Реальные Telegram-вызовы не выполнялись. Техническая приёмка Groq этапа 10
  выполнена; независимая языковая проверка KZ и написания имён внутри description
  остаётся открытой. Метрики и ограничения описаны в отчёте этапа 10.
- Production rollout не выполнялся; для него нужны отдельное разрешение и доступ к VPS.

## Runtime state

- Локальная Compose-БД запущена и healthy; application container не запускался в polling mode.
- На проверке 2026-10-21 контейнер `terricon-events-bot-db-1` healthy; приложение
  Terricon не запущено. Старого `terricon-hardening-test-db` больше нет.
- Проверочный образ `terricon-events-bot:groq-test` собран; production-контейнеры
  не пересоздавались. Одноразовая PostgreSQL публикационного gate удалена после тестов.
- Остальные проекты и их контейнеры не менялись.

## Next step

Закрыть языковую часть этапа 10: независимая проверка KZ и решение о точном
сохранении имён внутри свободного description (пример real-9698: Серикова→Серікова).
Структурированное поле speaker уже сохраняется точно; метки 24 классификаций
заморожены и после inference не менялись. Не расширять эвристику имён без проверки.

Этап 10 опубликован по запросу пользователя; языковой gate остаётся открытым.
Production rollout требует отдельного поручения. Перед запуском: настроенный пользователем
GROQ_API_KEY, обычный backup/build/migrate до 0011/preflight; не сбрасывать quota state
и не менять scope при рестарте. Public KZ UI gate исходной спецификации сохраняется.

## Проверка перед публикацией этапа 10

- На отдельной PostgreSQL 17 повторён весь suite: **266 passed**, без пропусков.
- Ruff check/format (123 Python-файла), strict mypy (56 source files), offline lock,
  shell syntax и staged diff check — PASS. Код после этой проверки не менялся.
- В 30 файлах публикации отсутствуют `.env`, venv, backups, dist и память вне проекта.
  Поиск известных форматов API-ключей/private keys по staged blobs — без совпадений;
  ключ для прежних live-тестов не сохранён. Новые живые AI-вызовы не выполнялись.
- Одноразовая публикационная testPG и её volume удалены. Docker/restore/live evidence
  сохранены из инструментальной приёмки, перед push повторно не запускались.
- Remote main перед code push совпадал с базой 5c6c290; после push подтверждён code SHA
  511e11d. Последующий коммит статуса также проверяется на remote после публикации.
