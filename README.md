# Terricon Events Bot

Неофициальный Telegram-бот для каталога мероприятий Terricon Valley и уведомлений о
новых событиях. Требования зафиксированы в
[`docs/specs/terricon-events-bot.md`](docs/specs/terricon-events-bot.md), порядок реализации —
в [`docs/plans/terricon-events-bot-v1.md`](docs/plans/terricon-events-bot-v1.md).

## Локальная разработка

Требуется `uv`; Python 3.13 и зависимости устанавливаются автоматически.

```bash
cp .env.example .env
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Секреты хранятся только в `.env`, который исключён из Git.
