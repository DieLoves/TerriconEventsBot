from __future__ import annotations

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from terricon_events_bot.application.delivery import TelegramSendError
from terricon_events_bot.application.feedback import (
    FeedbackBlockedError,
    FeedbackNotification,
    FeedbackRateLimitedError,
    FeedbackService,
    FeedbackStateError,
)
from terricon_events_bot.application.users import UserService
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.telegram.callbacks import (
    AdminTicketAction,
    AdminTicketCallback,
    FeedbackAction,
    FeedbackActionCallback,
    FeedbackKindCallback,
)
from terricon_events_bot.telegram.delivery import normalize_telegram_error
from terricon_events_bot.telegram.rendering import ScreenRenderer
from terricon_events_bot.telegram.views import TelegramViews


class AiogramFeedbackGateway:
    def __init__(self, bot: Bot, admin_chat_id: int) -> None:
        self._bot = bot
        self._admin_chat_id = admin_chat_id

    async def publish(self, notification: FeedbackNotification) -> tuple[int, int]:
        if notification.telegram_file_id is not None:
            try:
                await self._bot.send_photo(
                    chat_id=self._admin_chat_id,
                    photo=notification.telegram_file_id,
                )
            except TelegramAPIError as error:
                raise normalize_telegram_error(error) from error
        try:
            message = await self._bot.send_message(
                chat_id=self._admin_chat_id,
                text=self._notification_text(notification),
                reply_markup=self.ticket_keyboard(notification.ticket_id, blocked=False),
            )
        except TelegramAPIError as error:
            raise normalize_telegram_error(error) from error
        return self._admin_chat_id, message.message_id

    async def send_user_reply(self, telegram_id: int, body: str) -> int:
        try:
            message = await self._bot.send_message(
                chat_id=telegram_id,
                text=f"Ответ администратора:\n\n{body}",
            )
        except TelegramAPIError as error:
            raise normalize_telegram_error(error) from error
        return message.message_id

    async def notify_closed(self, telegram_id: int) -> None:
        try:
            await self._bot.send_message(chat_id=telegram_id, text="Обращение закрыто.")
        except TelegramAPIError as error:
            raise normalize_telegram_error(error) from error

    @staticmethod
    def ticket_keyboard(ticket_id: int, *, blocked: bool) -> InlineKeyboardMarkup:
        block_action = AdminTicketAction.UNBLOCK if blocked else AdminTicketAction.BLOCK
        block_text = "Разблокировать" if blocked else "Блокировать"
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Ответить",
                        callback_data=AdminTicketCallback(
                            ticket_id=ticket_id,
                            action=AdminTicketAction.REPLY,
                        ).pack(),
                    ),
                    InlineKeyboardButton(
                        text="Закрыть",
                        callback_data=AdminTicketCallback(
                            ticket_id=ticket_id,
                            action=AdminTicketAction.CLOSE,
                        ).pack(),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=block_text,
                        callback_data=AdminTicketCallback(
                            ticket_id=ticket_id,
                            action=block_action,
                        ).pack(),
                    )
                ],
            ]
        )

    @staticmethod
    def _notification_text(notification: FeedbackNotification) -> str:
        name = notification.display_name or "не указано"
        username = f"@{notification.username}" if notification.username else "не указан"
        heading = "Новое сообщение" if notification.continuation else "Новое обращение"
        prefix = (
            f"{heading} #{notification.ticket_id}\n"
            f"Тип: {notification.kind.value}\n"
            f"Имя: {name}\n"
            f"Username: {username}\n\n"
        )
        body_limit = 4096 - len(prefix)
        body = notification.body or "[фото]"
        if len(body) > body_limit:
            body = f"{body[: body_limit - 1]}…"
        return f"{prefix}{body}"


