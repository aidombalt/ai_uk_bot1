"""Domain-модели: результат классификации, контекст ЖК, payload эскалации."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Theme(StrEnum):
    EMERGENCY = "EMERGENCY"
    TECH_FAULT = "TECH_FAULT"
    IMPROVEMENT = "IMPROVEMENT"
    SECURITY = "SECURITY"
    INFO_REQUEST = "INFO_REQUEST"
    LEGAL_ORG = "LEGAL_ORG"
    UTILITY = "UTILITY"
    OTHER = "OTHER"


class Urgency(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Character(StrEnum):
    QUESTION = "QUESTION"
    COMPLAINT_MILD = "COMPLAINT_MILD"
    COMPLAINT_STRONG = "COMPLAINT_STRONG"
    AGGRESSION = "AGGRESSION"
    PROVOCATION = "PROVOCATION"
    REPORT = "REPORT"


class AddressedTo(StrEnum):
    """Семантика адресации сообщения. Главный признак для решения «отвечать
    или молчать», который должен определять LLM (не regex).

    * UC        — жилец обращается к управляющей компании, описывает проблему
                  инфраструктуры, ждёт реакции от УК.
    * RESIDENTS — общение между жильцами на бытовые темы, не требующие УК
                  (потерянные животные, поиск соседей, поздравления, обмен
                  мнениями про внешние события).
    * UNCLEAR   — невозможно однозначно определить. По умолчанию трактуется
                  как «не отвечать» — лучше промолчать, чем спамить.
    """

    UC = "uc"
    RESIDENTS = "residents"
    UNCLEAR = "unclear"


class Classification(BaseModel):
    """Структурированный результат классификатора (формат из ТЗ §5.2)."""

    theme: Theme
    urgency: Urgency
    character: Character
    name: str | None = None
    summary: str = Field(..., description="10–15 слов сути запроса")
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    # None = классификатор не определил (например regex-fallback).
    # Pipeline в этом случае использует старую heuristic из is_off_topic.
    # LLM-классификатор всегда заполняет это поле явно.
    addressed_to: AddressedTo | None = None


class ReplyMode(StrEnum):
    """Режим ответа жильцам в конкретном ЖК.

    * NORMAL — стандартная работа: FAQ + LLM ответы, эскалации, всё как обычно.
    * HOLIDAY — праздничный/отпускной режим: вместо FAQ-ответа отправляется
      шаблонный текст «мы временно работаем в особом режиме». Эскалации
      продолжают работать (управляющий получает уведомления).
    * OFF — бот не отвечает жильцам публично, но эскалирует только серьёзные
      случаи (HIGH_URGENCY, EMERGENCY, агрессия). Полезно для отпуска
      управляющего, когда чат не должен пустовать.
    """

    NORMAL = "normal"
    HOLIDAY = "holiday"
    OFF = "off"


DEFAULT_HOLIDAY_MESSAGE = (
    "Здравствуйте! В данный момент управляющая компания работает "
    "в особом режиме. Ваше обращение принято и будет рассмотрено "
    "в ближайший рабочий день. По срочным вопросам — телефон диспетчерской."
)


class ComplexInfo(BaseModel):
    """Подмножество AppConfig.ComplexConfig для передачи в prompt и pipeline."""

    id: str
    name: str
    address: str
    manager_chat_id: int
    escalation_chat_id: int | None = None
    escalate_to_manager: bool = True
    escalate_to_chat: bool = False
    manager_user_id: int | None = None
    auto_delete_aggression: bool = False
    strikes_for_ban: int = 3
    trolling_strikes_for_ban: int = 6
    reply_mode: ReplyMode = ReplyMode.NORMAL
    holiday_message: str | None = None
    daily_replies_limit: int = 5
    daily_window_hours: int = 6
    chat_mode_enabled: bool = False


class IncomingMessage(BaseModel):
    """Нормализованный вход в pipeline."""

    chat_id: int
    message_id: str
    user_id: int | None = None
    user_name: str | None = None
    text: str
    received_at: datetime
    # True если в сообщении явно упомянут бот (через @username или markup
    # user_mention). Используется для anti-trolling логики: если юзер
    # тегает бота, но классификатор считает обращение off-topic — это
    # признак намеренного троллинга, и со 2-го раза подряд мы удаляем.
    bot_mentioned: bool = False
    # Контекст реплая / форварда (из Max API msg.link).
    # reply_to_bot=True означает что жилец ответил именно на сообщение бота —
    # критично для addressed_to: агрессия в ответ боту = обращение к УК.
    reply_to_bot: bool = False
    linked_message_text: str | None = None   # текст цитируемого сообщения
    linked_message_type: str | None = None   # "reply" или "forward"
    linked_sender_name: str | None = None    # имя автора цитируемого сообщения


class PipelineDecision(BaseModel):
    """Что pipeline решил сделать с сообщением."""

    classification: Classification
    reply_text: str | None = None  # None = публичный ответ не нужен
    escalate: bool = False
    escalation_reason: Literal[
        "aggression",
        "provocation",
        "high_urgency",
        "always_escalate_theme",
        "low_confidence",
        "llm_error",
        "after_hours",
        "spam_drugs",
        "spam_crypto",
        "spam_earn",
        "spam_esoteric",
        "spam_ads",
        "spam_mass_mention",
        "spam_unknown",
        "fragment_troll",
    ] | None = None
