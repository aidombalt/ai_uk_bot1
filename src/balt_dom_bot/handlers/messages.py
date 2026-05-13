"""Обработчик `message_created`: нормализует событие и делегирует pipeline.

ВАЖНО: в maxapi для одного типа события должен быть только один `@dp.message_created()`,
иначе цепочка обработки прерывается. Поэтому /help-команда обрабатывается прямо
здесь, перед основным pipeline, а не отдельным хендлером.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from balt_dom_bot.log import get_logger
from balt_dom_bot.models import IncomingMessage
from balt_dom_bot.services.pipeline import Pipeline
from balt_dom_bot.handlers import admin_commands as admin_cmd
from balt_dom_bot.handlers import resident_commands as resident_cmd

if TYPE_CHECKING:
    from balt_dom_bot.handlers.manager_reply import ManagerReplyHandler

log = get_logger(__name__)


def _extract_linked_mid(msg: Any) -> str | None:
    """Извлекает mid сообщения, на которое пришёл реплай (из msg.link)."""
    try:
        link = getattr(msg, "link", None)
        if link is None:
            return None
        link_msg = getattr(link, "message", None)
        if link_msg is None:
            return None
        # Стандартная структура MaxAPI: message.body.mid
        body = getattr(link_msg, "body", None)
        if body is not None:
            mid = getattr(body, "mid", None)
            if mid:
                return str(mid)
        # Fallback: mid прямо на объекте сообщения
        mid = getattr(link_msg, "mid", None)
        return str(mid) if mid else None
    except Exception:
        return None


def register_message_handlers(
    dp: Any,
    pipeline: Pipeline,
    manager_reply_handler: "ManagerReplyHandler | None" = None,
    escalations_repo: Any = None,
) -> None:
    @dp.message_created()
    async def on_message(event: Any) -> None:  # type: ignore[no-untyped-def]
        msg = event.message
        body = getattr(msg, "body", None)
        if body is None:
            return
        text = getattr(body, "text", None)
        if not text:
            return

        sender = getattr(msg, "sender", None)
        recipient = getattr(msg, "recipient", None)
        chat_id = (
            getattr(recipient, "chat_id", None)
            or getattr(event, "chat_id", None)
        )
        if chat_id is None:
            log.debug("messages.skip_no_chat_id")
            return

        # Не реагируем на собственные сообщения.
        bot_user_id = None
        try:
            me = await event.bot.get_me()
            bot_user_id = getattr(me, "user_id", None)
        except Exception:
            pass
        sender_id = getattr(sender, "user_id", None)
        if bot_user_id and sender_id == bot_user_id:
            return

        # Admin-команды в ЛИЧНОМ диалоге с ботом. ВАЖНО: вся ветка обёрнута в
        # try/except — даже если что-то сломается в admin-логике, обработка
        # обычных сообщений жильцов в чатах ЖК должна продолжиться без сбоев.
        try:
            if (
                admin_cmd.is_command(text)
                and admin_cmd.is_dialog_chat(msg)
                and sender_id is not None
                and getattr(pipeline, "_global_settings", None) is not None
            ):
                # /me доступна ВСЕМ (не только админам) — для удобной настройки.
                cmd_low = text.strip().split()[0].lower().split("@")[0]
                if cmd_low in admin_cmd.ANYONE_COMMANDS:
                    await admin_cmd.handle_admin_command(
                        bot=event.bot, text=text,
                        user_id=sender_id, chat_id=int(chat_id),
                        user_name=getattr(sender, "name", None),
                        complexes=pipeline._complexes,
                        global_settings=pipeline._global_settings,
                        admin_complexes=[],  # пустой — это не нужно для /me
                    )
                    return
                # Остальные admin-команды — только для управляющих.
                admin_jks = await admin_cmd.is_admin(sender_id, pipeline._complexes)
                if admin_jks:
                    handled = await admin_cmd.handle_admin_command(
                        bot=event.bot, text=text,
                        user_id=sender_id, chat_id=int(chat_id),
                        user_name=getattr(sender, "name", None),
                        complexes=pipeline._complexes,
                        global_settings=pipeline._global_settings,
                        admin_complexes=admin_jks,
                    )
                    if handled:
                        return
        except Exception as exc:
            log.exception("admin_cmd.crash_falling_through", error=str(exc))

        # Пользовательские команды жильца (/help, /mystatus, /contacts...).
        # Обрабатываем ДО pipeline — в любом чате (группа или личка).
        if resident_cmd.is_resident_command(text):
            try:
                handled = await resident_cmd.handle_resident_command(
                    bot=event.bot,
                    text=text,
                    user_id=sender_id,
                    chat_id=int(chat_id),
                    complexes=pipeline._complexes,
                    escalations=escalations_repo,
                )
                if handled:
                    log.info("resident_cmd.handled", cmd=text.split()[0], chat_id=chat_id)
                    return
            except Exception as exc:
                log.exception("resident_cmd.crash", error=str(exc))

        ts = getattr(msg, "timestamp", None)
        received_at = (
            datetime.fromtimestamp(ts / 1000) if isinstance(ts, (int, float)) else datetime.now()
        )

        # Определяем, упомянут ли бот в этом сообщении. Два пути:
        # 1) Через body.markup — список разметки сообщения, содержащий
        #    user_mention элементы с user_id.
        # 2) Fallback — простой поиск подстроки @id<bot_id>_bot в тексте.
        bot_mentioned = False
        try:
            markup = getattr(body, "markup", None) or []
            if bot_user_id is not None:
                for m in markup:
                    m_type = getattr(m, "type", None)
                    m_uid = getattr(m, "user_id", None)
                    if m_type == "user_mention" and m_uid == bot_user_id:
                        bot_mentioned = True
                        break
            if not bot_mentioned and bot_user_id is not None:
                # Текстовый fallback. В Max боты обычно имеют username вида
                # `id<bot_id>_bot`, проверяем оба варианта.
                lower = text.lower()
                if f"@id{bot_user_id}_bot" in lower:
                    bot_mentioned = True
        except Exception:
            # При любой проблеме — считаем что не упомянут. Это безопасный
            # дефолт: anti-trolling логика просто не сработает на этом
            # сообщении, но обычная обработка пройдёт нормально.
            bot_mentioned = False

        # Извлекаем контекст реплая/форварда из Max API (msg.link).
        # reply_to_bot=True если жилец ответил именно на сообщение бота —
        # это ключевой сигнал для LLM: агрессия в ответ боту = addressed_to=uc.
        linked_message_text: str | None = None
        linked_message_type: str | None = None
        reply_to_bot: bool = False
        linked_sender_name: str | None = None
        linked_message_mid: str | None = None   # для manager reply flow
        try:
            link = getattr(msg, "link", None)
            if link is not None:
                link_type = getattr(link, "type", None)
                linked_message_type = str(link_type) if link_type else None
                link_msg = getattr(link, "message", None)
                link_sender = getattr(link, "sender", None)
                if link_msg:
                    linked_message_text = getattr(link_msg, "text", None)
                    linked_message_mid = _extract_linked_mid(msg)
                if link_sender:
                    linked_sender_name = getattr(link_sender, "name", None)
                    link_sender_id = getattr(link_sender, "user_id", None)
                    if bot_user_id and link_sender_id == bot_user_id:
                        reply_to_bot = True
        except Exception:
            pass

        incoming = IncomingMessage(
            chat_id=int(chat_id),
            message_id=str(getattr(body, "mid", "") or getattr(msg, "mid", "")),
            user_id=sender_id,
            user_name=getattr(sender, "name", None) or getattr(sender, "first_name", None),
            text=text,
            received_at=received_at,
            bot_mentioned=bot_mentioned,
            reply_to_bot=reply_to_bot,
            linked_message_text=linked_message_text,
            linked_message_type=linked_message_type,
            linked_sender_name=linked_sender_name,
        )

        log.info(
            "messages.received",
            chat_id=incoming.chat_id,
            user_id=incoming.user_id,
            mid=incoming.message_id,
            chars=len(text),
        )

        # Manager Reply Flow: если сообщение является реплаем управляющего
        # на отслеживаемое уведомление в чате «Обращения» — обрабатываем особо.
        # Это должно произойти ДО pipeline, т.к. pipeline просто вернёт
        # «unknown chat» для escalation_chat_id.
        if manager_reply_handler is not None and linked_message_mid:
            try:
                handled = await manager_reply_handler.try_handle(
                    chat_id=int(chat_id),
                    user_id=sender_id,
                    text=text,
                    linked_mid=linked_message_mid,
                )
                if handled:
                    return
            except Exception as exc:
                log.exception("manager_reply.crash", error=str(exc))

        try:
            await pipeline.handle(incoming)
        except Exception as exc:
            log.exception("messages.pipeline_crash", error=str(exc))
