"""Классификатор сообщений.

Реализации:
* `StubClassifier`      — keyword-эвристики; самостоятельный классификатор + fallback для LLM.
* `LlmClassifier`       — YandexGPT с JSON-промтом из ТЗ §5.2.
* `SafetyNetClassifier` — обёртка: после primary принудительно поднимает character
  до AGGRESSION/PROVOCATION, если в тексте жёсткие маркеры (LLM может ошибиться, мат — нет).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError

from balt_dom_bot.config import YandexGptConfig
from balt_dom_bot.log import get_logger
from balt_dom_bot.models import AddressedTo, Character, Classification, Theme, Urgency
from balt_dom_bot.prompts.classifier import CLASSIFIER_SYSTEM_PROMPT, build_user_message
from balt_dom_bot.services.yandex_gpt import GptMessage, YandexGptClient

if TYPE_CHECKING:
    from balt_dom_bot.storage.prompts_repo import PromptProvider

log = get_logger(__name__)


class Classifier(Protocol):
    async def classify(
        self, *,
        text: str,
        author_name: str | None = None,
        chat_context: list | None = None,
        reply_to_bot: bool = False,
        linked_text: str | None = None,
        linked_sender_name: str | None = None,
        linked_type: str | None = None,
    ) -> Classification: ...


# --- словари эвристик ---------------------------------------------------------

# Прямой мат и личные оскорбления (адресованы конкретному лицу/команде УК).
# При срабатывании → AGGRESSION → silent + эскалация.
_PROFANITY = (
    # Мат
    "сук", "бляд", "пизд", "хуй", "хуе", "ебан", "ебат", "пидор",
    # Прямые оскорбления (без мата)
    "мудак", "урод", "идиот", "дебил", "тупой", "тупые", "тупиц",
    "кретин", "придурок", "дурак", "болван", "недоумок",
    "сволоч", "подонок", "подонк", "мраз", "тварь", "гнид", "падл",
    "ублюд", "ничтожеств", "скотин", "отброс",
)

# Прямые маркеры агрессии: посыл/прямая инвектива.
_AGGRESSION_MARKERS = (
    "заткнись", "пошёл ты", "пошел ты", "идите вы",
    "пошли вы", "иди ты", "да пошли", "идите на",
)

# Бездоказательные обвинения и угрозы — ПРОВОКАЦИЯ.
# Это попытка дискредитировать УК или вызвать скандал в чате без фактов.
_PROVOCATION_MARKERS = (
    # Обвинения в преступной деятельности (без фактов в сообщении)
    "вориш", "жулик", "мошенник", "мошенн", "обкрад", "обкрадыв",
    "разворуют", "разворуете", "разворов", "обираете", "обдираете",
    "мафи", "банд",  # «мафия из УК», «банда жулья»
    "крысы", "крыс ", "крысят",  # пейоративы
    # Прямые угрозы санкциями (без юридической базы)
    "посадить бы", "посадить вас", "в тюрьм", "за решётк", "за решетк",
    "ответите за", "достанем вас", "достанем тебя",
    # Pejorative-обвинения (эмоциональные, без аргументов)
    "позор", "позорищ", "мерзк", "отвратит", "омерзит", "гнусн",
    "сборище", "шайка",
    # «вас всех уволить», обобщённые претензии без сути
    "вас всех уволить", "разогнать вас", "выгнать всех",
)

_EMERGENCY = ("потоп", "затопил", "затопле", "горит", "пожар", "авари", "прорыв", "затопл")
_TECH = ("лифт", "не работает", "сломал", "сломан", "сигнализаци", "домофон")
_IMPROVEMENT = ("уборк", "грязно", "мусор", "газон", "детск", "площадк", "озеленен")
_SECURITY = ("охран", "посторон", "чужая машина", "чужой автомобиль", "проникнов")
_INFO = ("режим работы", "график", "часы работы", "телефон", "контакт", "когда откро")
_LEGAL = ("осс", "общее собрание", "норматив", "закон", "тариф", "полномочи")
_UTILITY = ("горяч", "холодн", "вода", "электричест", "свет", "отоплен", "тепло")

_HIGH_URGENCY = ("срочно", "опасно", "немедленно", "помогите", "горит", "потоп", "ребёнок", "ребенок")

_COMPLAINT_STRONG = (
    "уже третий раз", "уже третий день", "уже неделю", "уже месяц",
    "сколько можно", "опять", "доколе", "когда уже", "наконец-то",
)
_COMPLAINT_MILD = ("плохо", "медленно", "долго", "не убирают", "не отвечают")
_REPORT = ("смотрите", "вот", "видео", "фото", "выкладываю")

# Маркеры явной адресации к УК. Это узкий набор: слова, которые встречаются
# почти ИСКЛЮЧИТЕЛЬНО в контексте обращения к управляющей компании.
# НАМЕРЕННО НЕ ВКЛЮЧЕНЫ слова бытового контекста («парадная», «подъезд»,
# «квартира», «соседи», «дом», «крыша» и т.п.) — они есть в любом ЖК-чате,
# в том числе в обычной болтовне жильцов.
# Используется только в fallback-режиме (когда LLM не вернул addressed_to).
_UC_ADDRESSING_MARKERS = (
    "управляющ", "ук ", " ук,", " ук.", " ук!", " ук?", "ук:",
    "балтийск", "приморск", "приморский друг",
    "помогите", "помогит", "спасите",
    "подскаж", "обращение", "обращаюсь",
    "когда восстанов", "когда почин", "когда уберут",
    "когда отремонт", "когда включат",
)


def is_off_topic(
    text: str,
    theme: Theme,
    character: Character,
    addressed_to: "AddressedTo | None" = None,
) -> bool:
    """Эвристика: сообщение не адресовано УК.

    Главный сигнал — addressed_to (определяется LLM). Если LLM явно сказал
    RESIDENTS или UNCLEAR — это off-topic (бот молчит). Если UC — НЕ off-topic.

    Если addressed_to не определено (None — например при regex-fallback или
    в старых тестах), используем старую heuristic: theme=OTHER + нет UC-маркеров.

    Агрессия и провокация ВСЕГДА обрабатываются (эскалация управляющему),
    независимо от адресации.
    """
    # Override: агрессию/провокацию обрабатываем всегда.
    if character in (Character.AGGRESSION, Character.PROVOCATION):
        return False

    # Если LLM явно классифицировал адресацию — доверяем ему.
    if addressed_to is not None:
        if addressed_to == AddressedTo.UC:
            return False
        # RESIDENTS или UNCLEAR — off-topic. UNCLEAR означает «лучше промолчать
        # чем неуместно ответить». Это безопасный default.
        return True

    # Fallback (regex / нет addressed_to): тема явная → не off-topic.
    if theme != Theme.OTHER:
        return False
    text_lc = text.lower()
    if _has_any(text_lc, _UC_ADDRESSING_MARKERS):
        return False
    return True


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def _detect_aggression_marker(text_lc: str) -> Character | None:
    """Жёсткая проверка: маркеры, которые ОБЯЗАНЫ блокировать публичный ответ."""
    if _has_any(text_lc, _PROFANITY) or _has_any(text_lc, _AGGRESSION_MARKERS):
        return Character.AGGRESSION
    if _has_any(text_lc, _PROVOCATION_MARKERS):
        return Character.PROVOCATION
    return None


def _detect_character(text_lc: str) -> Character:
    forced = _detect_aggression_marker(text_lc)
    if forced is not None:
        return forced
    if _has_any(text_lc, _COMPLAINT_STRONG):
        return Character.COMPLAINT_STRONG
    if _has_any(text_lc, _COMPLAINT_MILD):
        return Character.COMPLAINT_MILD
    if _has_any(text_lc, _REPORT):
        return Character.REPORT
    return Character.QUESTION


def _detect_theme(text_lc: str) -> Theme:
    if _has_any(text_lc, _EMERGENCY):
        return Theme.EMERGENCY
    if _has_any(text_lc, _UTILITY):
        return Theme.UTILITY
    if _has_any(text_lc, _TECH):
        return Theme.TECH_FAULT
    if _has_any(text_lc, _SECURITY):
        return Theme.SECURITY
    if _has_any(text_lc, _IMPROVEMENT):
        return Theme.IMPROVEMENT
    if _has_any(text_lc, _LEGAL):
        return Theme.LEGAL_ORG
    if _has_any(text_lc, _INFO):
        return Theme.INFO_REQUEST
    return Theme.OTHER


def _detect_urgency(text_lc: str, theme: Theme) -> Urgency:
    if theme == Theme.EMERGENCY or _has_any(text_lc, _HIGH_URGENCY):
        return Urgency.HIGH
    if theme in {Theme.TECH_FAULT, Theme.UTILITY, Theme.SECURITY}:
        return Urgency.MEDIUM
    return Urgency.LOW


def _summary(text: str, max_words: int = 15) -> str:
    words = re.findall(r"\S+", text)
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]) + "…"


# --- Stub ---------------------------------------------------------------------


def _detect_addressed_to_fallback(
    text_lc: str, theme: Theme, character: Character,
) -> AddressedTo | None:
    """Грубая heuristic для regex-fallback (когда LLM упал).

    Возвращает None если не уверены — pipeline тогда сам решит через
    is_off_topic с старой логикой. Возвращает явное значение только
    когда сигнал однозначный.
    """
    # Агрессия/провокация всегда обрабатываем (= UC).
    if character in (Character.AGGRESSION, Character.PROVOCATION):
        return AddressedTo.UC
    # Прямые UC-маркеры.
    if _has_any(text_lc, _UC_ADDRESSING_MARKERS):
        return AddressedTo.UC
    # Тематические сообщения (вода, лифт, ремонт) — UC независимо от слов.
    if theme in (Theme.UTILITY, Theme.TECH_FAULT, Theme.EMERGENCY):
        return AddressedTo.UC
    # Короткие бессмысленные сообщения — троллинг (unclear).
    stripped = text_lc.strip().strip("?!.,:;… )(")
    if len(stripped) <= 6:
        return AddressedTo.UNCLEAR
    # Растянутые крики ("АЛОООО", "Эээээй") — повтор гласной 4+ раз.
    for vowel in "аоеуыэияёюй":
        if vowel * 4 in text_lc:
            return AddressedTo.UNCLEAR
    # Иначе не уверены.
    return None


class StubClassifier:
    """Keyword-эвристики; не зависит от LLM."""

    async def classify(
        self, *,
        text: str,
        author_name: str | None = None,
        chat_context: list | None = None,
    ) -> Classification:
        # chat_context для эвристик не используется — они работают только
        # на текущем тексте. Параметр принимается для совместимости интерфейса.
        text_lc = text.lower()
        character = _detect_character(text_lc)
        theme = _detect_theme(text_lc)
        urgency = _detect_urgency(text_lc, theme)
        confidence = 0.9 if (theme != Theme.OTHER or character != Character.QUESTION) else 0.4
        addressed_to = _detect_addressed_to_fallback(text_lc, theme, character)

        result = Classification(
            theme=theme,
            urgency=urgency,
            character=character,
            name=author_name,
            summary=_summary(text),
            confidence=confidence,
            addressed_to=addressed_to,
        )
        log.debug("classifier.stub", **result.model_dump(mode="json"))
        return result


# --- LLM ----------------------------------------------------------------------

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict | None:
    """Достаёт первый валидный JSON-объект из текста LLM (устойчиво к markdown)."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        nl = raw.find("\n")
        if nl != -1 and raw[:nl].strip().lower() in {"json", ""}:
            raw = raw[nl + 1 :]
    match = _JSON_OBJECT_RE.search(raw)
    if match is None:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


