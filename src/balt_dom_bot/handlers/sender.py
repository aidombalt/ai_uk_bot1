"""Адаптеры `MessageSender`/`ReplySender` поверх maxapi.Bot."""

from __future__ import annotations

from typing import Any

from balt_dom_bot.log import get_logger

log = get_logger(__name__)


def _extract_mid(sent_message: Any) -> str | None:
    """Достаёт mid из ответа maxapi.send_message.

    Структура: SendedMessage → .message: Message → .body: MessageBody → .mid: str
    """
    if sent_message is None:
        return None
    # Путь по нормальной цепочке SendedMessage → message → body → mid
    msg = getattr(sent_message, "message", None)
    if msg is not None:
        body = getattr(msg, "body", None)
        if body is not None:
            mid = getattr(body, "mid", None)
            if mid:
                return str(mid)
    # Fallback на старые варианты, если SDK когда-то вернёт по-другому
    body = getattr(sent_message, "body", None)
    if body is not None:
        mid = getattr(body, "mid", None)
        if mid:
            return str(mid)
    mid = getattr(sent_message, "mid", None)
    return str(mid) if mid else None


def _build_inline_keyboard(approve_payload: str, ignore_payload: str) -> list[Any]:
    """Строит inline-клавиатуру с двумя кнопками (approve / ignore)."""
    from maxapi.types.attachments.buttons import CallbackButton  # type: ignore[import-not-found]
    from maxapi.utils.inline_keyboard import InlineKeyboardBuilder  # type: ignore[import-not-found]

    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Одобрить автоответ", payload=approve_payload),
        CallbackButton(text="🙈 Игнорировать", payload=ignore_payload),
    )
    return [kb.as_markup()]


class MaxBotReplySender:
    """Отправка публичного ответа в чат ЖК."""

    def __init__(self, bot: Any):
        self._bot = bot

    async def send_reply(self, *, chat_id: int, text: str, reply_to_mid: str | None) -> None:
        try:
            link = None
            if reply_to_mid:
                from maxapi.types.message import MessageLinkType, NewMessageLink  # type: ignore[import-not-found]
                link = NewMessageLink(type=MessageLinkType.REPLY, mid=reply_to_mid)
            await self._bot.send_message(chat_id=chat_id, text=text, link=link)
            log.info("reply.sent", chat_id=chat_id, chars=len(text), reply_to_mid=reply_to_mid)
        except Exception as exc:
            log.exception("reply.failed", chat_id=chat_id, error=str(exc))


