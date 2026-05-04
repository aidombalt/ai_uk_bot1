"""Эскалация со state в SQLite + два канала (личка управляющего / чат «Обращения»).

Алгоритм:
  1. Создаём запись escalation (status=PENDING) — получаем `esc_id`.
  2. Пытаемся доставить карточку:
     - в чат «Обращения» (если `escalate_to_chat` и есть `escalation_chat_id`)
     - в личку управляющему (если `escalate_to_manager`)
  3. Если хоть один канал доставил — сохраняем mid карточки.
  4. Если оба провалились — оставляем PENDING (не IGNORED!), управляющий
     увидит обращение в GUI / можно повторить.
"""

from __future__ import annotations

from typing import Protocol

from balt_dom_bot.log import get_logger
from balt_dom_bot.models import Classification, ComplexInfo, IncomingMessage, PipelineDecision
from balt_dom_bot.storage.escalations import EscalationRepo, EscalationStatus

log = get_logger(__name__)

REASON_LABEL: dict[str, str] = {
    "aggression": "🚫 агрессия / оскорбления",
    "provocation": "⚠️ провокация",
    "high_urgency": "🆘 высокая срочность",
    "always_escalate_theme": "🏛 особая тема",
    "low_confidence": "❓ низкая уверенность классификатора",
    "llm_error": "🛠 ошибка LLM",
    "after_hours": "🌙 вне рабочих часов",
    "fragment_troll": "🧩 дробный мат / троллинг частями",
}


class MessageSender(Protocol):
    async def send_escalation_to_chat(
        self,
        *,
        chat_id: int,
        text: str,
        approve_payload: str,
        ignore_payload: str,
    ) -> str | None:
        """Отправка в групповой чат. Возвращает mid или None."""
        ...

    async def send_escalation_to_user(
        self,
        *,
        user_id: int,
        text: str,
        approve_payload: str,
        ignore_payload: str,
    ) -> str | None:
        """Отправка в личку. Возвращает mid или None.

        В Max работает только если пользователь хотя бы раз написал боту.
        Иначе вернёт None и залогирует dialog.not.found.
        """
        ...

    async def edit_escalation_card(
        self,
        *,
        manager_chat_id: int,
        manager_message_id: str,
        text: str,
    ) -> None:
        ...


def render_card(
    *,
    incoming: IncomingMessage,
    cls: Classification,
    complex_info: ComplexInfo,
    decision: PipelineDecision,
    proposed_reply: str | None,
    esc_id: int,
    prior_context: list | None = None,
) -> str:
    reason = REASON_LABEL.get(decision.escalation_reason or "", "—")
    name = cls.name or incoming.user_name or "—"
    proposed_block = (
        f"\n\n💬 Предложенный автоответ:\n«{proposed_reply}»"
        if proposed_reply
        else "\n\n💬 Автоответ не сформирован — нужен ручной."
    )
    # Предшествующие сообщения того же жильца — дают управляющему
    # полный контекст, даже если первые сообщения были к соседям.
    context_block = ""
    if prior_context:
        lines = []
        for entry in prior_context[-3:]:
            txt = (entry.text or "").replace("\n", " ").strip()[:150]
            if not txt:
                continue
            lines.append(f"  • «{txt}»")
            if entry.bot_reply:
                bot_short = entry.bot_reply.replace("\n", " ").strip()[:80]
                lines.append(f"    ↳ Бот ответил: «{bot_short}»")
        if lines:
            context_block = (
                "\n\n📋 До обращения (от того же жильца):\n" + "\n".join(lines)
            )
    return (
        f"📩 Обращение #{esc_id} | {complex_info.name} | {complex_info.address}\n"
        f"👤 Жилец: {name}\n"
        f"🏷 {cls.theme.value}  ⚡ {cls.urgency.value}  💬 {cls.character.value}  "
        f"(уверенность: {cls.confidence:.2f})\n"
        f"🎯 Причина: {reason}\n"
        f"📝 Суть: {cls.summary}\n"
        f"─────────────────\n"
        f"Оригинал: «{incoming.text}»"
        f"{context_block}"
        f"{proposed_block}"
    )


