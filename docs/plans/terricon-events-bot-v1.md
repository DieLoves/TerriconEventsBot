# Технический план реализации Terricon Events Bot v1

## 1. Исходное состояние и архитектура

- Проект greenfield: кода и существующей архитектуры нет, поэтому конфликтов со спецификацией не обнаружено.
- Инициализировать Python-проект с `src`-layout, Git, `pyproject.toml`, `uv.lock`, `.gitignore`, `.env.example`, Ruff, mypy и pytest.
- Разделить приложение на слои: Telegram handlers/rendering, application services, domain types, SQLAlchemy repositories, внешние адаптеры Terricon/OpenAI и фоновые jobs.
- Собирать приложение через единый composition root: конфигурация → PostgreSQL → клиенты API → сервисы → scheduler → aiogram dispatcher.
- Использовать один экземпляр long-polling приложения. Долговременные задания хранить в PostgreSQL; APScheduler отвечает только за запуск обработчиков по расписанию.
- Текущее окружение имеет Python 3.12 и не имеет Docker/PostgreSQL. Python 3.13 устанавливать через `uv`; полные интеграционные проверки потребуют установки Docker или запуска на подготовленном VPS.

## 2. Этапы реализации

### Этап 1 — фундамент проекта

- Добавить типизированную конфигурацию Pydantic Settings со строгой проверкой обязательных секретов, диапазонов интервалов, ID администраторов и режимов доступа.
- Настроить асинхронный engine/session factory SQLAlchemy, Unit of Work и Alembic.
- Ввести доменные enum: локаль, source theme, категория, формат, состояние события, тип уведомления, статус тикета и рассылки.
- Добавить YAML-локализации `ru` и `kz`: проверять совпадение ключей, использовать RU fallback и запрещать публичный KZ-режим при незакрытых TODO.
- Реализовать resolver декоративных изображений: локализованный файл → общий → отсутствие изображения.
- Настроить безопасное логирование без секретов, Telegram ID и содержимого обращений.

### Этап 2 — схема PostgreSQL

Создать миграции группами, чтобы каждый следующий функциональный этап опирался на завершённую схему:

- Пользователи: настройки языка и изображений, onboarding, активность, режим доступа, feedback-блокировка, ID текущего menu message.
- События: общие поля, дата UTC, тема источника, формат, языки, доступность, missing streak, semantic/important hashes, revision и timestamps.
- Локализации и категории: уникальность `(event_id, locale)` и `(event_id, category)`, источник перевода `official|machine`, metadata классификации.
- Синхронизация: runs, состояние каждого endpoint, baseline flag, ошибки и время последнего успеха.
- Подписки: `subscribe_all` и категории с транзакционным обеспечением взаимоисключения.
- Надёжная доставка: domain changes, notification deliveries и outbox с уникальностью по пользователю, событию, revision и типу изменения.
- Feedback и admin: тикеты, сообщения, Telegram `file_id`, DB-backed FSM state, блокировки.
- Рассылки: draft, локализованный контент, аудитория, deliveries и прогресс.
- Эксплуатация: OpenAI usage ledger, дедупликация admin-alerts и heartbeat приложения.
- Добавить индексы по дате события, доступности, категориям, pending outbox, открытым тикетам и незавершённым рассылкам.

### Этап 3 — импорт Terricon API

Статус: завершён 2026-09-18.

- Реализовать `TerriconClient` на `httpx.AsyncClient` и Pydantic DTO фактического API.
- Запрашивать шесть комбинаций через semaphore; каждый endpoint получает собственный timeout, первоначальный запрос и три retry с exponential backoff, jitter и `Retry-After`.
- Нормализовать URL, дату, формат и языки мероприятия; неизвестные значения сохранять безопасно и логировать, а не обрушать цикл.
- Объединять локали по source ID. Общие поля выбирать из RU, затем KZ; расхождения
  фиксировать как admin-alert. Верхнеуровневый `url` фактического API локализован сегментом
  `ru|kz`, поэтому хранить его вместе с локализацией и не считать расхождением общего поля.
- Выполнять upsert одного цикла транзакционно после валидации полученных частей. Ошибка одной пары locale/theme не должна повреждать ранее сохранённые данные.
- Завершать первоначальный baseline только после первого цикла, где успешны все шесть endpoint. До этого новые записи не создают уведомления.
- Увеличивать missing streak только когда обе локали конкретной темы успешно загружены и ID отсутствует в обеих.
- Вычислять отдельно:
  - semantic hash для решения о переклассификации;
  - important hash и структурированный diff для пользовательских уведомлений.