class LlmClassifier:
    """Классификатор поверх YandexGPT. При невалидном ответе делегирует fallback.

    Системный промт берётся из `PromptProvider` (БД с дефолтом из кода) —
    GUI может править без передеплоя.
    """

    PROMPT_NAME = "classifier_system"

    def __init__(
        self,
        gpt: YandexGptClient,
        gpt_cfg: YandexGptConfig,
        *,
        fallback: Classifier | None = None,
        prompt_provider: "PromptProvider | None" = None,
    ):
        self._gpt = gpt
        self._cfg = gpt_cfg
        self._fallback = fallback or StubClassifier()
        self._prompts = prompt_provider

    async def classify(
        self, *,
        text: str,
        author_name: str | None = None,
        chat_context: list | None = None,
        reply_to_bot: bool = False,
        linked_text: str | None = None,
        linked_sender_name: str | None = None,
        linked_type: str | None = None,
    ) -> Classification:
        if self._prompts is not None:
            system = await self._prompts.get(self.PROMPT_NAME, CLASSIFIER_SYSTEM_PROMPT)
        else:
            system = CLASSIFIER_SYSTEM_PROMPT
        try:
            raw = await self._gpt.complete(
                [
                    GptMessage(role="system", text=system),
                    GptMessage(
                        role="user",
                        text=build_user_message(
                            text, author_name, chat_context,
                            reply_to_bot=reply_to_bot,
                            linked_text=linked_text,
                            linked_sender_name=linked_sender_name,
                            linked_type=linked_type,
                        ),
                    ),
                ],
                temperature=self._cfg.classifier_temperature,
                max_tokens=400,
            )
        except Exception as exc:
            log.warning(
                "classifier.llm_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return await self._fallback.classify(
                text=text, author_name=author_name, chat_context=chat_context,
            )

        data = _extract_json(raw)
        if data is None:
            log.warning("classifier.json_extract_failed", raw_preview=raw[:200])
            return await self._fallback.classify(
                text=text, author_name=author_name, chat_context=chat_context,
            )

        if data.get("name") == "":
            data["name"] = None
        if not data.get("name") and author_name:
            data["name"] = author_name

        # LLM иногда путает поля и пишет character-значение в theme
        # (например "PROVOCATION" или "AGGRESSION" вместо темы).
        # Спасаем классификацию: сбрасываем theme в OTHER, character остаётся.
        _valid_themes = {t.value for t in Theme}
        if data.get("theme") not in _valid_themes:
            log.warning(
                "classifier.theme_salvaged",
                bad_theme=data.get("theme"),
                character=data.get("character"),
            )
            data["theme"] = Theme.OTHER.value

        try:
            cls = Classification.model_validate(data)
        except ValidationError as exc:
            log.warning("classifier.validation_failed", error=str(exc), data=data)
            return await self._fallback.classify(
                text=text, author_name=author_name, chat_context=chat_context,
            )

        log.info("classifier.llm_ok", **cls.model_dump(mode="json"))
        return cls


# --- Safety-net ---------------------------------------------------------------


class SafetyNetClassifier:
    """Поднимает character до AGGRESSION/PROVOCATION при наличии жёстких маркеров."""

    def __init__(self, primary: Classifier):
        self._primary = primary

    async def classify(
        self, *,
        text: str,
        author_name: str | None = None,
        chat_context: list | None = None,
        reply_to_bot: bool = False,
        linked_text: str | None = None,
        linked_sender_name: str | None = None,
        linked_type: str | None = None,
    ) -> Classification:
        result = await self._primary.classify(
            text=text, author_name=author_name, chat_context=chat_context,
            reply_to_bot=reply_to_bot, linked_text=linked_text,
            linked_sender_name=linked_sender_name, linked_type=linked_type,
        )
        forced = _detect_aggression_marker(text.lower())
        if forced is None or result.character in {Character.AGGRESSION, Character.PROVOCATION}:
            return result
        log.warning(
            "classifier.safety_net_override",
            from_=result.character.value,
            to=forced.value,
        )
        return result.model_copy(
            update={"character": forced, "confidence": max(result.confidence, 0.95)}
        )
