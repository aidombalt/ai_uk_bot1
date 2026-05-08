"""Проверяет полноту информации в обращении жильца.

Для заявок (TECH_FAULT, UTILITY, IMPROVEMENT, SECURITY) определяет,
указал ли житель ключевые детали: парадную/подъезд, квартиру или этаж.
Если детали отсутствуют — формирует уточняющий вопрос.

Дизайн:
  - Детектирование наличия деталей: чистые эвристики (regex), без LLM-вызова.
  - Текст уточняющего вопроса: настраивается через GUI → Промты
    (ключ ``completeness_clarification``).
  - Для QUESTION-сообщений срабатывает только если в тексте описана
    конкретная проблема («не работает», «сломан», «течёт» и т.п.), а не
    чисто информационный вопрос («как подключить?», «к кому обратиться?»).
  - Не активируется для: AGGRESSION, PROVOCATION, EMERGENCY HIGH_URGENCY,
    тем вне LOCATION_THEMES, сообщений где детали уже есть.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from balt_dom_bot.log import get_logger
from balt_dom_bot.models import Character, Classification, Theme, Urgency
from balt_dom_bot.prompts.completeness import DEFAULT_CLARIFICATION_QUESTION

if TYPE_CHECKING:
    from balt_dom_bot.storage.prompts_repo import PromptProvider

log = get_logger(__name__)

# Темы, для которых принципиально знать местоположение
_LOCATION_THEMES = frozenset({
    Theme.TECH_FAULT,
    Theme.UTILITY,
    Theme.IMPROVEMENT,
    Theme.SECURITY,
})

# Признаки наличия локационных деталей в тексте
# Парадная/подъезд
_ENTRANCE_RE = re.compile(
    r"(?:парадн\w*|подъезд\w*|п[- ]?д\.?|пд\.?)\s*[№#]?\s*\d+",
    re.IGNORECASE,
)
# Квартира
_APARTMENT_RE = re.compile(
    r"(?:квартир\w+|кварт\.?|кв\.?)\s*[№#]?\s*\d+",
    re.IGNORECASE,
)
# Этаж
_FLOOR_RE = re.compile(
    r"(?:\d+\s*[-–]?\s*й?\s*(?:этаж|эт\.?)|(?:этаж|эт\.?)\s*\d+)",
    re.IGNORECASE,
)

# Маркеры конкретной проблемы — срабатывают для QUESTION-сообщений,
# чтобы отличить «как подключить?» (инфо-запрос, уточнения не нужны)
# от «перестал работать» (заявка на ремонт, нужна локация).
_ISSUE_MARKERS: tuple[str, ...] = (
    "не работает", "не работал", "не работают",
    "сломал", "сломан", "сломалась", "сломались", "сломали",
    "перестал", "перестала", "перестали",
    "не открывает", "не открывается", "не закрывает", "не закрывается",
    "не включает", "не включается", "не функционирует",
    "пропала вода", "пропал свет", "нет воды", "нет тепла", "нет света",
    "течёт", "течет", "протекает", "течь", "затопило", "залило",
    "вышел из строя", "вышла из строя", "вышли из строя",
    "не откликается", "завис", "зависает", "зависла",
)

# Prompt name для GUI
PROMPT_NAME = "completeness_clarification"


@dataclass
class CompletenessResult:
    needs_clarification: bool
    missing: list[str] = field(default_factory=list)
    clarification_question: str = ""


def _has_location_details(text: str) -> bool:
    """True если в тексте уже есть детали местоположения."""
    return bool(
        _ENTRANCE_RE.search(text)
        or _APARTMENT_RE.search(text)
        or _FLOOR_RE.search(text)
    )


def _has_issue_marker(text_lc: str) -> bool:
    """True если QUESTION-сообщение описывает конкретную проблему (не просто вопрос)."""
    return any(m in text_lc for m in _ISSUE_MARKERS)


class CompletenessChecker:
    """Детектирует отсутствие ключевых деталей и генерирует уточняющий вопрос.

    Не делает LLM-вызовов: детектирование — regex, вопрос — настраиваемый
    шаблон (PromptProvider с дефолтом из кода).
    """

    def __init__(self, *, prompt_provider: "PromptProvider | None" = None):
        self._prompts = prompt_provider

    async def _get_question(self) -> str:
        if self._prompts is not None:
            return await self._prompts.get(PROMPT_NAME, DEFAULT_CLARIFICATION_QUESTION)
        return DEFAULT_CLARIFICATION_QUESTION

    async def check(self, text: str, cls: Classification) -> CompletenessResult:
        """Основная проверка полноты.

        Returns:
            CompletenessResult с флагом и готовым текстом вопроса (если нужен).
        """
        # Не применяем к: агрессии/провокации (уходят на модерацию)
        if cls.character in (Character.AGGRESSION, Character.PROVOCATION):
            return CompletenessResult(needs_clarification=False)

        # Не применяем к темам, где локация не нужна
        if cls.theme not in _LOCATION_THEMES:
            return CompletenessResult(needs_clarification=False)

        # EMERGENCY HIGH urgency: немедленная эскалация важнее уточнений
        if cls.theme == Theme.EMERGENCY or cls.urgency == Urgency.HIGH:
            return CompletenessResult(needs_clarification=False)

        text_lc = text.lower()

        # Для QUESTION: запрашиваем детали только если описана конкретная
        # проблема, а не чисто информационный вопрос
        if cls.character == Character.QUESTION:
            if not _has_issue_marker(text_lc):
                return CompletenessResult(needs_clarification=False)

        # Если детали уже есть — уточнять не нужно
        if _has_location_details(text):
            return CompletenessResult(needs_clarification=False)

        question = await self._get_question()
        log.debug(
            "completeness.clarification_needed",
            theme=cls.theme.value,
            character=cls.character.value,
        )
        return CompletenessResult(
            needs_clarification=True,
            missing=["парадная/подъезд", "квартира или этаж"],
            clarification_question=question,
        )
