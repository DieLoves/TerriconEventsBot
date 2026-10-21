# План перехода на Groq — этапы 9–10

Статус: этап 9 завершён и опубликован 2026-10-21 (`e175f15`);
этап 10 начат (только smoke 3/3).
Спецификация: `docs/specs/groq-migration.md`; основа — `docs/specs/terricon-events-bot.md`.
Исполнение в текущем рабочем дереве по запросу пользователя; прежний локальный diff
сохраняется. После приёмки пользователь 2026-10-21 отдельно разрешил commit/push
в origin/main; актуальный статус — `docs/handoffs/current.md`.
Deploy и изменения пользовательских секретов не выполняются.

## Этап 9 — замена провайдера (текущая задача)

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

## Этап 10 — аккаунт, квоты и живая приёмка (следующий этап)

- [x] Выполнить разрешённый пользователем начальный smoke: 3/3 HTTP 200, strict JSON,
  RU/KZ classification и RU → KZ translation, 1230 токенов.
- [ ] Пользователь настраивает `GROQ_API_KEY`; сверить реальные account limits.
- [ ] Подготовить 20–30 размеченных случаев, включая смешанные категории, `other`,
  длинное описание, оба направления перевода, null и текст с чужими инструкциями.
- [ ] Измерить качество, input/output/reasoning tokens и latency GPT-OSS 120B;
  сравнивать 20B только при необходимости по результатам.
- [ ] На измеренных размерах выбрать лимиты выдачи и throttling RPM/TPM/RPD/TPD
  с общим учётом классификации и перевода, сохранением состояния при рестарте.
- [ ] Проверить начальный импорт, исчерпание квоты, восстановление и fallback;
  записать доказательства. Production запуск остаётся отдельным действием.

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
