# План перехода на Groq — этапы 9–10

Статус: этап 9 завершён и опубликован 2026-10-21 (`e175f15`);
этап 10: техническая часть и инструментальная приёмка выполнены;
независимая языковая проверка KZ остаётся открытой.
Спецификация: `docs/specs/groq-migration.md`; основа — `docs/specs/terricon-events-bot.md`.
Исполнение в текущем рабочем дереве по запросу пользователя. Пользователь отдельно
разрешил commit/push этапа 10 в существующий origin/main; актуальный статус
публикации — `docs/handoffs/current.md`.
Deploy и изменения пользовательских секретов не выполняются.

## Этап 9 — замена провайдера (завершён)

### Задача 9.1 — контракт и SDK

- [x] Сначала добавить HTTP MockTransport-тесты: строгая схема классификации и перевода,
  nullable-поля, невалидные категории/JSON, отказ, length finish, отсутствующий usage,
  timeout/connection/HTTP и 429. Увидеть красный тест до реализации.
- [x] Ввести `GroqEventAdapter.classify_event(ClassificationInput)` и
  `translate_missing_locale(TranslationInput)` через `AsyncGroq.chat.completions.create`.
  JSON валидировать существующими DTO; prompt versions увеличить до v2-groq.
- [x] Заменить `openai` на `groq` в pyproject/lock и composition; отключить SDK retry.
  Ввести `AIUsageData`, `AIAdapterError`, `AIPricing`, `AIUsage`, `AIOperation`;
  физические имена SQL не менять.
- [x] Перевести Settings, fixtures, runtime/admin и безопасное логирование на Groq.
  Defaults: GPT-OSS 120B, цены 0/0 на Free, месячный бюджет 5, timeout 30 с.

### Задача 9.2 — очередь и регрессия

Дополнение по ревью: durable retry перевода на completed classification.next_attempt_at,
без переклассификации; malformed envelope и SDK DEBUG закрыты регрессионными тестами.

- [x] Проверить RED→GREEN: `Retry-After` переносится в `next_attempt_at`, batch после
  429 прекращает новые попытки; следующая проверка не позже 24-часового fallback.
  Ошибка с usage сохраняет токены, официальный перевод по-прежнему побеждает машинный.
- [x] Прогнать unit и весь isolated PostgreSQL suite, Ruff/format/mypy/uv lock,
  Docker build и проверку отсутствия SDK `openai` в установленном окружении/образе.
- [x] Обновить основной план, README, шаблон env, deployment и current handoff.
  Зафиксировать реальные результаты; этап 9 отметить завершённым только после gate.

## Этап 10 — аккаунт, квоты и живая приёмка

- [x] Выполнить разрешённый пользователем начальный smoke: 3/3 HTTP 200, strict JSON,
  RU/KZ classification и RU → KZ translation, 1230 токенов.
- [x] Ключ предоставлен для процессов приёмки; account limits сверены:30 RPM,
  1000 RPD,8000 TPM,200000 TPD. `.env` не менялся; production key отдельно.
- [x] Подготовить 20–30 размеченных случаев, включая смешанные категории, `other`,
  длинное описание, оба направления перевода, null и текст с чужими инструкциями.
- [x] Измерить качество, input/output/reasoning tokens и latency GPT-OSS 120B;
  сравнивать 20B только при необходимости по результатам.
- [x] На измеренных размерах выбрать лимиты выдачи и throttling RPM/TPM/RPD/TPD
  с общим учётом классификации и перевода, сохранением состояния при рестарте.
- [x] Проверить начальный импорт, исчерпание квоты, восстановление и fallback;
  записать доказательства. Production запуск остаётся отдельным действием.
- [ ] Независимая языковая проверка KZ и точного написания имён внутри description
  (real-9698: Серикова→Серікова); speaker/null защищены кодом. Public KZ UI gate
  исходной спецификации не снимается.

## Решения и журнал

- Используем Chat Completions — документированный интерфейс официального SDK Groq.
- SQL-имена сохраняются: миграция таблиц не нужна для удаления библиотеки OpenAI.
- Старые завершённые этапы не переписываем как ещё не сделанную работу.
- Живую приёмку и точную настройку Free не объявляем пройденными по mock-тестам.

## Фактическая приёмка этапа 9

235 tests с PostgreSQL 17, Ruff/format/mypy/lock/diff и package/Docker builds — PASS.
OpenAI SDK отсутствует в venv и Docker. Подробности и границы живой проверки:
`docs/operations/groq-smoke-2026-10-21.md`. Три Important review findings
воспроизведены RED→GREEN; повторный independent review после исправлений не выполнялся.

