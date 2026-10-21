# План полного hardening-аудита Terricon Events Bot v1

Статус: завершён 2026-09-20.

Цель — найти и устранить дефекты, уязвимости, гонки, деградации устойчивости и очевидные
потери производительности без изменения функционального scope спецификации.

## 1. Статический и security-аудит

- Проверить весь Python/Bash/Docker/Alembic-код, конфигурацию и dependency lock.
- Проверить авторизацию, callback/FSM ownership, обработку внешнего текста/URL/файлов,
  секреты и структурированные логи.
- Проверить актуальные advisories production-зависимостей и базовых контейнеров доступными
  автоматическими средствами.

## 2. Корректность и конкурентность

- Проверить транзакционные границы, внешнее I/O внутри транзакций, idempotency, leases,
  `FOR UPDATE SKIP LOCKED`, retry/fallback и restart recovery.
- Проверить пограничные состояния sync/classification/outbox/feedback/broadcast/runtime.
- Для каждого подтверждённого дефекта сначала добавить regression test, затем минимальное
  исправление и профильный gate.

## 3. Производительность

- Проверить N+1-запросы, неограниченные выборки, лишние full scan, индексы и scheduler overlap.
- Проверить размеры Telegram payload, memory amplification и удержание DB-соединений во время
  сетевых вызовов.
- Оптимизировать только доказанные горячие места, не добавляя новую функциональность.

## 4. Финальная проверка

- Ruff, format, mypy, dependency lock/security audit и shell checks.
- Полный test suite на PostgreSQL 17, включая новые regression tests.
- Alembic upgrade/downgrade, Docker build, migrations, preflight, healthcheck и backup/restore.
- Повторный аудит полного diff, обновление handoff и фиксация оставшихся внешних launch-gates.

## 5. Результат

- Устранена гонка между классификацией и materialization: изменение не попадает в outbox,
  пока категории находятся в `pending|retry_wait`, поэтому получатели важного изменения всегда
  вычисляются по итоговому объединению старых и новых категорий.
- Feedback preview ограничен реальным лимитом Telegram после HTML-экранирования имени,
  username и тела; полное обращение при этом сохраняется и отправляется администратору.
- Первый автоматический sync после запуска фиксируется как `startup`, последующие — как
  `scheduled`.
- Подтверждение broadcast больше не загружает все ID получателей в память: delivery rows и их
  количество создаются одним атомарным `INSERT ... SELECT`/CTE на стороне PostgreSQL.
- Из runtime image удалён неиспользуемый `pip` с уязвимым vendored-кодом. Финальный Trivy scan
  фактического rootfs не нашёл исправляемых HIGH/CRITICAL уязвимостей ни в Debian 13.7, ни в
  Python-пакетах; `pip-audit` production dependency set также не нашёл известных уязвимостей.
- Полный PostgreSQL 17 gate: `209 passed`; Alembic autogenerate не обнаружил расхождений модели
  и схемы.
- Ruff, Ruff format (`112 files`), strict mypy (`53 source files`), `uv lock --offline --check`,
  shell syntax, Compose config и `git diff --check`: пройдены.
- Docker image build, container preflight и healthcheck пройдены; healthcheck вернул
  `database/schema/heartbeat = true`.
- Реальный backup/restore smoke прошёл на revision `0010_hardening_queue_state`, 25 public-таблиц.
- Внешними launch-gates остаются только пользовательская верификация KZ-текстов, реальные
  Telegram/OpenAI smoke и production rollout; они требуют внешних учётных данных либо отдельного
  разрешения и не являются незавершённой реализацией.
