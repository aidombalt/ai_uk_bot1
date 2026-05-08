"""Форматирует черновик ответа управляющего в официальный стиль УК.

Принимает сырой текст управляющего («будем чинить в пятницу, звоните диспетчеру»)
и возвращает вежливую официальную формулировку без обещаний конкретных сроков.
Если LLM недоступен — возвращает None, и вызывающий код предлагает только оригинал.
"""

from __future__ import annotations

from balt_dom_bot.config import YandexGptConfig
from balt_dom_bot.log import get_logger
from balt_dom_bot.services.sanitizer import sanitize_response
from balt_dom_bot.services.yandex_gpt import GptMessage, YandexGptClient

log = get_logger(__name__)

_SYSTEM_PROMPT = """\
Ты — AI-редактор управляющей компании жилого комплекса. \
Задача: взять черновик ответа управляющего жильцу и переформулировать его \
в вежливый, нейтральный, официальный тон от лица УК. \
Сохрани СМЫСЛ и СОДЕРЖАНИЕ — только улучши формулировку.

Требования:
• Вежливо и нейтрально, без эмоций
• Без «мы», «наша компания», «я» — только безличные конструкции или пассив
• Без markdown, без списков, без эмодзи
• Максимум 3 предложения
• Без канцелярита («настоящим уведомляем», «доводим до сведения»)
• Если черновик уже написан нормально — достаточно минимальных правок
• НЕ добавлять информацию, которой нет в черновике

Отвечай только текстом ответа жильцу — без пояснений, без кавычек, без префиксов."""


class ReplyFormatter:
    """Переформатирует ответ управляющего через LLM в стиле УК."""

    def __init__(self, *, gpt: YandexGptClient, gpt_cfg: YandexGptConfig):
        self._gpt = gpt
        self._cfg = gpt_cfg

    async def format(self, manager_text: str, complex_name: str) -> str | None:
        """Возвращает форматированный текст или None при ошибке."""
        user_msg = (
            f"ЖК: {complex_name}\n\n"
            f"Черновик ответа управляющего:\n«{manager_text}»\n\n"
            f"Переформулируй в официальный тон УК, сохранив смысл."
        )
        try:
            raw = await self._gpt.complete(
                [
                    GptMessage(role="system", text=_SYSTEM_PROMPT),
                    GptMessage(role="user", text=user_msg),
                ],
                temperature=0.3,
                max_tokens=300,
            )
        except Exception as exc:
            log.warning("reply_formatter.llm_failed", error=str(exc))
            return None

        cleaned = sanitize_response(raw)
        if not cleaned:
            log.warning("reply_formatter.empty_after_sanitize", raw_len=len(raw))
            return None

        log.info("reply_formatter.ok", chars=len(cleaned))
        return cleaned
