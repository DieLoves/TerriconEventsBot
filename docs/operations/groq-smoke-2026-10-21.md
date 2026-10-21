# Groq: приёмка этапа 9 и первая живая проверка

Дата: 2026-10-21. SDK `groq==0.37.1`; модель `openai/gpt-oss-120b`.
Реальные вызовы отдельно разрешены пользователем. Ключ вводился скрыто в процесс,
не сохранялся в `.env`, код, логи, отчёт или память. Тесты не запускали Telegram-бота.

## Offline gate

- Полный suite с отдельной PostgreSQL 17: **235 passed**, без пропусков.
- Ruff check/format, mypy strict (53 source files), `uv lock --offline --check`,
  `git diff --check`, wheel/sdist build — PASS.
- Docker-образ `terricon-events-bot:groq-test` собран; проверка SDK/импорта адаптера
  выполняется с отключённой сетью. SDK OpenAI удалён из зависимостей и venv.
- SQL schema drift отсутствует: 10 существующих миграций upgrade/downgrade проходят,
  SQL-имена ledger/enum сохранены; новых миграций не требуется.
- Новые регрессии: strict JSON и nullable-поля, невалидные категории/JSON/envelope,
  refusal/incomplete output, usage, transport/HTTP/429; durable Retry-After и fallback;
  повтор перевода после рестарта, официальная локаль, deadline и отсутствие дублей;
  SDK debug-запросы не попадают в application logs.

Начальный unit-прогон в sandbox завершился timeout в
`test_start_and_locale_callback_flow_through_router_middleware` и зависанием thread
notification HTTP MockTransport. Повтор всего suite вне sandbox прошёл; это ограничение
окружения, а не скрытый непройденный тест. Зависший тестовый процесс остановлен.

Независимое read-only ревью выявило три Important: malformed envelope, SDK DEBUG dumps,
потерю перевода после 429. Все воспроизведены RED и исправлены; финальный suite зелёный.
Повторное независимое ревью после исправлений не выполнялось.

## Реальные API-запросы

Три небольших синтетических примера, а не выборка настоящих мероприятий.
Все ответы HTTP 200 и прошли Pydantic-валидацию текущих строгих схем.

| Сценарий | Результат | Input | Output | Время |
|---|---|---:|---:|---:|
| RU: встреча Python/asyncio/REST | `development_it` | 319 | 63 | 334 мс |
| KZ: стартап и венчурное финансирование | `startups_products`, `business_finance` | 330 | 69 | 607 мс |
| RU → KZ: встреча разработчиков | валидный перевод | 300 | 149 | 449 мс |

Итого usage: **949 input + 281 output = 1230 токенов**. В переводе сохранены
`https://example.com/python`, перенос строки, `Ada Lovelace` и `audience=null`.

Заголовки аккаунта подтвердили `x-ratelimit-limit-requests=1000` (RPD) и
`x-ratelimit-limit-tokens=8000` (TPM). RPM/TPD по этим заголовкам не подтверждались.
Проверка не исчерпывала квоты и не проверяла настоящий HTTP 429.

## Открытый этап 10

- Оценить качество на 20–30 размеченных настоящих событиях и оба направления перевода.
- Сверить оставшиеся квоты аккаунта, длинные тексты и начальный импорт.
- Настроить общий throttling RPM/TPM/RPD/TPD с восстановлением после рестарта.
- Для реального запуска отдельно настроить `GROQ_API_KEY` в локальном окружении.
  Переданный для smoke ключ в проект не записывался.

На момент этой приёмки этап 9 выполнен локально; commit/push и production rollout
ещё не выполнялись. Последующий запрос пользователя на публикацию и её актуальный
статус отражаются в `docs/handoffs/current.md`. Этот отчёт фиксирует исходную приёмку.