class FeedbackTelegramController:
    def __init__(
        self,
        service: FeedbackService,
        gateway: AiogramFeedbackGateway,
        users: UserService,
        renderer: ScreenRenderer,
        views: TelegramViews,
    ) -> None:
        self._service = service
        self._gateway = gateway
        self._users = users
        self._renderer = renderer
        self._views = views

    async def choose_kind(
        self,
        callback: CallbackQuery,
        callback_data: FeedbackKindCallback,
        current_user: User,
    ) -> None:
        await callback.answer()
        try:
            await self._service.begin(current_user.id, callback_data.kind)
        except FeedbackBlockedError:
            await self._render_callback(
                callback,
                current_user,
                "feedback.blocked",
            )
            return
        if callback.message is not None:
            await self._renderer.render(
                current_user.id,
                callback.message.chat.id,
                self._views.feedback_text_prompt(current_user),
            )

    async def action(
        self,
        callback: CallbackQuery,
        callback_data: FeedbackActionCallback,
        current_user: User,
    ) -> None:
        await callback.answer()
        if callback_data.action is FeedbackAction.CANCEL:
            await self._service.cancel(current_user.id)
            if callback.message is not None:
                await self._renderer.render(
                    current_user.id,
                    callback.message.chat.id,
                    self._views.feedback(current_user),
                )
            return
        if callback_data.action is FeedbackAction.SKIP_PHOTO:
            try:
                draft = await self._service.set_photo(current_user.id, None)
            except FeedbackStateError:
                await self._render_callback(callback, current_user, "errors.invalid_action")
                return
            if callback.message is not None:
                await self._renderer.render(
                    current_user.id,
                    callback.message.chat.id,
                    self._views.feedback_preview(current_user, draft),
                )
            return
        try:
            notification = await self._service.confirm(current_user.id)
        except FeedbackRateLimitedError:
            await self._render_callback(callback, current_user, "feedback.rate_limited")
            return
        except FeedbackBlockedError:
            await self._render_callback(callback, current_user, "feedback.blocked")
            return
        except FeedbackStateError:
            await self._render_callback(callback, current_user, "errors.invalid_action")
            return
        try:
            chat_id, message_id = await self._gateway.publish(notification)
            await self._service.attach_admin_message(notification.ticket_id, chat_id, message_id)
        except TelegramSendError:
            # The ticket remains persisted and visible through the admin ticket list.
            pass
        await self._render_callback(callback, current_user, "feedback.sent")

    async def delete_profile(self, callback: CallbackQuery, current_user: User) -> None:
        await callback.answer()
        chat_id = (
            callback.message.chat.id if callback.message is not None else current_user.telegram_id
        )
        message_id = callback.message.message_id if callback.message is not None else None
        screen = self._views.feedback_notice(current_user, "privacy.deleted")
        await self._users.delete_profile(current_user.id)
        if callback.bot is None:
            return
        if message_id is not None:
            try:
                await callback.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramAPIError:
                pass
        await callback.bot.send_message(
            chat_id=chat_id,
            text=screen.text,
            parse_mode=ParseMode.HTML,
            reply_markup=screen.keyboard,
        )

    async def handle_message(self, message: Message, current_user: User) -> bool:
        draft = await self._service.draft(current_user.id)
        if draft is not None:
            if draft.state == "text":
                if message.text is None:
                    await self._renderer.render(
                        current_user.id,
                        message.chat.id,
                        self._views.feedback_text_prompt(current_user),
                    )
                    return True
                await self._service.set_text(current_user.id, message.text)
                await self._renderer.render(
                    current_user.id,
                    message.chat.id,
                    self._views.feedback_photo_prompt(current_user),
                )
                return True
            if draft.state == "photo":
                if not message.photo:
                    await self._renderer.render(
                        current_user.id,
                        message.chat.id,
                        self._views.feedback_photo_prompt(current_user),
                    )
                    return True
                ready = await self._service.set_photo(current_user.id, message.photo[-1].file_id)
                await self._renderer.render(
                    current_user.id,
                    message.chat.id,
                    self._views.feedback_preview(current_user, ready),
                )
                return True
            await self._renderer.render(
                current_user.id,
                message.chat.id,
                self._views.feedback_preview(current_user, draft),
            )
            return True

        file_id = message.photo[-1].file_id if message.photo else None
        body = message.caption if message.photo else message.text
        if body is None and file_id is None:
            return False
        try:
            notification = await self._service.add_user_reply(
                current_user.id,
                body=body,
                telegram_file_id=file_id,
            )
        except (FeedbackStateError, FeedbackBlockedError):
            return False
        try:
            chat_id, message_id = await self._gateway.publish(notification)
            await self._service.attach_admin_message(notification.ticket_id, chat_id, message_id)
        except TelegramSendError:
            pass
        await message.answer(self._views.feedback_notice(current_user, "feedback.sent").text)
        return True

    async def _render_callback(
        self, callback: CallbackQuery, user: User, localization_key: str
    ) -> None:
        if callback.message is None:
            return
        await self._renderer.render(
            user.id,
            callback.message.chat.id,
            self._views.feedback_notice(user, localization_key),
        )
