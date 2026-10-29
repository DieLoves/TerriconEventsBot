# Эксплуатация и ручной rollout

Команды выполняются на VPS из корня репозитория. В `.env` должны находиться production-значения;
файл не копируется в Git и не выводится в логи. PostgreSQL volume нельзя удалять командой
`docker compose down -v`.

## Ежедневный backup

Host-скрипт запускает `pg_dump` внутри PostgreSQL-контейнера, атомарно сохраняет custom-format
архив вне database volume и оставляет семь последних архивов:

```bash
BACKUP_DIR=/var/backups/terricon-events-bot ./scripts/backup.sh
```

Каталог должен быть доступен только системному пользователю приложения. Скрипт выставляет режим
`0700` для каталога и `0600` для нового архива. Архивы называются
`terricon-YYYYMMDDTHHMMSSNNNNNNNNNZ-PID.dump`.

Пример ежедневного cron в 02:15 UTC:

```cron
15 2 * * * cd /opt/terricon-events-bot && BACKUP_DIR=/var/backups/terricon-events-bot ./scripts/backup.sh >>/var/log/terricon-backup.log 2>&1
```

Пользователь cron должен иметь доступ к Docker daemon. После изменения cron нужно вручную
запустить команду один раз и убедиться, что создан непустой архив.

## Проверка восстановления

Проверка поднимает изолированный временный PostgreSQL 17 на `tmpfs`, восстанавливает архив,
проверяет точный Alembic revision `0013_default_subscriptions` и наличие не менее 26 публичных
таблиц, затем всегда удаляет контейнер. Production database и её volume не изменяются.

Последний архив из `BACKUP_DIR`:

```bash
BACKUP_DIR=/var/backups/terricon-events-bot ./scripts/restore-smoke.sh
```

Конкретный архив:

```bash
./scripts/restore-smoke.sh /var/backups/terricon-events-bot/terricon-20260919T021500Z-1234.dump
```

Свежий backup считается проверенным только после строки `Restore smoke OK`. Проверку восстановления
нужно выполнять после настройки backup, затем регулярно и перед production rollout.

## Ручной rollout

1. Создать backup текущей базы и проверить его восстановление:

   ```bash
   export BACKUP_DIR=/var/backups/terricon-events-bot
   backup_path="$(./scripts/backup.sh)"
   ./scripts/restore-smoke.sh "$backup_path"
   ```

2. Получить утверждённую версию без переписывания истории:

   ```bash
   git fetch origin
   git switch main
   git pull --ff-only origin main
   git rev-parse HEAD
   ```

3. Собрать новый образ, не останавливая текущий контейнер:

   ```bash
   docker compose build
   ```

4. Применить миграции отдельной одноразовой командой:

   ```bash
   docker compose run --rm migrate
   ```

5. Пересоздать приложение и проверить состояние:

   ```bash
   docker compose up -d app
   docker compose ps
   docker compose exec -T app terricon-healthcheck
   ```

   Healthcheck должен вывести `database`, `schema` и `heartbeat` со значением `true`.

6. Выполнить smoke-проверки без платных автоматических запросов:

   ```bash
   docker compose run --rm --no-deps app terricon-api-smoke
   docker compose logs --since=10m app
   ```

   В Telegram вручную проверить `/start`, каталог, `/admin` и `/status`. Реальные Groq-вызовы и
   массовые рассылки не используются как автоматический smoke.

## Переход существующей установки на Groq

Сверить `.env` с новым `.env.example`: обязательный `GROQ_API_KEY`, модель
`GROQ_MODEL=openai/gpt-oss-120b`, остальные AI-настройки также имеют префикс `GROQ_`.
Значения старых `OPENAI_*` больше не используются. Ключи между провайдерами несовместимы.
На Free цены токенов нулевые; при платном тарифе задать реальные цены для учёта бюджета.

Для SDK (этап9) миграция не требовалась: ledger/enum сохраняют прежние SQL-имена.
Этап10 добавляет `0011_groq_shared_quota` с двумя таблицами общих квот. Перед новым
preflight выполнить upgrade head; очистка таблиц/volume не нужна. Сверить новые
`GROQ_RPM/RPD/TPM/TPD` и общий `GROQ_QUOTA_SCOPE` с `.env.example`; значения должны
оставлять запас относительно реальных organization limits. Usage неизвестного
вызова сохраняет полный token reserve до истечения окна. Сброс quota-таблиц или
смена scope при рестарте обходят ограничения и для штатной эксплуатации не нужны.
Использовать обычный порядок
backup/build/migrate/preflight; сам production restart выполняется отдельно.
Проверки: [этап9 smoke](groq-smoke-2026-10-21.md),
[этап10 и общие квоты](groq-stage10-2026-10-21.md).

## Гибридный источник (2026-10-26)

Миграция `0012_hybrid_source` добавляет источник цикла, вид события, подписку на
программы и ключи endpoint `stage`/`legacy`. Перед её применением backup проверяется
ещё по прежней ревизии:

```bash
EXPECTED_SCHEMA_REVISION=0011_groq_shared_quota ./scripts/restore-smoke.sh "$backup_path"
```

Новые настройки необязательны: `STAGE_URL` по умолчанию — соседний с `BASE_URL`
`…/eventsByStage`, `SOURCE_FALLBACK_ALERT_HOURS=24`. `terricon-api-smoke` проверяет
8 endpoint (6 `getAllEvents` + 2 `eventsByStage`). Первый цикл после выкатки добавляет
записи `eventsByStage` (~81) без уведомлений о прошедших; в `/status` строка
«Источник: eventsByStage (основной)».

## Beta и public mode

Закрытая beta работает с `ACCESS_MODE=allowlist`. Перед public mode KZ-файл должен не содержать
`TODO`; автоматический readiness-gate проверяется без изменения сохранённого `.env`:

```bash
docker compose run --rm --no-deps -e ACCESS_MODE=public app terricon-preflight
```

Только после успешного preflight изменить в `.env` одну настройку на `ACCESS_MODE=public` и
пересоздать app через `docker compose up -d app`. Изменение кода или удаление database volume для
перехода в public mode не требуется.

## Подписка по умолчанию (2026-10-29)

Миграция `0013_default_subscriptions` только с данными: каждому пользователю без
подписки ставит «Все новые мероприятия». Backup перед ней проверяется по прежней ревизии:

```bash
EXPECTED_SCHEMA_REVISION=0012_hybrid_source ./scripts/restore-smoke.sh "$backup_path"
```

Downgrade ничего не делает — подписки по умолчанию неотличимы от выбранных вручную.
