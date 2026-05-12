"""LLM-детектор спама — второй слой после regex (spam_detector.py).

Запускается только когда:
  1. spam_detector.detect() вернул is_spam=False
  2. spam_detector.is_spam_candidate() вернул True (есть @mention + коммерческие слова)

Использует YandexGPT с коротким фокусированным промтом (max_tokens=100).
При любой ошибке API возвращает is_spam=False (fail-safe): лучше пропустить
единичное спам-сообщение, чем ошибочно забанить жильца.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from balt_dom_bot.config import YandexGptConfig
from balt_dom_bot.log import get_logger
from balt_dom_bot.prompts.spam_checker import SPAM_CHECKER_SYSTEM_PROMPT
from balt_dom_bot.services.spam_detector import _normalize_obfuscated
from balt_dom_bot.services.yandex_gpt import GptMessage, YandexGptClient

log = get_logger(__name__)

_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)

_VALID_CATEGORIES = frozenset({"drugs", "earn", "crypto", "esoteric", "ads"})

# Фрагменты, характерные для отказа модели отвечать.
# Отказ от ответа = контент, скорее всего, является подозрительным.
_REFUSAL_FRAGMENTS: frozenset[str] = frozenset({
    "не могу обсуждать",
    "не могу помочь",
    "не могу отвечать",
    "не буду обсуждать",
    "не стану обсуждать",
    "это выходит за рамки",
    "давайте поговорим о",
    "давайте обсудим что",
    "не могу выполнить",
})


@dataclass
class SpamLLMVerdict:
    is_spam: bool
    category: str | None = None   # "drugs"|"earn"|"crypto"|"esoteric"|"ads"|None
    reason: str = ""               # краткое объяснение для логов / карточки управляющего


def _parse_verdict(raw: str) -> SpamLLMVerdict:
    """Разбирает JSON из ответа LLM.

    Если LLM отказывается отвечать (safety refusal) → is_spam=True:
    легитимные сообщения жильцов никогда не вызывают отказ модели.
    При технических ошибках (пустой ответ, сломанный JSON) → is_spam=False.
    """
    raw = raw.strip()
    # Убираем markdown-обёртку если есть
    if raw.startswith("```"):
        nl = raw.find("\n")
        raw = raw[nl + 1:].rstrip("`").strip() if nl != -1 else raw[3:].rstrip("`").strip()
    m = _JSON_RE.search(raw)
    if m is None:
        # Проверяем: отказ модели или технический сбой?
        raw_lower = raw.lower()
        if any(f in raw_lower for f in _REFUSAL_FRAGMENTS):
            log.warning("spam_llm.safety_refusal", raw_preview=raw[:120])
            # Отказ = контент подозрителен. Легитимные сообщения не вызывают отказов.
            return SpamLLMVerdict(is_spam=True, reason="llm_refused")
        log.warning("spam_llm.parse_no_json", raw_preview=raw[:100])
        return SpamLLMVerdict(is_spam=False, reason="parse_error")
    try:
        data = json.loads(m.group(0))
        category = data.get("category") or None
        if category not in _VALID_CATEGORIES:
            category = None
        return SpamLLMVerdict(
            is_spam=bool(data.get("is_spam", False)),
            category=category,
            reason=str(data.get("reason") or "")[:120],
        )
    except (json.JSONDecodeError, TypeError):
        log.warning("spam_llm.parse_json_error", raw_preview=raw[:100])
        return SpamLLMVerdict(is_spam=False, reason="json_error")


class SpamLLMChecker:
    """Бинарный LLM-классификатор: спам или нет.

    Передаёт LLM оригинальный текст и (если отличается) нормализованный —
    чтобы модель видела и обфусцированный оригинал, и читаемую форму.
    """

    def __init__(self, gpt: YandexGptClient, gpt_cfg: YandexGptConfig):
        self._gpt = gpt
        self._cfg = gpt_cfg

    async def check(self, text: str) -> SpamLLMVerdict:
        """Проверяет текст на спам. Fail-safe: ошибки API → is_spam=False.

        LLM получает НОРМАЛИЗОВАННЫЙ текст: emoji убраны, гомоглифы заменены
        кириллицей. Это предотвращает срабатывание safety-фильтров модели на
        emoji-маркеры запрещённых веществ (❄️🍁 и т.п.) в оригинале.
        """
        norm = _normalize_obfuscated(text)
        is_obfuscated = norm.strip().lower() != text.strip().lower()
        if is_obfuscated:
            user_msg = (
                f"Нормализованный текст (из обфусцированного оригинала — "
                f"гомоглифы и emoji заменены):\n«{norm}»"
            )
        else:
            user_msg = f"«{text}»"

        try:
            raw = await self._gpt.complete(
                [
                    GptMessage(role="system", text=SPAM_CHECKER_SYSTEM_PROMPT),
                    GptMessage(role="user", text=user_msg),
                ],
                temperature=0.0,   # детерминированность важна для безопасности
                max_tokens=100,    # короткий ответ: только JSON
            )
        except Exception as exc:
            log.warning(
                "spam_llm.api_error",
                error=str(exc), error_type=type(exc).__name__,
                preview=text[:80],
            )
            return SpamLLMVerdict(is_spam=False, reason=f"api_error")

        verdict = _parse_verdict(raw)
        log.info(
            "spam_llm.verdict",
            is_spam=verdict.is_spam,
            category=verdict.category,
            reason=verdict.reason,
            preview=text[:80],
        )
        return verdict
