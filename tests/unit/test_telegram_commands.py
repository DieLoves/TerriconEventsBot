from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SetMyCommands

from terricon_events_bot.localization import LocalizationCatalog
from terricon_events_bot.telegram.commands import publish_user_commands

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_start_command_is_published_per_locale_and_failures_do_not_block() -> None:
    bot = Mock(spec=Bot)
    bot.set_my_commands = AsyncMock(
        side_effect=[True, TelegramNetworkError(SetMyCommands(commands=[]), "offline")]
    )

    await publish_user_commands(cast(Any, bot), LocalizationCatalog.load(PROJECT_ROOT / "locales"))

    calls = bot.set_my_commands.await_args_list
    assert [call.kwargs["language_code"] for call in calls] == [None, "kk"]
    assert [call.args[0][0].description for call in calls] == ["Главное меню", "Басты мәзір"]
    assert all(call.args[0][0].command == "start" for call in calls)