class MaxBotEscalationSender:
    """Отправка карточек эскалации.

    Методы:
    * `send_escalation_to_chat(chat_id=...)` — групповой чат «Обращения», работает всегда если бот в чате.
    * `send_escalation_to_user(user_id=...)` — личка управляющему. ВАЖНО: работает
      только если пользователь сам начал диалог с ботом (ввёл /start). Иначе Max
      возвращает 404 dialog.not.found — мы это ловим и возвращаем None.
    * `send_notification_to_chat(chat_id=..., text=...)` — уведомление БЕЗ кнопок:
      используется для логирования автоответов в чат «Обращения», когда эскалация
      не нужна (бот сам справился), но управляющему стоит видеть активность.
    """

    def __init__(self, bot: Any):
        self._bot = bot

    async def send_notification_to_chat(self, *, chat_id: int, text: str) -> str | None:
        """Уведомление без кнопок — для прозрачности работы бота."""
        try:
            sent = await self._bot.send_message(chat_id=chat_id, text=text)
            mid = _extract_mid(sent)
            log.info("notification_sender.sent", chat_id=chat_id, mid=mid)
            return mid
        except Exception as exc:
            log.warning(
                "notification_sender.failed",
                chat_id=chat_id, error=str(exc), error_type=type(exc).__name__,
            )
            return None

    async def send_notification_to_user(
        self, *, user_id: int, chat_id: int | None, text: str,
    ) -> str | None:
        """Уведомление без кнопок в личку управляющему. Best-effort.

        Сначала пробует chat_id (надёжнее, если он указан правильно).
        Если упало — пробует user_id (требует чтобы юзер /start'нул бота).
        """
        if chat_id is not None:
            try:
                sent = await self._bot.send_message(chat_id=chat_id, text=text)
                mid = _extract_mid(sent)
                log.info("notification_user.sent_via_chat_id",
                         chat_id=chat_id, mid=mid)
                return mid
            except Exception as exc:
                log.info("notification_user.chat_id_failed",
                         chat_id=chat_id, error=str(exc))
        try:
            sent = await self._bot.send_message(user_id=user_id, text=text)
            mid = _extract_mid(sent)
            log.info("notification_user.sent_via_user_id",
                     user_id=user_id, mid=mid)
            return mid
        except Exception as exc:
            log.warning("notification_user.failed",
                        user_id=user_id, error=str(exc))
            return None

    async def send_with_button(
        self,
        *,
        chat_id: int | None = None,
        user_id: int | None = None,
        text: str,
        button_text: str,
        button_payload: str,
    ) -> str | None:
        """Шлёт сообщение с одной inline-кнопкой. Используется для:
        - бан-нотификаций (кнопка «Разбанить»)
        - подозрительной активности (опционально кнопка действия)
        """
        try:
            from maxapi.types.attachments.buttons.callback_button import (
                CallbackButton,
            )
            from maxapi.types.attachments.inline_keyboard.inline_keyboard_attachment import (
                InlineKeyboardBuilder,
            )
            builder = InlineKeyboardBuilder()
            builder.row(CallbackButton(
                text=button_text[:64], payload=button_payload[:256],
            ))
            kwargs: dict[str, Any] = {
                "text": text, "attachments": [builder.as_markup()],
            }
            if chat_id is not None:
                kwargs["chat_id"] = chat_id
            elif user_id is not None:
                kwargs["user_id"] = user_id
            else:
                return None
            sent = await self._bot.send_message(**kwargs)
            return _extract_mid(sent)
        except Exception as exc:
            log.warning(
                "send_with_button.failed",
                chat_id=chat_id, user_id=user_id,
                error=str(exc), error_type=type(exc).__name__,
            )
            return None

    async def send_escalation_to_chat(
        self,
        *,
        chat_id: int,
        text: str,
        approve_payload: str,
        ignore_payload: str,
    ) -> str | None:
        kb = _build_inline_keyboard(approve_payload, ignore_payload)
        try:
            sent = await self._bot.send_message(chat_id=chat_id, text=text, attachments=kb)
            mid = _extract_mid(sent)
            log.info("escalation_sender.chat_sent", chat_id=chat_id, mid=mid)
            return mid
        except Exception as exc:
            log.warning(
                "escalation_sender.chat_failed",
                chat_id=chat_id, error=str(exc), error_type=type(exc).__name__,
            )
            return None

    async def send_escalation_to_user(
        self,
        *,
        user_id: int,  # на самом деле chat_id личного диалога (исторически)
        text: str,
        approve_payload: str,
        ignore_payload: str,
    ) -> str | None:
        """Отправляет в личный диалог управляющего.

        В БД хранится `chat_id` личного диалога (не user_id), поэтому пробуем
        сначала `chat_id=`, fallback на `user_id=`. Параметр назван `user_id`
        исторически — менять интерфейс ради косметики не стоит.
        """
        kb = _build_inline_keyboard(approve_payload, ignore_payload)
        # 1) Сначала пробуем как chat_id (это и есть наше число из БД).
        try:
            sent = await self._bot.send_message(chat_id=user_id, text=text, attachments=kb)
            mid = _extract_mid(sent)
            log.info("escalation_sender.personal_sent_via_chat_id", chat_id=user_id, mid=mid)
            return mid
        except TypeError:
            # SDK не принимает chat_id (старая версия) — пробуем user_id ниже.
            log.debug("escalation_sender.chat_id_not_supported")
        except Exception as exc:
            log.warning(
                "escalation_sender.personal_chat_id_failed",
                chat_id=user_id, error=str(exc), error_type=type(exc).__name__,
            )

        # 2) Fallback: пробуем как user_id (если число — реально user_id).
        try:
            sent = await self._bot.send_message(user_id=user_id, text=text, attachments=kb)
            mid = _extract_mid(sent)
            log.info("escalation_sender.personal_sent_via_user_id", user_id=user_id, mid=mid)
            return mid
        except Exception as exc:
            log.warning(
                "escalation_sender.personal_user_id_failed",
                user_id=user_id, error=str(exc), error_type=type(exc).__name__,
                hint=(
                    "Управляющий должен открыть бота и нажать /start, чтобы "
                    "Max разрешил боту писать в этот личный диалог. "
                    "Или используйте чат «Обращения» вместо личной отправки."
                ),
            )
            return None

    async def edit_escalation_card(
        self,
        *,
        manager_chat_id: int,
        manager_message_id: str,
        text: str,
    ) -> None:
        """Редактирует текст карточки. ВНИМАНИЕ: кнопки в Max через
        edit_message с пустым attachments НЕ убираются — это особенность Max API.
        Для resolve (одобрено/проигнорировано) используйте
        `resolve_escalation_card` — она удаляет старое и шлёт новое.
        """
        if not manager_message_id:
            return
        try:
            await self._bot.edit_message(message_id=manager_message_id, text=text)
            log.info("escalation_sender.card_edited", mid=manager_message_id)
        except Exception as exc:
            log.warning(
                "escalation_sender.edit_failed",
                mid=manager_message_id, error=str(exc), error_type=type(exc).__name__,
            )

    async def send_manager_reply_choice(
        self,
        *,
        chat_id: int,
        text: str,
        draft_id: int,
        has_formatted: bool,
    ) -> str | None:
        """Карточка выбора варианта ответа управляющего (formatted vs original)."""
        try:
            from maxapi.types.attachments.buttons import CallbackButton  # type: ignore[import-not-found]
            from maxapi.utils.inline_keyboard import InlineKeyboardBuilder  # type: ignore[import-not-found]

            kb = InlineKeyboardBuilder()
            buttons = []
            if has_formatted:
                buttons.append(
                    CallbackButton(
                        text="✅ Отправить форматированный",
                        payload=f"mr:f:{draft_id}",
                    )
                )
            buttons.append(
                CallbackButton(
                    text="📝 Отправить оригинал",
                    payload=f"mr:o:{draft_id}",
                )
            )
            kb.row(*buttons)
            sent = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                attachments=[kb.as_markup()],
            )
            mid = _extract_mid(sent)
            log.info(
                "manager_reply_choice.sent",
                chat_id=chat_id, draft_id=draft_id, mid=mid,
            )
            return mid
        except Exception as exc:
            log.warning(
                "manager_reply_choice.failed",
                chat_id=chat_id, draft_id=draft_id,
                error=str(exc), error_type=type(exc).__name__,
            )
            return None

    async def resolve_escalation_card(
        self,
        *,
        chat_id: int,
        old_message_id: str,
        text: str,
    ) -> str | None:
        """Заменяет карточку с кнопками на финальное сообщение БЕЗ кнопок.

        Алгоритм: удаляем старое + шлём новое в тот же чат. Это единственный
        надёжный способ убрать inline-клавиатуру в Max — `edit_message` не
        очищает attachments при пустом списке.
        """
        if not old_message_id or not chat_id:
            return None
        # 1) Удалить карточку
        try:
            await self._bot.delete_message(message_id=old_message_id)
            log.info("escalation_sender.card_deleted", mid=old_message_id, chat_id=chat_id)
        except Exception as exc:
            # Если бот не имеет прав на delete (не админ) — карточка останется
            # с кнопками, но finalize-сообщение всё равно отправим.
            log.warning(
                "escalation_sender.delete_failed",
                mid=old_message_id, error=str(exc), error_type=type(exc).__name__,
                hint="Боту нужны права на удаление сообщений (админ в групповом чате).",
            )
        # 2) Отправить финальное сообщение без кнопок
        try:
            sent = await self._bot.send_message(chat_id=chat_id, text=text)
            mid = _extract_mid(sent)
            log.info("escalation_sender.resolved_sent", chat_id=chat_id, mid=mid)
            return mid
        except Exception as exc:
            log.warning(
                "escalation_sender.resolved_send_failed",
                chat_id=chat_id, error=str(exc), error_type=type(exc).__name__,
            )
            return None