def render_resolved_card(
    *, original_text: str, status: EscalationStatus, by_user_id: int | None
) -> str:
    badge = {
        EscalationStatus.APPROVED: "✅ Автоответ одобрен и отправлен в чат ЖК",
        EscalationStatus.IGNORED: "🙈 Обращение помечено как обработанное вручную",
        EscalationStatus.PENDING: "⏳ В работе",
    }[status]
    by = f" (id={by_user_id})" if by_user_id else ""
    return f"{original_text}\n\n────────\n{badge}{by}"


class Escalator:
    def __init__(self, *, sender: MessageSender, repo: EscalationRepo):
        self._sender = sender
        self._repo = repo

    async def escalate(
        self,
        *,
        incoming: IncomingMessage,
        complex_info: ComplexInfo,
        decision: PipelineDecision,
        proposed_reply: str | None,
        prior_context: list | None = None,
    ) -> int | None:
        # 1. Создаём pending-запись.
        esc_id = await self._repo.create(
            complex_id=complex_info.id,
            incoming=incoming,
            classification=decision.classification,
            proposed_reply=proposed_reply,
            reason=decision.escalation_reason or "unknown",
            manager_chat_id=complex_info.manager_chat_id,
        )

        text = render_card(
            incoming=incoming,
            cls=decision.classification,
            complex_info=complex_info,
            decision=decision,
            proposed_reply=proposed_reply,
            esc_id=esc_id,
            prior_context=prior_context,
        )
        approve_payload = f"e:a:{esc_id}"
        ignore_payload = f"e:i:{esc_id}"

        # 2. Пробуем доставить хотя бы одним каналом. Сохраняем mid отдельно
        #    для каждого канала — нужно для синхронной правки обеих карточек
        #    при resolve.
        chat_mid: str | None = None
        chat_card_chat_id: int | None = None
        manager_mid: str | None = None
        delivered_via: list[str] = []

        # 2a. Чат «Обращения» — приоритет, потому что не требует /start.
        if complex_info.escalate_to_chat and complex_info.escalation_chat_id:
            mid = await self._sender.send_escalation_to_chat(
                chat_id=complex_info.escalation_chat_id,
                text=text,
                approve_payload=approve_payload,
                ignore_payload=ignore_payload,
            )
            if mid is not None:
                chat_mid = mid
                chat_card_chat_id = complex_info.escalation_chat_id
                delivered_via.append("chat")

        # 2b. Личка управляющего.
        if complex_info.escalate_to_manager and complex_info.manager_chat_id:
            mid = await self._sender.send_escalation_to_user(
                user_id=complex_info.manager_chat_id,
                text=text,
                approve_payload=approve_payload,
                ignore_payload=ignore_payload,
            )
            if mid is not None:
                manager_mid = mid
                delivered_via.append("manager")

        if not delivered_via:
            log.error(
                "escalation.delivery_failed_all_channels",
                esc_id=esc_id,
                manager_chat_id=complex_info.manager_chat_id,
                escalation_chat_id=complex_info.escalation_chat_id,
                escalate_to_manager=complex_info.escalate_to_manager,
                escalate_to_chat=complex_info.escalate_to_chat,
                hint=(
                    "Если to_manager: управляющий должен сначала открыть личку "
                    "с ботом и нажать /start. Если to_chat: проверьте, что бот "
                    "добавлен в чат с ID escalation_chat_id и имеет права."
                ),
            )
            return esc_id

        await self._repo.set_message_ids(
            esc_id,
            manager_message_id=manager_mid,
            chat_message_id=chat_mid,
            chat_card_chat_id=chat_card_chat_id,
        )
        log.info(
            "escalation.sent",
            esc_id=esc_id,
            via=delivered_via,
            chat_mid=chat_mid,
            manager_mid=manager_mid,
            complex_id=complex_info.id,
            reason=decision.escalation_reason,
        )
        return esc_id
