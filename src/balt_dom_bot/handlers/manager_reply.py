"""Обработчик ответов управляющего в чате «Обращения».

Поток:
  1. Управляющий пишет реплай на уведомление/карточку бота в чате «Обращения».
  2. ManagerReplyHandler.try_handle() — проверяет, что:
     a) chat_id — это escalation_chat_id какого-то ЖК,
     b) сообщение является реплаем (linked_mid не None),
     c) linked_mid есть в notification_map.
  3. Если предыдущие черновики для этого уведомления ещё PENDING — отменяет их
     (smart replace: последний ответ вытесняет предыдущий).
  4. Форматирует текст управляющего через LLM (стиль УК).
  5. Отправляет карточку выбора с двумя кнопками:
     «✅ Отправить форматированный» / «📝 Отправить оригинал».
  6. Сохраняет черновик + choice_card_mid в БД.

Нажатие кнопки обрабатывается в handlers/callbacks.py (mr:f:<id> / mr:o:<id>).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from balt_dom_bot.log import get_logger

if TYPE_CHECKING:
    from balt_dom_bot.handlers.sender import MaxBotEscalationSender
    from balt_dom_bot.services.reply_formatter import ReplyFormatter
    from balt_dom_bot.storage.complexes_repo import ComplexesRepo
    from balt_dom_bot.storage.manager_reply_repo import ManagerReplyRepo

log = get_logger(__name__)


def _format_choice_card(
    *,
    manager_text: str,
    formatted_text: str | None,
    complex_name: str,
) -> str:
    if formatted_text:
        fmt_block = f"💬 Форматированный (рекомендуется):\n«{formatted_text}»"
    else:
        fmt_block = "⚠️ AI-форматирование недоступно — LLM не ответил."
    return (
        f"📝 Ответ управляющего · {complex_name}\n"
        f"─────\n"
        f"Оригинал: «{manager_text}»\n"
        f"─────\n"
        f"{fmt_block}\n"
        f"─────\n"
        f"Выберите вариант для отправки жильцу в чат ЖК:"
    )


class ManagerReplyHandler:
    """Обрабатывает реплай управляющего на уведомление бота в чате «Обращения»."""

    def __init__(
        self,
        *,
        complexes: "ComplexesRepo",
        manager_reply_repo: "ManagerReplyRepo",
        reply_formatter: "ReplyFormatter",
        escalation_sender: "MaxBotEscalationSender",
        bot: Any,
    ):
        self._complexes = complexes
        self._repo = manager_reply_repo
        self._formatter = reply_formatter
        self._sender = escalation_sender
        self._bot = bot

    async def try_handle(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        text: str,
        linked_mid: str | None,
    ) -> bool:
        """Возвращает True если сообщение обработано как ответ управляющего.

        Если True — вызывающий код должен прекратить дальнейшую обработку.
        """
        if not linked_mid:
            return False

        # Проверяем: является ли этот чат чатом «Обращения» какого-то ЖК
        complex_row = await self._complexes.find_by_escalation_chat(chat_id)
        if complex_row is None:
            return False

        # Ищем linked_mid в notification_map
        notif = await self._repo.find_notification(linked_mid)
        if notif is None:
            # Реплай на какое-то сообщение в чате, но не на отслеживаемое
            log.debug(
                "manager_reply.notif_not_found",
                linked_mid=linked_mid, chat_id=chat_id,
            )
            return False

        log.info(
            "manager_reply.detected",
            notif_mid=linked_mid,
            complex_id=notif.complex_id,
            manager_user_id=user_id,
            resident_chat_id=notif.resident_chat_id,
        )

        # Smart replace: отменяем предыдущие PENDING черновики для этого уведомления
        old_card_mids = await self._repo.cancel_pending(linked_mid)
        for old_mid in old_card_mids:
            try:
                await self._bot.delete_message(message_id=old_mid)
            except Exception as exc:
                log.debug(
                    "manager_reply.delete_old_card_failed",
                    mid=old_mid, error=str(exc),
                )

        # Создаём новый черновик
        draft_id = await self._repo.create_draft(notif, text)

        # Форматируем через LLM (best-effort: ошибка = только оригинал)
        formatted: str | None = None
        try:
            formatted = await self._formatter.format(text, complex_row.name)
        except Exception as exc:
            log.warning("manager_reply.format_failed", error=str(exc))

        # Карточка выбора
        card_text = _format_choice_card(
            manager_text=text,
            formatted_text=formatted,
            complex_name=complex_row.name,
        )

        # Отправляем карточку в тот же чат «Обращения»
        choice_mid = await self._sender.send_manager_reply_choice(
            chat_id=chat_id,
            text=card_text,
            draft_id=draft_id,
            has_formatted=formatted is not None,
        )

        # Сохраняем formatted_text и choice_card_mid в черновике
        await self._repo.set_draft_choice_card(
            draft_id,
            formatted_text=formatted,
            choice_card_mid=choice_mid,
        )

        log.info(
            "manager_reply.draft_ready",
            draft_id=draft_id,
            choice_mid=choice_mid,
            has_formatted=formatted is not None,
            complex_id=notif.complex_id,
        )
        return True
