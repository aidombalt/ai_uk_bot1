"""Генератор ответных сообщений в стиле УК (ТЗ §3.2, §6).

Алгоритм `FaqFirstResponder.respond`:
  1. Если character == REPORT — короткое подтверждение, без LLM.
  2. Кэш per-(complex, theme, normalized_text) — если есть, отдаём.
  3. FAQ-fast-path по theme + keywords → форматирование шаблона.
  4. LLM (YandexGPT) с системным промтом из БД (через PromptProvider, дефолт — `prompts/responder.py`).
  5. Sanitizer: убираем «мы/наша УК», обрезаем до разумной длины.
  6. Сохраняем в кэш.
  7. При любой ошибке — нейтральный fallback, чтобы жилец не остался без ответа.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from balt_dom_bot.config import YandexGptConfig
from balt_dom_bot.kb.faq import find_template, format_template
from balt_dom_bot.log import get_logger
from balt_dom_bot.models import Character, Classification, ComplexInfo
from balt_dom_bot.prompts.responder import RESPONDER_SYSTEM_PROMPT, build_user_message
from balt_dom_bot.services.cache import NullResponseCache, ResponseCache
from balt_dom_bot.services.sanitizer import sanitize_response
from balt_dom_bot.services.yandex_gpt import GptMessage, YandexGptClient

if TYPE_CHECKING:
    from balt_dom_bot.storage.prompts_repo import PromptProvider

log = get_logger(__name__)


class Responder(Protocol):
    async def respond(
        self,
        *,
        classification: Classification,
        original_text: str,
        complex_info: ComplexInfo,
        manager_context: str | None = None,
    ) -> str: ...


def _safe_fallback(name: str | None) -> str:
    greeting = f"{name}, здравствуйте" if name else "Здравствуйте"
    return (
        f"{greeting}, спасибо за обращение. Запрос принят и передан "
        f"специалистам. О результатах будет сообщено дополнительно."
    )


def _report_response(name: str | None) -> str:
    greeting = f"{name}, здравствуйте" if name else "Здравствуйте"
    return (
        f"{greeting}, спасибо за информацию. Сведения зафиксированы и переданы "
        f"профильной службе."
    )


class FaqFirstResponder:
    """FAQ → LLM → safe fallback. С кэшем и пост-обработкой."""

    PROMPT_NAME = "responder_system"

    def __init__(
        self,
        *,
        gpt: YandexGptClient,
        gpt_cfg: YandexGptConfig,
        cache: ResponseCache | None = None,
        prompt_provider: "PromptProvider | None" = None,
    ):
        self._gpt = gpt
        self._cfg = gpt_cfg
        self._cache: ResponseCache = cache or NullResponseCache()
        self._prompts = prompt_provider

    async def respond(
        self,
        *,
        classification: Classification,
        original_text: str,
        complex_info: ComplexInfo,
        manager_context: str | None = None,
    ) -> str:
        cls = classification

        # 1) REPORT — фиксированный шаблон, без LLM.
        if cls.character == Character.REPORT:
            return _report_response(cls.name)

        # 2) Кэш (manager_context отключает кэш — это персонализированный ответ).
        if manager_context is None:
            cached = await self._cache.get(
                complex_id=complex_info.id,
                theme=cls.theme,
                text=original_text,
            )
            if cached:
                log.info("responder.cache_hit", theme=cls.theme.value)
                return cached

        # 3) FAQ-fast-path.
        tpl = find_template(cls.theme, original_text)
        if tpl is not None and manager_context is None:
            text = format_template(
                tpl,
                name=cls.name,
                complex_name=complex_info.name,
                address=complex_info.address,
            )
            log.info("responder.faq_hit", theme=cls.theme.value)
            await self._cache.set(
                complex_id=complex_info.id, theme=cls.theme, text=original_text, response=text
            )
            return text

        # 4) LLM.
        user_prompt = build_user_message(
            classification=cls,
            original_text=original_text,
            complex_info=complex_info,
            manager_context=manager_context,
        )

        if self._prompts is not None:
            system = await self._prompts.get(self.PROMPT_NAME, RESPONDER_SYSTEM_PROMPT)
        else:
            system = RESPONDER_SYSTEM_PROMPT

        try:
            raw = await self._gpt.complete(
                [
                    GptMessage(role="system", text=system),
                    GptMessage(role="user", text=user_prompt),
                ],
                temperature=self._cfg.responder_temperature,
                max_tokens=self._cfg.max_tokens,
            )
        except Exception as exc:
            log.warning(
                "responder.llm_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return _safe_fallback(cls.name)

        # 5) Постпроцессинг.
        cleaned = sanitize_response(raw)
        if not cleaned:
            log.warning("responder.empty_after_sanitize", raw_len=len(raw))
            return _safe_fallback(cls.name)
        log.info("responder.llm_ok", chars=len(cleaned))

        # 6) Кэш.
        if manager_context is None:
            await self._cache.set(
                complex_id=complex_info.id,
                theme=cls.theme,
                text=original_text,
                response=cleaned,
            )

        return cleaned