По следующему запросу пользователя всё опубликовано в origin/main коммитом
`e175f15`; перед публикацией повторён полный suite: 235 passed. Текущий handoff
содержит сведения о remote-проверке и отдельном коммите статусов документации.

## Исполнение этапа 10 — 2026-10-21

Работа в существующем checkout согласно запросу пользователя и этому плану.
Без commit/push/deploy и изменения `.env`; временная тестовая PostgreSQL изолирована.

1. Собрать snapshot публичных Terricon-событий; до AI-вызовов разметить 24 реальных
   события с допустимыми категориями, отдельно добавить синтетические edge cases.
   Сохранить происхождение, измерить strict outputs, токены и latency, RU↔KZ.
2. RED→GREEN: общий PostgreSQL quota guard перед HTTP: скользящие окна 60 с/24 ч,
   RPM/TPM/RPD/TPD, атомарное резервирование, восстановление после рестарта,
   общий scope модели для обеих операций, durable cooldown и внешние остатки.
   Миграция 0011 добавляет только новые quota-таблицы; прежний usage ledger сохранён.
3. Встроить guard в Groq adapter/composition; резервировать консервативную оценку
   UTF-8 payload+schema+instructions+output allowance, после ответа учитывать actual
   usage. При неизвестном результате оставить резерв до истечения окна. HTTP I/O
   вне транзакции; oversized запрос отклонять безопасным кодом. Проверить batch,
   fallback24h, restart, concurrency и большой initial import на isolated PostgreSQL.
4. Полный PostgreSQL suite/static/lock/build, независимое итоговое review;
   обновить README/runbook/handoff с фактическим статусом и ограничениями.

Решения: значения лимитов до живых измерений предварительные и с запасом;
гарантия охватывает процессы с общей БД/scope. Чужие приложения без этой БД
контролируются через response headers/429, их будущий расход предсказать нельзя.
Дневное окно24h намеренно консервативнее календарного reset. Ключ в процесс,
не в файлы; точные account RPM/TPD требуют страницы Limits пользователя.

### Журнал выполнения

- Baseline: 235/235 PostgreSQL17 PASS; Terricon live6/6, 208 событий/локаль,
  invalid0. Разметка24 реальных событий заморожена до inference, автор — агент;
  независимая человеческая gold-разметка не заявляется.
- Quota RED: отсутствующий модуль, затем GREEN22 schema/quota tests;
  adapter RED4 → GREEN28; settings RED6 → GREEN.
- Initial import30: throttle/restart/recovery/fallback проверены. Найден starvation
  expired job свежим quota denial; RED → GREEN, срок очереди не перезапускается.
- Independent read-only review: Critical0, Important1 (response headers без учёта
  in-flight reservations); два отдельных RED tokens/requests → GREEN. Дополнительно
  RED first quota denial при возрасте23ч59м → GREEN. Целевой suite37 PASS.
- Решение: 24h fallback считается от исходного queued created_at, если first_attempt_at
  ещё не установлен; это совпадает с существующим snapshot/deadline и не продлевает
  ожидание при исчерпанных квотах. Стоимость ошибки решения — более ранний other
  для событий, долго ждавших первого обращения к AI, вместо скрытого ожидания48h.
- Review minors calibration/resume учитываются в финальном прогоне: отдельные версии
  отчётов для prompt changes; runtime output allowances подбираются по измерениям.


### Итог текущей сессии

- 24 реальных события: baseline18/24; promptv3 —23/24 с первого раза и24/24 после
  отдельного recovery provider JSON failure. Метки не менялись; разметка агентская.
- 8 переводов RU↔KZ +5synthetic edge cases; speaker spelling defect исправлен
  translationv4, RED→GREEN + два живых направления. Прозаические имена в description
  оставлены явным языковым gate, без небезопасной эвристики распознавания имён.
- Allowances512/2048, defaults24/800/6400/160000, общий scope и persistent buckets.
  Реальный restart PostgreSQL сохранил quota denial; restore0011/27tablesPASS.
- Финальный gate **266tests**, Ruff/format123, mypy56, lock/build/shell/diff,
  Dockerbuild и повторный image preflight — PASS. Отчёт и4JSONevidence:
  `docs/operations/groq-stage10-2026-10-21.md`. Реальный429не провоцировался.
- На момент инструментальной приёмки commit/push/deploy не выполнялись. По следующему
  запросу разрешена публикация этапа 10; `.env` не менялся, testPG удалена после gate.
  Handoff обновлён. Технические задачи завершены, открытая языковая задача выше
  остаётся текущей; весь этап10безусловно завершённым не объявляется.
