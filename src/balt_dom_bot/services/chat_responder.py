"""Генератор реплик для chat-mode (диалогового режима).

Отличается от FaqFirstResponder тем, что:
* всегда идёт в LLM (FAQ слишком статичен для диалога)
* передаёт историю переписки последних N реплик
* использует отдельный системный промт (chat_mode)

Если LLM упал — возвращает безопасный fallback.
"""

from __future__ import annotations

from balt_dom_bot.config import YandexGptConfig
from balt_dom_bot.log import get_logger
from balt_dom_bot.models import Classification, ComplexInfo
from balt_dom_bot.prompts.chat_mode import CHAT_MODE_SYSTEM_PROMPT
from balt_dom_bot.services.sanitizer import sanitize_response
from balt_dom_bot.services.yandex_gpt import GptMessage, YandexGptClient
from balt_dom_bot.storage.chat_mode_repo import ChatMessage

log = get_logger(__name__)


_FALLBACK_REPLY = (
    "Здравствуйте! Обращение принято. Для уточнения деталей — что именно "
    "Вы хотели бы сообщить?"
)


class ChatResponder:
    def __init__(
        self,
        *,
        gpt: YandexGptClient,
        gpt_cfg: YandexGptConfig,
    ):
        self._gpt = gpt
        self._cfg = gpt_cfg

    async def respond(
        self,
        *,
        text: str,
        history: list[ChatMessage],
        complex_info: ComplexInfo,
        classification: Classification | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """Генерирует реплику с учётом истории."""
        # Собираем контекст: system → history → user.
        prompt = system_prompt or CHAT_MODE_SYSTEM_PROMPT

        messages: list[GptMessage] = [GptMessage(role="system", text=prompt)]

        ctx_lines = [
            f"Жилой комплекс: {complex_info.name}, адрес: {complex_info.address}.",
        ]
        if classification and classification.name:
            ctx_lines.append(f"Имя жильца: {classification.name}.")
        messages.append(GptMessage(role="system", text="\n".join(ctx_lines)))

        for m in history:
            # role в БД — 'user'/'assistant', типы совпадают.
            messages.append(GptMessage(role=m.role, text=m.text))  # type: ignore[arg-type]

        messages.append(GptMessage(role="user", text=text))

        try:
            reply = await self._gpt.complete(
                messages=messages,
                temperature=0.5,  # выше чем у responder — нужна живая речь
                max_tokens=400,
            )
        except Exception as exc:
            log.warning("chat_responder.llm_error", error=str(exc))
            return _FALLBACK_REPLY

        if not reply or len(reply.strip()) < 5:
            log.warning("chat_responder.empty_reply")
            return _FALLBACK_REPLY

        return sanitize_response(reply.strip())
