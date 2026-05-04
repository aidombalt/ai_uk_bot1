"""Пост-обработка LLM-ответа: фильтр запрещённых формулировок (ТЗ §3.3).

LLM просим в системном промте не использовать «мы/наша УК», но это не гарантия —
здесь жёсткая страховка перед отправкой жильцу.
"""

from __future__ import annotations

import re

from balt_dom_bot.log import get_logger

log = get_logger(__name__)

# Все падежные формы притяжательного «наш»: наш, наша, наше, наши, нашего,
# нашему, нашим, наших, нашей, нашею, нашими.
_NASH = r"наш(?:его|ему|ими|их|им|ей|ею|а|е|и|у)?"

# Все падежные формы «мы/нас/нам/нами»: «мы», «нас», «нам», «нами».
# Каждое слово обрабатывается отдельно регексами ниже.

_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    # «нашей УК» / «наша компания» / «нашими специалистами» → нейтральная форма
    (re.compile(rf"\b{_NASH}\s+(УК|управля\w+\s+компани\w+|компани\w+|специалист\w+|сотрудник\w+)\b",
                re.IGNORECASE),
     r"\1"),  # выкидываем притяжательное, оставляем существительное
    # «у нас в ...» → «в ...»
    (re.compile(r"\bу\s+нас\s+в\b", re.IGNORECASE), "в"),
    # отдельно «у нас» (без «в») — уберём целиком
    (re.compile(r"\bу\s+нас\b", re.IGNORECASE), ""),
    # «мы + глагол» → «специалистами + глагол» — слишком тонко;
    # делаем мягко: «мы » → «специалисты » (LLM редко начинает с «мы», обычно «Мы»)
    (re.compile(r"\bмы\s+", re.IGNORECASE), "специалисты "),
    # «нам/нас/нами» в косвенных падежах — убираем
    (re.compile(r"\b(нам|нас|нами)\s+", re.IGNORECASE), ""),
    # любые оставшиеся формы «наш/наша/...» в общем виде
    (re.compile(rf"\b{_NASH}\b", re.IGNORECASE), ""),
)

# Префиксы, которые целиком вырезаются.
_BANNED_PREFIXES = (
    "конечно!", "конечно,", "конечно.",
    "разумеется,", "безусловно,",
    "спасибо за вопрос", "спасибо за ваш вопрос",
)

_MAX_SENTENCES = 6  # запас сверху от ТЗ-рекомендованных 2–4

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE = re.compile(r"\s+")
# Большая буква в начале каждого предложения (после . ! ? и пробела).
_AFTER_SENT = re.compile(r"([.!?]\s+)([а-яёa-z])")


def _capitalize_sentences(text: str) -> str:
    """Восстанавливает заглавные после точки/восклицания/вопроса."""
    if not text:
        return text
    if text[0].islower():
        text = text[0].upper() + text[1:]
    return _AFTER_SENT.sub(lambda m: m.group(1) + m.group(2).upper(), text)


def sanitize_response(text: str) -> str:
    """Возвращает безопасный для отправки в чат вариант ответа."""
    if not text:
        return text
    cleaned = text.strip().strip("«»\"'`")

    # 1) Срезаем banned-префиксы.
    lower = cleaned.lower()
    for pref in _BANNED_PREFIXES:
        if lower.startswith(pref):
            cleaned = cleaned[len(pref):].lstrip(" ,.;:—-")
            lower = cleaned.lower()

    # 2) Замены запрещённых форм.
    for pattern, replacement in _REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)

    # 3) Чистим двойные пробелы и пробелы перед знаками.
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)

    # 4) Капитализация в начале и после точек.
    cleaned = _capitalize_sentences(cleaned)

    # 5) Обрезаем до _MAX_SENTENCES.
    sentences = _SENTENCE_SPLIT.split(cleaned)
    if len(sentences) > _MAX_SENTENCES:
        log.warning("responder.too_long", sentences=len(sentences), limit=_MAX_SENTENCES)
        cleaned = " ".join(sentences[:_MAX_SENTENCES])

    return cleaned