- Сохранять domain change только после baseline; повторный импорт одинакового ответа должен быть полностью идемпотентным.
- Добавить ручную smoke-команду API, которая ничего не записывает в БД.

### Этап 4 — OpenAI-адаптер

Статус: завершён 2026-09-18.

- Использовать `AsyncOpenAI` и Responses API с `store=False`, `reasoning.effort=low` и строгим `json_schema`; дополнительно валидировать ответ Pydantic-моделью. GPT‑5.6 Luna официально поддерживает Responses и Structured Outputs. [OpenAI Docs](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- Реализовать отдельные операции `classify_event` и `translate_missing_locale`; статические интерфейсные тексты и admin-рассылки через этот адаптер не пропускать.
- В классификаторе проверять количество меток и исключительность `other`; сохранять prompt version, модель, usage и время выполнения.
- Обрабатывать классификацию как устойчивую DB-очередь: `pending → completed|retry_wait|fallback`. После 24 часов фиксировать `other`.
- Создавать уведомление о новом событии только после завершённой классификации либо финального fallback.
- Для машинного перевода хранить provenance и заменять его официальной локалью при следующем успешном импорте.
- Реализовать месячный ledger токенов и стоимости. Перед вызовом проверять мягкий лимит; цены сделать конфигурируемыми с defaults из спецификации.
- При исчерпании лимита применять ту же политику pending/fallback и отправлять один дедуплицированный admin-alert.

### Этап 5 — каталог и Telegram UI

- Подключить middleware для загрузки пользователя, allowlist/public access, onboarding и admin authorization.
- Создать компактные `CallbackData`-контракты только из enum-кодов, ID и номеров страниц; пользовательский текст в callback не помещать.
- Реализовать `CatalogQuery` как SQL-запрос с совместными фильтрами, timezone-aware границами и стабильной сортировкой.
- Сделать renderer одного экрана:
  - редактировать существующий текст/media message;
  - при несовместимой смене типа сообщения безопасно заменить его;
  - восстанавливаться, если пользователь удалил menu message;
  - игнорировать Telegram `message is not modified`.
- Экранировать весь внешний текст, ограничивать caption/text и разбивать полное описание на допустимые сообщения.
- Реализовать onboarding, главное меню, каталог, страницы результатов, карточку события, описание, настройки, подписки, privacy/delete и «О проекте».
- Для постеров сначала отправлять URL; при Telegram/API ошибке повторять карточку без изображения.
- Сохранять состояние фильтров и многошаговых сценариев в PostgreSQL, а не в памяти процесса.

### Этап 6 — подписки и outbox

- Изменять `subscribe_all` и категорийные подписки одной транзакцией.
- Материализовать получателей domain changes в delivery-записи с уникальными ключами, используя текущие подписки и объединение старых/новых категорий.
- Группировать pending deliveries пользователя в один логический дайджест, дедуплицируя событие с несколькими категориями.
- Ввести clock abstraction и `Asia/Almaty`; в тихие часы только накапливать outbox.
- Отправлять большие дайджесты несколькими Telegram-сообщениями, сохраняя единый batch ID.
- Повторять только transient-ошибки. При Telegram `403` деактивировать пользователя и закрывать его pending deliveries.
- Отмечать delivery завершённым только после подтверждённой отправки, чтобы перезапуск не создавал дублей.

### Этап 7 — feedback и admin-функции

- Реализовать feedback wizard через DB-backed FSM: тип → текст → фото → preview → confirm.
- Проверять rolling limit трёх новых тикетов за 24 часа и feedback blacklist.
- В admin-чате создавать служебное сообщение тикета с безопасно сформированными данными пользователя и кнопками ответа, закрытия и блокировки.
- Ответы отправлять новым сообщением от имени бота; не использовать Telegram forwarding.
- Поддержать продолжение диалога по открытому тикету и ежедневное удаление закрытых данных старше 90 дней.
- Удаление пользователя выполнять одной транзакцией: настройки, подписки, FSM, тикеты, сообщения и pending deliveries; открытые тикеты закрывать.
- Реализовать `/admin`, status, stats, manual sync, reclassify, tickets, block/unblock и broadcast wizard.
- Для рассылки сохранять draft до подтверждения, требовать локализованный текст для каждой выбранной аудитории, создавать delivery rows и отправлять их throttled batch-процессом.
- Обеспечить возобновление рассылки после рестарта и итоговый отчёт по доставленным, временно ошибочным и заблокировавшим бота пользователям.

### Этап 8 — scheduler, эксплуатация и деплой

- Зарегистрировать APScheduler jobs:
  - немедленная синхронизация при старте и далее каждые 6 часов;
  - обработка classification queue;
  - отправка outbox и рассылок;
  - выпуск ночного дайджеста после 09:00;
  - очистка feedback;
  - heartbeat.
- Для sync, classification и broadcast использовать PostgreSQL advisory locks и `max_instances=1`; данные очередей остаются источником истины.
- Добавить graceful shutdown: остановить приём updates, scheduler и HTTP-клиенты, дождаться текущей транзакции, закрыть pool.
- Подготовить multi-stage Dockerfile и Compose с app/PostgreSQL, healthchecks, persistent volume и отдельным migration command.
- Healthcheck приложения должен проверять БД и свежесть heartbeat без отдельного публичного HTTP-сервера.
- Добавить host-скрипты `pg_dump`, ротацию семи архивов и проверку восстановления в временную БД; документировать daily cron.
- Описать ручной rollout: backup → получение версии → сборка → миграции → restart → healthcheck → smoke-команды.
- После закрытой beta переключать только `ACCESS_MODE`; KZ открывать публично после автоматической проверки отсутствия TODO.

## 3. Основные программные интерфейсы

- `TerriconClient.fetch(locale, theme) -> list[SourceEvent]`
- `SyncService.run(trigger) -> SyncResult`
- `Classifier.classify(ClassificationInput) -> ClassificationResult`
- `EventTranslator.translate(EventLocalization, target_locale) -> EventLocalization`
- `CatalogService.list(CatalogFilter, page) -> Page[EventSummary]`
- `SubscriptionService.replace(user_id, mode, categories)`
- `OutboxService.materialize(changes)` и `DeliveryWorker.run_batch()`
- `FeedbackService` и `BroadcastService` с явными state transition методами.
- `Clock` и `TelegramGateway` оформляются как протоколы, чтобы время и отправку можно было полностью подменять в тестах.
- Доменные сервисы не зависят от aiogram; handlers только преобразуют Telegram update в вызов сервиса и renderer.

## 4. Тестирование и контрольные точки

- Unit: DTO, нормализация, хеши/diff, retry policy, бюджет GPT, category validation, timezone, фильтры, разбиение сообщений и переходы state machine.
- Repository integration: каждая Alembic-миграция, ограничения подписок, upsert, missing streak, конкурентное получение outbox через `FOR UPDATE SKIP LOCKED`.
- Service integration: полный импорт из зафиксированных API fixtures, partial failure, baseline, повторный одинаковый цикл, важное изменение, отмена и восстановление.
- OpenAI contract: mocked Responses API, корректный schema output, invalid/refusal/timeout, usage ledger, 24-часовой fallback и замена машинной локали.
- Telegram handlers: RU/KZ fallback, allowlist, callbacks, удалённый menu message, изображения on/off, feedback и admin authorization.
- Delivery: тихие часы, мультиметки без дублей, рестарт между созданием и отправкой, `403`, transient retry, возобновление broadcast.
- Privacy: удаление пользователя и отсутствие его Telegram ID/текста в логах.
- Operations: чистый `docker compose up`, миграции, healthcheck, backup/restore smoke.
- Обязательный локальный gate: `ruff check`, `ruff format --check`, `mypy`, unit tests.
- Полный gate перед beta: PostgreSQL integration tests, mocked end-to-end bot flow и opt-in live Terricon smoke; реальные OpenAI/Telegram вызовы в автоматических тестах запрещены.

## 5. Допущения и ограничения

- Production запускается в одном экземпляре; несколько long-polling replicas не поддерживаются.
- PostgreSQL — единственный production storage; SQLite fallback не создаётся.
- API Terricon остаётся публичным и не требует дополнительной авторизации.
- Наличие платного OpenAI API-ключа и Telegram Bot Token проверяется только при запуске приложения; секреты не нужны для unit tests.
- Технический план не требует миграции существующих данных, поскольку текущей базы и приложения нет.
- Перед полной проверкой Docker-инфраструктуры окружение необходимо дополнить Docker Compose; отсутствие Docker сейчас является ограничением среды, а не конфликтом архитектуры.
