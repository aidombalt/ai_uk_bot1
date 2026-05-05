"""Smoke-тесты новой логики chat-mode.

Проверяем:
1. _should_auto_engage: правильно фильтрует только COMPLAINT_STRONG → UC.
2. Pipeline активирует chat-mode для нелистиста при COMPLAINT_STRONG.
3. Pipeline активирует chat-mode при reply_to_bot + есть история.
4. Pipeline НЕ активирует chat-mode для простого вопроса без истории.
5. После обычного ответа история сидируется в chat_messages.
6. Резюме отправляется управляющему на 3-м обмене (len(prior_history)==4).
7. Нет per-message уведомления в чат «Обращения» при chat-mode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from balt_dom_bot.config import AppConfig, PipelineConfig, YandexGptConfig
from balt_dom_bot.models import (
    AddressedTo,
    Character,
    Classification,
    IncomingMessage,
    Theme,
    Urgency,
)
from balt_dom_bot.services.pipeline import (
    Pipeline,
    ReplySender,
    _should_auto_engage,
)
from balt_dom_bot.storage.chat_mode_repo import ChatMessage
from balt_dom_bot.storage.complexes_repo import ComplexRow

# ---------------------------------------------------------------------------
# Константы и фабрики
# ---------------------------------------------------------------------------

CHAT_ID = -100_000_042
USER_ID = 7
NOW = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)


def _make_cfg() -> AppConfig:
    return AppConfig(
        yandex_gpt=YandexGptConfig(folder_id="STUB_FID", api_key="STUB_KEY"),
        pipeline=PipelineConfig(
            confidence_threshold=0.6,
            silent_characters=["AGGRESSION", "PROVOCATION"],
            always_escalate_themes=["EMERGENCY", "LEGAL_ORG"],
        ),
    )


def _make_complex_row(**kwargs: Any) -> ComplexRow:
    defaults = dict(
        id="test-chat-1",
        name="Тест ЖК",
        address="ул. Тестовая, 1",
        chat_id=CHAT_ID,
        manager_chat_id=99999,
        active=True,
        updated_at="2026-01-01",
        escalation_chat_id=88888,
        escalate_to_manager=True,
        escalate_to_chat=True,
        manager_user_id=None,
        auto_delete_aggression=False,
        strikes_for_ban=3,
        trolling_strikes_for_ban=6,
        reply_mode="normal",
        holiday_message=None,
        daily_replies_limit=5,
        daily_window_hours=6,
        chat_mode_enabled=True,  # включаем chat-mode для всех тестов
    )
    defaults.update(kwargs)
    return ComplexRow(**defaults)


def _make_msg(
    text: str,
    user_id: int = USER_ID,
    reply_to_bot: bool = False,
) -> IncomingMessage:
    return IncomingMessage(
        chat_id=CHAT_ID,
        message_id=f"mid.{abs(hash(text)):016x}",
        user_id=user_id,
        user_name="Тест Жилец",
        text=text,
        received_at=NOW,
        reply_to_bot=reply_to_bot,
    )


def _make_cls(
    *,
    character: Character,
    addressed_to: AddressedTo = AddressedTo.UC,
    theme: Theme = Theme.IMPROVEMENT,
    urgency: Urgency = Urgency.LOW,
    confidence: float = 0.9,
) -> Classification:
    return Classification(
        theme=theme,
        urgency=urgency,
        character=character,
        name="Тест Жилец",
        summary="тест",
        confidence=confidence,
        addressed_to=addressed_to,
    )


# ---------------------------------------------------------------------------
# Фейковые зависимости
# ---------------------------------------------------------------------------


class _FakeClassifier:
    def __init__(self, cls: Classification) -> None:
        self._cls = cls

    async def classify(self, *, text: str, **_: Any) -> Classification:
        return self._cls


class _FakeResponder:
    def __init__(self, reply: str | None = "Обычный ответ") -> None:
        self._reply = reply
        self.called = False

    async def respond(self, *, classification: Any, original_text: str, complex_info: Any) -> str | None:
        self.called = True
        return self._reply


class _FakeReplySender(ReplySender):
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def send_reply(self, *, chat_id: int, text: str, reply_to_mid: str | None) -> None:
        self.replies.append(text)


class _FakeComplexesRepo:
    def __init__(self, row: ComplexRow) -> None:
        self._row = row

    async def find_by_chat(self, chat_id: int) -> ComplexRow | None:
        return self._row if chat_id == self._row.chat_id else None


class _FakeEscalator:
    def __init__(self) -> None:
        self.escalated: list[str] = []

    async def escalate(self, *, incoming: Any, complex_info: Any, decision: Any, **_: Any) -> None:
        self.escalated.append(decision.escalation_reason or "")


class _FakeChatModeRepo:
    """Имитация ChatModeRepo с настраиваемыми историей и whitelist."""

    def __init__(
        self,
        whitelisted: bool = False,
        history: list[ChatMessage] | None = None,
    ) -> None:
        self._whitelisted = whitelisted
        self._history: list[ChatMessage] = history or []
        self.appended: list[tuple[str, str]] = []

    async def is_whitelisted(self, *, chat_id: int, user_id: int) -> bool:
        return self._whitelisted

    async def get_history(self, *, chat_id: int, user_id: int) -> list[ChatMessage]:
        return list(self._history)

    async def append_message(self, *, chat_id: int, user_id: int, role: str, text: str) -> None:
        self.appended.append((role, text))


class _FakeChatResponder:
    """Имитация ChatResponder."""

    def __init__(self, reply: str = "Ответ в режиме диалога") -> None:
        self._reply = reply
        self.called = False
        self.received_history: list[ChatMessage] = []

    async def respond(
        self, *,
        text: str,
        history: list[ChatMessage],
        complex_info: Any,
        classification: Any = None,
        system_prompt: str | None = None,
    ) -> str:
        self.called = True
        self.received_history = list(history)
        return self._reply


class _FakeNotifier:
    """Имитация MaxBotEscalationSender для проверки уведомлений."""

    def __init__(self) -> None:
        self.notifications: list[str] = []

    async def send_notification_to_chat(self, *, chat_id: int, text: str) -> None:
        self.notifications.append(text)


def _build_pipeline(
    cls: Classification,
    complex_row: ComplexRow,
    *,
    chat_mode_repo: _FakeChatModeRepo | None = None,
    chat_responder: _FakeChatResponder | None = None,
    responder: Any = None,
    notifier: _FakeNotifier | None = None,
) -> tuple[Pipeline, _FakeReplySender, _FakeEscalator]:
    cfg = _make_cfg()
    reply_sender = _FakeReplySender()
    escalator = _FakeEscalator()
    pipeline = Pipeline(
        cfg=cfg,
        classifier=_FakeClassifier(cls),
        responder=responder or _FakeResponder(),
        escalator=escalator,
        reply_sender=reply_sender,
        complexes=_FakeComplexesRepo(complex_row),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
        notifier=notifier,
    )
    return pipeline, reply_sender, escalator


# ---------------------------------------------------------------------------
# Тесты _should_auto_engage (unit)
# ---------------------------------------------------------------------------


def test_auto_engage_complaint_strong_uc() -> None:
    cls = _make_cls(character=Character.COMPLAINT_STRONG, addressed_to=AddressedTo.UC)
    assert _should_auto_engage(cls) is True


def test_auto_engage_complaint_strong_none_addressed() -> None:
    """addressed_to=None (fallback от StubClassifier) → benefit of the doubt."""
    cls = _make_cls(character=Character.COMPLAINT_STRONG, addressed_to=AddressedTo.UC)
    cls2 = cls.model_copy(update={"addressed_to": None})
    assert _should_auto_engage(cls2) is True


def test_auto_engage_rejects_residents() -> None:
    cls = _make_cls(character=Character.COMPLAINT_STRONG, addressed_to=AddressedTo.RESIDENTS)
    assert _should_auto_engage(cls) is False


def test_auto_engage_rejects_question() -> None:
    cls = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UC)
    assert _should_auto_engage(cls) is False


def test_auto_engage_rejects_complaint_mild() -> None:
    cls = _make_cls(character=Character.COMPLAINT_MILD, addressed_to=AddressedTo.UC)
    assert _should_auto_engage(cls) is False


def test_auto_engage_rejects_aggression() -> None:
    cls = _make_cls(character=Character.AGGRESSION, addressed_to=AddressedTo.UC)
    assert _should_auto_engage(cls) is False


def test_auto_engage_rejects_high_urgency() -> None:
    cls = _make_cls(character=Character.COMPLAINT_STRONG, urgency=Urgency.HIGH)
    assert _should_auto_engage(cls) is False


def test_auto_engage_rejects_emergency_theme() -> None:
    cls = _make_cls(character=Character.COMPLAINT_STRONG, theme=Theme.EMERGENCY)
    assert _should_auto_engage(cls) is False


# ---------------------------------------------------------------------------
# Тесты pipeline: активация chat-mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_mode_activates_for_complaint_strong() -> None:
    """Нелистиста с COMPLAINT_STRONG → chat-mode должен включиться."""
    chat_mode_repo = _FakeChatModeRepo(whitelisted=False, history=[])
    chat_responder = _FakeChatResponder()
    cls = _make_cls(character=Character.COMPLAINT_STRONG, addressed_to=AddressedTo.UC)
    pipeline, reply_sender, _ = _build_pipeline(
        cls, _make_complex_row(),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
    )
    msg = _make_msg("Уже третий раз не работает домофон")
    await pipeline.handle(msg)

    assert chat_responder.called, "ChatResponder должен быть вызван для COMPLAINT_STRONG"
    assert reply_sender.replies, "Ответ должен быть отправлен в чат"
    assert reply_sender.replies[0] == "Ответ в режиме диалога"


@pytest.mark.asyncio
async def test_chat_mode_activates_on_reply_to_bot_with_history() -> None:
    """reply_to_bot=True + есть история → продолжение диалога через chat-mode."""
    history = [
        ChatMessage(role="user", text="Грязно в подъезде"),
        ChatMessage(role="assistant", text="Понял, уточните подъезд"),
    ]
    chat_mode_repo = _FakeChatModeRepo(whitelisted=False, history=history)
    chat_responder = _FakeChatResponder()
    cls = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UC)
    pipeline, reply_sender, _ = _build_pipeline(
        cls, _make_complex_row(),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
    )
    msg = _make_msg("Первый подъезд", reply_to_bot=True)
    await pipeline.handle(msg)

    assert chat_responder.called, "Должен использоваться chat-mode при reply_to_bot + история"
    assert chat_responder.received_history == history


@pytest.mark.asyncio
async def test_chat_mode_not_activated_for_simple_question() -> None:
    """Обычный вопрос без истории → не chat-mode, обычный responder."""
    chat_mode_repo = _FakeChatModeRepo(whitelisted=False, history=[])
    chat_responder = _FakeChatResponder()
    normal_responder = _FakeResponder(reply="FAQ ответ")
    cls = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UC)
    pipeline, reply_sender, _ = _build_pipeline(
        cls, _make_complex_row(),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
        responder=normal_responder,
    )
    msg = _make_msg("Когда уберут мусор?")
    await pipeline.handle(msg)

    assert not chat_responder.called, "Простой вопрос должен идти через обычный responder"
    assert normal_responder.called
    assert reply_sender.replies[0] == "FAQ ответ"


@pytest.mark.asyncio
async def test_chat_mode_not_activated_for_reply_without_history() -> None:
    """reply_to_bot=True, но истории нет → chat-mode не активируется."""
    chat_mode_repo = _FakeChatModeRepo(whitelisted=False, history=[])
    chat_responder = _FakeChatResponder()
    normal_responder = _FakeResponder(reply="Обычный ответ")
    cls = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UC)
    pipeline, _, _ = _build_pipeline(
        cls, _make_complex_row(),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
        responder=normal_responder,
    )
    msg = _make_msg("Хорошо", reply_to_bot=True)
    await pipeline.handle(msg)

    assert not chat_responder.called, "Без истории reply_to_bot не активирует chat-mode"


@pytest.mark.asyncio
async def test_chat_mode_not_activated_when_disabled() -> None:
    """chat_mode_enabled=False → chat-mode не работает даже для COMPLAINT_STRONG."""
    chat_mode_repo = _FakeChatModeRepo(whitelisted=False, history=[])
    chat_responder = _FakeChatResponder()
    normal_responder = _FakeResponder(reply="Обычный ответ")
    cls = _make_cls(character=Character.COMPLAINT_STRONG, addressed_to=AddressedTo.UC)
    pipeline, _, _ = _build_pipeline(
        cls, _make_complex_row(chat_mode_enabled=False),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
        responder=normal_responder,
    )
    msg = _make_msg("Уже третий раз не работает лифт")
    await pipeline.handle(msg)

    assert not chat_responder.called, "При chat_mode_enabled=False не должен вызываться ChatResponder"


@pytest.mark.asyncio
async def test_whitelist_user_always_gets_chat_mode() -> None:
    """Whitelisted юзер всегда получает chat-mode, даже для простого вопроса."""
    chat_mode_repo = _FakeChatModeRepo(whitelisted=True, history=[])
    chat_responder = _FakeChatResponder()
    cls = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UC)
    pipeline, _, _ = _build_pipeline(
        cls, _make_complex_row(),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
    )
    msg = _make_msg("Когда откроется детская площадка?")
    await pipeline.handle(msg)

    assert chat_responder.called, "Whitelist юзер должен всегда использовать chat-mode"


# ---------------------------------------------------------------------------
# Тесты: сидирование истории
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_seeded_after_normal_reply() -> None:
    """После обычного ответа бота история должна быть сохранена для future replay."""
    chat_mode_repo = _FakeChatModeRepo(whitelisted=False, history=[])
    chat_responder = _FakeChatResponder()
    normal_responder = _FakeResponder(reply="Обычный ответ")
    cls = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UC)
    pipeline, _, _ = _build_pipeline(
        cls, _make_complex_row(),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
        responder=normal_responder,
    )
    msg = _make_msg("Когда уберут мусор?")
    await pipeline.handle(msg)

    assert len(chat_mode_repo.appended) == 2, "Должны быть сохранены user + assistant"
    assert chat_mode_repo.appended[0] == ("user", "Когда уберут мусор?")
    assert chat_mode_repo.appended[1][0] == "assistant"


@pytest.mark.asyncio
async def test_history_not_seeded_when_chat_mode_disabled() -> None:
    """При chat_mode_enabled=False история не должна сидироваться."""
    chat_mode_repo = _FakeChatModeRepo(whitelisted=False, history=[])
    normal_responder = _FakeResponder(reply="Обычный ответ")
    cls = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UC)
    pipeline, _, _ = _build_pipeline(
        cls, _make_complex_row(chat_mode_enabled=False),
        chat_mode_repo=chat_mode_repo,
        responder=normal_responder,
    )
    msg = _make_msg("Когда уберут мусор?")
    await pipeline.handle(msg)

    assert len(chat_mode_repo.appended) == 0, "При chat_mode_enabled=False история не сохраняется"


# ---------------------------------------------------------------------------
# Тесты: резюме управляющему
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_sent_at_third_exchange() -> None:
    """На 3-м обмене (prior_history len=4) отправляется резюме управляющему."""
    history_before = [
        ChatMessage(role="user", text="Лифт сломан"),
        ChatMessage(role="assistant", text="Понял, номер лифта?"),
        ChatMessage(role="user", text="Лифт №2"),
        ChatMessage(role="assistant", text="Принято, когда заметили?"),
    ]
    chat_mode_repo = _FakeChatModeRepo(whitelisted=True, history=history_before)
    chat_responder = _FakeChatResponder(reply="Информация принята.")
    notifier = _FakeNotifier()
    cls = _make_cls(character=Character.COMPLAINT_STRONG, addressed_to=AddressedTo.UC)
    pipeline, _, _ = _build_pipeline(
        cls, _make_complex_row(escalate_to_chat=True, escalation_chat_id=88888),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
        notifier=notifier,
    )
    msg = _make_msg("Сегодня утром заметил")
    await pipeline.handle(msg)

    assert len(notifier.notifications) == 1, "Должно быть ровно одно резюме"
    summary = notifier.notifications[0]
    assert "Диалог с жильцом" in summary
    assert "Обменов: 3" in summary
    assert "Сегодня утром заметил" in summary
    assert "Информация принята." in summary


@pytest.mark.asyncio
async def test_summary_not_sent_before_threshold() -> None:
    """До 3-го обмена (prior_history len<4) резюме не отправляется."""
    history_before = [
        ChatMessage(role="user", text="Лифт сломан"),
        ChatMessage(role="assistant", text="Понял, номер лифта?"),
    ]
    chat_mode_repo = _FakeChatModeRepo(whitelisted=True, history=history_before)
    chat_responder = _FakeChatResponder()
    notifier = _FakeNotifier()
    cls = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UC)
    pipeline, _, _ = _build_pipeline(
        cls, _make_complex_row(escalate_to_chat=True, escalation_chat_id=88888),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
        notifier=notifier,
    )
    msg = _make_msg("Лифт №2")
    await pipeline.handle(msg)

    assert len(notifier.notifications) == 0, "До 3-го обмена резюме не отправляется"


@pytest.mark.asyncio
async def test_no_per_message_notify_in_chat_mode() -> None:
    """В chat-mode per-message уведомление в чат «Обращения» не отправляется."""
    chat_mode_repo = _FakeChatModeRepo(whitelisted=True, history=[])
    chat_responder = _FakeChatResponder()
    notifier = _FakeNotifier()
    cls = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UC)
    pipeline, _, _ = _build_pipeline(
        cls, _make_complex_row(escalate_to_chat=True, escalation_chat_id=88888),
        chat_mode_repo=chat_mode_repo,
        chat_responder=chat_responder,
        notifier=notifier,
    )
    msg = _make_msg("Когда уберут мусор?")
    await pipeline.handle(msg)

    assert len(notifier.notifications) == 0, (
        "При chat-mode первые два обмена — без уведомлений (резюме придёт позже)"
    )
