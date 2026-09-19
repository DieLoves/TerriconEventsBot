from __future__ import annotations

# ruff: noqa: RUF001 - Russian admin UI intentionally mixes technical terms.
from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from terricon_events_bot.application.admin import AdminService
from terricon_events_bot.application.broadcasts import (
    BroadcastDraft,
    BroadcastService,
    BroadcastStateError,
)
from terricon_events_bot.application.delivery import TelegramSendError
from terricon_events_bot.application.feedback import FeedbackService
from terricon_events_bot.application.operations import (
    JobAlreadyRunningError,
    SyncRunner,
)
from terricon_events_bot.application.users import UserService
from terricon_events_bot.domain.enums import (
    BroadcastAudience,
    BroadcastStatus,
    FeedbackAuthor,
    Locale,
    SyncTrigger,
    TicketStatus,
)
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.telegram.callbacks import (
    AdminAction,
    AdminActionCallback,
    AdminTicketAction,
    AdminTicketCallback,
    BroadcastAction,
    BroadcastActionCallback,
    BroadcastAudienceCallback,
)
from terricon_events_bot.telegram.feedback import AiogramFeedbackGateway
from terricon_events_bot.telegram.middleware import UserContextMiddleware


class AdminTelegramController:
    def __init__(
        self,
        admin: AdminService,
        feedback: FeedbackService,
        broadcasts: BroadcastService,
        sync: SyncRunner,
        feedback_gateway: AiogramFeedbackGateway,
        users: UserService,
    ) -> None:
        self._admin = admin
        self._feedback = feedback
        self._broadcasts = broadcasts
        self._sync = sync
        self._feedback_gateway = feedback_gateway
        self._users = users

    async def menu(self, message: Message, is_admin: bool) -> None:
        if not await self._require_message_admin(message, is_admin):
            return
        await message.answer("Администрирование", reply_markup=self._admin_keyboard())

    async def status(self, message: Message, is_admin: bool) -> None:
        if not await self._require_message_admin(message, is_admin):
            return
        await message.answer(await self._status_text())

    async def stats(self, message: Message, is_admin: bool) -> None:
        if not await self._require_message_admin(message, is_admin):
            return
        await message.answer(await self._stats_text())

    async def sync(self, message: Message, is_admin: bool) -> None:
        if not await self._require_message_admin(message, is_admin):
            return
        await message.answer("Синхронизация запущена.")
        try:
            result = await self._sync.run(SyncTrigger.MANUAL)
        except JobAlreadyRunningError:
            await message.answer("Синхронизация уже выполняется.")
            return
        await message.answer(
            "Синхронизация завершена: "
            f"{result.status.value}; endpoints "
            f"{result.successful_endpoints}/"
            f"{result.successful_endpoints + result.failed_endpoints}; "
            f"создано {result.created_events}, обновлено {result.updated_events}."
        )

    async def reclassify(
        self,
        message: Message,
        command: CommandObject,
        is_admin: bool,
    ) -> None:
        if not await self._require_message_admin(message, is_admin):
            return
        event_id = self._positive_id(command.args)
        if event_id is None:
            await message.answer("Использование: /reclassify <event_id>")
            return
        try:
            await self._admin.reclassify(event_id)
        except (LookupError, ValueError) as error:
            await message.answer(f"Не удалось поставить событие в очередь: {error}")
            return
        await message.answer(f"Событие {event_id} поставлено в очередь классификации.")

    async def tickets(self, message: Message, is_admin: bool) -> None:
        if not await self._require_message_admin(message, is_admin):
            return
        tickets = await self._feedback.list_open()
        if not tickets:
            await message.answer("Открытых обращений нет.")
            return
        for ticket in tickets:
            await message.answer(
                self._ticket_text(ticket),
                reply_markup=self._feedback_gateway.ticket_keyboard(
                    ticket.ticket_id,
                    blocked=False,
                ),
            )

    async def block(
        self,
        message: Message,
        command: CommandObject,
        current_user: User,
        is_admin: bool,
    ) -> None:
        await self._set_block_from_command(message, command, current_user, is_admin, True)

    async def unblock(
        self,
        message: Message,
        command: CommandObject,
        current_user: User,
        is_admin: bool,
    ) -> None:
        await self._set_block_from_command(message, command, current_user, is_admin, False)

    async def broadcast(self, message: Message, is_admin: bool) -> None:
        if not await self._require_message_admin(message, is_admin):
            return
        await message.answer(
            "Выберите аудиторию рассылки.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        self._button(
                            "RU", BroadcastAudienceCallback(audience=BroadcastAudience.RU)
                        ),
                        self._button(
                            "KZ", BroadcastAudienceCallback(audience=BroadcastAudience.KZ)
                        ),
                        self._button(
                            "RU + KZ",
                            BroadcastAudienceCallback(audience=BroadcastAudience.BOTH),
                        ),
                    ]
                ]
            ),
        )

    async def action(
        self,
        callback: CallbackQuery,
        callback_data: AdminActionCallback,
        is_admin: bool,
    ) -> None:
        if not await self._require_callback_admin(callback, is_admin):
            return
        await callback.answer()
        if callback.message is None:
            return
        if callback_data.action is AdminAction.STATUS:
            await callback.message.answer(await self._status_text())
        elif callback_data.action is AdminAction.STATS:
            await callback.message.answer(await self._stats_text())
        elif callback_data.action is AdminAction.SYNC:
            await callback.message.answer("Синхронизация запущена.")
            try:
                result = await self._sync.run(SyncTrigger.MANUAL)
            except JobAlreadyRunningError:
                await callback.message.answer("Синхронизация уже выполняется.")
                return
            await callback.message.answer(f"Синхронизация: {result.status.value}.")
        elif callback_data.action is AdminAction.TICKETS:
            tickets = await self._feedback.list_open()
            if not tickets:
                await callback.message.answer("Открытых обращений нет.")
            for ticket in tickets:
                await callback.message.answer(
                    self._ticket_text(ticket),
                    reply_markup=self._feedback_gateway.ticket_keyboard(
                        ticket.ticket_id,
                        blocked=False,
                    ),
                )
        elif callback_data.action is AdminAction.BROADCAST and isinstance(
            callback.message, Message
        ):
            await self.broadcast(callback.message, True)
        else:
            await callback.message.answer("Администрирование", reply_markup=self._admin_keyboard())

    async def ticket_action(
        self,
        callback: CallbackQuery,
        callback_data: AdminTicketCallback,
        current_user: User,
        is_admin: bool,
    ) -> None:
        if not await self._require_callback_admin(callback, is_admin):
            return
        ticket = await self._feedback.ticket(callback_data.ticket_id)
        if ticket is None:
            await callback.answer("Обращение не найдено.", show_alert=True)
            return
        if callback_data.action is AdminTicketAction.VIEW:
            await callback.answer()
            if callback.message is not None:
                await callback.message.answer(self._ticket_text(ticket))
            return
        if callback_data.action is AdminTicketAction.REPLY:
            if ticket.status is TicketStatus.CLOSED:
                await callback.answer("Обращение уже закрыто.", show_alert=True)
                return
            await self._feedback.begin_admin_reply(current_user.id, ticket.ticket_id)
            await callback.answer()
            if callback.message is not None:
                await callback.message.answer(f"Введите ответ для обращения #{ticket.ticket_id}.")
            return
        if callback_data.action is AdminTicketAction.CLOSE:
            await self._feedback.close(ticket.ticket_id)
            try:
                await self._feedback_gateway.notify_closed(ticket.telegram_id)
            except TelegramSendError as error:
                if error.blocked:
                    await self._users.deactivate(ticket.user_id)
            await callback.answer("Обращение закрыто.")
            return
        blocked = callback_data.action is AdminTicketAction.BLOCK
        await self._feedback.set_blocked(
            ticket.ticket_id,
            blocked=blocked,
            admin_telegram_id=current_user.telegram_id,
        )
        await callback.answer("Блокировка обновлена.")
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(
                reply_markup=self._feedback_gateway.ticket_keyboard(
                    ticket.ticket_id,
                    blocked=blocked,
                )
            )

    async def choose_audience(
        self,
        callback: CallbackQuery,
        callback_data: BroadcastAudienceCallback,
        current_user: User,
        is_admin: bool,
    ) -> None:
        if not await self._require_callback_admin(callback, is_admin):
            return
        draft = await self._broadcasts.create_draft(
            current_user.telegram_id,
            callback_data.audience,
        )
        await callback.answer()
        if callback.message is not None:
            await callback.message.answer(self._body_prompt(draft))

    async def broadcast_action(
        self,
        callback: CallbackQuery,
        callback_data: BroadcastActionCallback,
        current_user: User,
        is_admin: bool,
    ) -> None:
        if not await self._require_callback_admin(callback, is_admin):
            return
        draft = await self._broadcasts.draft(callback_data.broadcast_id)
        if (
            draft is None
            or draft.created_by_telegram_id != current_user.telegram_id
            or draft.status is not BroadcastStatus.DRAFT
        ):
            await callback.answer("Черновик недоступен.", show_alert=True)
            return
        if callback_data.action is BroadcastAction.CANCEL:
            await self._broadcasts.cancel(draft.broadcast_id)
            await callback.answer("Рассылка отменена.")
            return
        if callback_data.action is BroadcastAction.SKIP_PHOTO:
            draft = await self._broadcasts.set_photo(draft.broadcast_id, None)
            await callback.answer()
            if callback.message is not None:
                await callback.message.answer(
                    self._broadcast_preview(draft),
                    reply_markup=self._broadcast_confirm_keyboard(draft.broadcast_id),
                )
            return
        try:
            report = await self._broadcasts.confirm(draft.broadcast_id)
        except BroadcastStateError:
            await callback.answer("Черновик заполнен не полностью.", show_alert=True)
            return
        await callback.answer("Рассылка подтверждена.")
        if callback.message is not None:
            await callback.message.answer(
                f"Рассылка #{report.broadcast_id}: получателей {report.total}, "
                f"статус {report.status.value}."
            )

    async def handle_message(
        self,
        message: Message,
        current_user: User,
        is_admin: bool,
    ) -> None:
        if not is_admin:
            raise SkipHandler
        ticket_id = await self._feedback.pending_admin_reply(current_user.id)
        if ticket_id is not None:
            if not message.text:
                await message.answer("Ответ должен быть текстовым сообщением.")
                return
            ticket = await self._feedback.ticket(ticket_id)
            if ticket is None or ticket.status is TicketStatus.CLOSED:
                await message.answer("Обращение уже недоступно.")
                return
            try:
                telegram_message_id = await self._feedback_gateway.send_user_reply(
                    ticket.telegram_id,
                    message.text,
                )
            except TelegramSendError as error:
                if error.blocked:
                    await self._users.deactivate(ticket.user_id)
                await message.answer("Не удалось доставить ответ пользователю.")
                return
            await self._feedback.complete_admin_reply(
                current_user.id,
                ticket_id,
                message.text,
                telegram_message_id,
            )
            await message.answer(f"Ответ по обращению #{ticket_id} доставлен.")
            return

        draft = await self._broadcasts.active_draft(current_user.telegram_id)
        if draft is None:
            raise SkipHandler
        missing = self._missing_locales(draft)
        if missing:
            if not message.text:
                await message.answer(self._body_prompt(draft))
                return
            draft = await self._broadcasts.set_body(
                draft.broadcast_id,
                missing[0],
                message.text,
            )
            if self._missing_locales(draft):
                await message.answer(self._body_prompt(draft))
            else:
                await message.answer(
                    "Прикрепите одно фото или пропустите шаг.",
                    reply_markup=self._broadcast_photo_keyboard(draft.broadcast_id),
                )
            return
        if not message.photo:
            await message.answer(
                "Ожидается фото или кнопка «Без фото».",
                reply_markup=self._broadcast_photo_keyboard(draft.broadcast_id),
            )
            return
        draft = await self._broadcasts.set_photo(
            draft.broadcast_id,
            message.photo[-1].file_id,
        )
        await message.answer(
            self._broadcast_preview(draft),
            reply_markup=self._broadcast_confirm_keyboard(draft.broadcast_id),
        )

    async def _status_text(self) -> str:
        status = await self._admin.status()
        return (
            "Состояние приложения\n"
            f"Baseline: {status.baseline_completed_at or 'нет'}\n"
            f"Последняя полная синхронизация: {status.last_full_success_at or 'нет'}\n"
            f"Endpoint с ошибками: {status.endpoint_failures}\n"
            f"Классификация в очереди: {status.pending_classifications}\n"
            f"Outbox в очереди: {status.pending_outbox}\n"
            f"Незавершённые рассылки: {status.unfinished_broadcasts}"
        )

    async def _stats_text(self) -> str:
        stats = await self._admin.stats()
        return (
            "Статистика\n"
            f"Пользователи: {stats.users} (активных {stats.active_users})\n"
            f"Подписки на всё: {stats.subscribe_all}\n"
            f"Подписки на категории: {stats.category_subscriptions}\n"
            f"События: {stats.events}\n"
            f"Открытые обращения: {stats.open_tickets}\n"
            f"Рассылки: {stats.broadcasts}\n"
            f"OpenAI за месяц: ${stats.openai_cost_usd}"
        )

    async def _set_block_from_command(
        self,
        message: Message,
        command: CommandObject,
        current_user: User,
        is_admin: bool,
        blocked: bool,
    ) -> None:
        if not await self._require_message_admin(message, is_admin):
            return
        ticket_id = self._positive_id(command.args)
        if ticket_id is None:
            name = "block" if blocked else "unblock"
            await message.answer(f"Использование: /{name} <ticket_id>")
            return
        try:
            await self._feedback.set_blocked(
                ticket_id,
                blocked=blocked,
                admin_telegram_id=current_user.telegram_id,
            )
        except LookupError:
            await message.answer("Обращение не найдено.")
            return
        await message.answer("Блокировка обновлена.")

    @staticmethod
    def _ticket_text(ticket: object) -> str:
        from terricon_events_bot.application.feedback import FeedbackTicketView

        assert isinstance(ticket, FeedbackTicketView)
        username = f"@{ticket.username}" if ticket.username else "не указан"
        lines = [
            f"Обращение #{ticket.ticket_id}",
            f"Тип: {ticket.kind.value}",
            f"Статус: {ticket.status.value}",
            f"Имя: {ticket.display_name or 'не указано'}",
            f"Username: {username}",
            "",
        ]
        for author, body, file_id in ticket.messages:
            prefix = "Пользователь" if author is FeedbackAuthor.USER else "Администратор"
            lines.append(f"{prefix}: {body or '[фото]'}")
            if file_id:
                lines.append("Фото приложено.")
        return "\n".join(lines)[:4096]

    @staticmethod
    def _missing_locales(draft: BroadcastDraft) -> tuple[Locale, ...]:
        required = (
            (Locale.RU,)
            if draft.audience is BroadcastAudience.RU
            else (Locale.KZ,)
            if draft.audience is BroadcastAudience.KZ
            else (Locale.RU, Locale.KZ)
        )
        return tuple(locale for locale in required if locale not in draft.bodies)

    def _body_prompt(self, draft: BroadcastDraft) -> str:
        missing = self._missing_locales(draft)
        locale = missing[0] if missing else None
        return f"Введите текст рассылки для {locale.value.upper()}." if locale else "Текст готов."

    @staticmethod
    def _broadcast_preview(draft: BroadcastDraft) -> str:
        sections = [f"Предпросмотр рассылки #{draft.broadcast_id}"]
        for locale in (Locale.RU, Locale.KZ):
            body = draft.bodies.get(locale)
            if body is not None:
                shortened = body if len(body) <= 1600 else f"{body[:1599]}…"
                sections.extend(("", f"{locale.value.upper()}:\n{shortened}"))
        sections.extend(("", f"Фото: {'да' if draft.telegram_file_id else 'нет'}"))
        return "\n".join(sections)[:4096]

    @staticmethod
    def _admin_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    AdminTelegramController._button(
                        "Статус", AdminActionCallback(action=AdminAction.STATUS)
                    ),
                    AdminTelegramController._button(
                        "Статистика", AdminActionCallback(action=AdminAction.STATS)
                    ),
                ],
                [
                    AdminTelegramController._button(
                        "Синхронизация", AdminActionCallback(action=AdminAction.SYNC)
                    ),
                    AdminTelegramController._button(
                        "Обращения", AdminActionCallback(action=AdminAction.TICKETS)
                    ),
                ],
                [
                    AdminTelegramController._button(
                        "Рассылка", AdminActionCallback(action=AdminAction.BROADCAST)
                    )
                ],
            ]
        )

    @staticmethod
    def _broadcast_photo_keyboard(broadcast_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    AdminTelegramController._button(
                        "Без фото",
                        BroadcastActionCallback(
                            broadcast_id=broadcast_id,
                            action=BroadcastAction.SKIP_PHOTO,
                        ),
                    )
                ],
                [
                    AdminTelegramController._button(
                        "Отмена",
                        BroadcastActionCallback(
                            broadcast_id=broadcast_id,
                            action=BroadcastAction.CANCEL,
                        ),
                    )
                ],
            ]
        )

    @staticmethod
    def _broadcast_confirm_keyboard(broadcast_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    AdminTelegramController._button(
                        "Подтвердить",
                        BroadcastActionCallback(
                            broadcast_id=broadcast_id,
                            action=BroadcastAction.CONFIRM,
                        ),
                    ),
                    AdminTelegramController._button(
                        "Отмена",
                        BroadcastActionCallback(
                            broadcast_id=broadcast_id,
                            action=BroadcastAction.CANCEL,
                        ),
                    ),
                ]
            ]
        )

    @staticmethod
    def _button(text: str, callback_data: object) -> InlineKeyboardButton:
        from aiogram.filters.callback_data import CallbackData

        assert isinstance(callback_data, CallbackData)
        return InlineKeyboardButton(text=text, callback_data=callback_data.pack())

    @staticmethod
    async def _require_message_admin(message: Message, is_admin: bool) -> bool:
        if is_admin:
            return True
        await message.answer("Команда доступна только администраторам.")
        return False

    @staticmethod
    async def _require_callback_admin(callback: CallbackQuery, is_admin: bool) -> bool:
        if is_admin:
            return True
        await callback.answer("Действие доступно только администраторам.", show_alert=True)
        return False

    @staticmethod
    def _positive_id(value: str | None) -> int | None:
        if value is None or not value.strip().isdigit():
            return None
        result = int(value.strip())
        return result if result > 0 else None


def build_admin_router(
    controller: AdminTelegramController,
    middleware: UserContextMiddleware,
) -> Router:
    router = Router(name="admin-ui")
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)

    router.message.register(controller.menu, Command("admin"))
    router.message.register(controller.status, Command("status"))
    router.message.register(controller.stats, Command("stats"))
    router.message.register(controller.sync, Command("sync"))
    router.message.register(controller.reclassify, Command("reclassify"))
    router.message.register(controller.tickets, Command("tickets"))
    router.message.register(controller.block, Command("block"))
    router.message.register(controller.unblock, Command("unblock"))
    router.message.register(controller.broadcast, Command("broadcast"))
    router.callback_query.register(controller.action, AdminActionCallback.filter())
    router.callback_query.register(controller.ticket_action, AdminTicketCallback.filter())
    router.callback_query.register(
        controller.choose_audience,
        BroadcastAudienceCallback.filter(),
    )
    router.callback_query.register(
        controller.broadcast_action,
        BroadcastActionCallback.filter(),
    )
    router.message.register(controller.handle_message, F.text | F.photo)
    return router
