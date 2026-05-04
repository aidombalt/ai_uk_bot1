"""Smoke-тесты pipeline: PROVOCATION + addressed_to=residents + incitement.

Ключевой баг: когда LLM классифицирует подстрекательство к смене УК как
addressed_to=residents (логично — сообщение адресовано жильцам как целевой
аудитории), pipeline раньше возвращал escalate=False и не удалял сообщение.

После фикса: PROVOCATION + is_incitement=True → escalate=True, независимо
от addressed_to.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
from balt_dom_bot.services.escalation import Escalator
from balt_dom_bot.services.pipeline import Pipeline, ReplySender
from balt_dom_bot.storage.complexes_repo import ComplexRow


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

CHAT_ID = -100_000_001
USER_ID = 42
NOW = datetime(2026, 5, 4, 13, 45, 0, tzinfo=timezone.utc)


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
        id="test-1",
        name="Тест ЖК",
        address="ул. Тестовая, 1",
        chat_id=CHAT_ID,
        manager_chat_id=99999,
        active=True,
        updated_at="2026-01-01",
        escalation_chat_id=None,
        escalate_to_manager=True,
        escalate_to_chat=False,
        manager_user_id=None,
        auto_delete_aggression=True,
        strikes_for_ban=3,
        trolling_strikes_for_ban=6,
        reply_mode="normal",
        holiday_message=None,
        daily_replies_limit=5,
        daily_window_hours=6,
        chat_mode_enabled=False,
    )
    defaults.update(kwargs)
    return ComplexRow(**defaults)


def _make_msg(text: str, user_id: int = USER_ID) -> IncomingMessage:
    return IncomingMessage(
        chat_id=CHAT_ID,
        message_id=f"mid.{abs(hash(text)):016x}",
        user_id=user_id,
        user_name="Лев",
        text=text,
        received_at=NOW,
    )


def _make_cls(
    *,
    character: Character,
    addressed_to: AddressedTo,
    theme: Theme = Theme.LEGAL_ORG,
    summary: str = "",
    confidence: float = 1.0,
) -> Classification:
    return Classification(
        theme=theme,
        urgency=Urgency.LOW,
        character=character,
        name="Лев",
        summary=summary,
        confidence=confidence,
        addressed_to=addressed_to,
    )


class _FakeClassifier:
    def __init__(self, cls: Classification) -> None:
        self._cls = cls

    async def classify(
        self, *, text: str, author_name: str | None = None,
        chat_context: list | None = None,
        reply_to_bot: bool = False,
        linked_text: str | None = None,
        linked_sender_name: str | None = None,
        linked_type: str | None = None,
    ) -> Classification:
        return self._cls


class _FakeResponder:
    async def respond(self, *, classification: Any, original_text: str, complex_info: Any) -> str | None:
        return None


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


class _EscalationTracker:
    def __init__(self) -> None:
        self.escalated: list[tuple[str, str]] = []  # (reason, text)

    async def escalate(self, *, incoming: Any, complex_info: Any, decision: Any, proposed_reply: Any, prior_context: Any = None) -> None:
        self.escalated.append((decision.escalation_reason or "", incoming.text))


def _build_pipeline(
    cls: Classification,
    complex_row: ComplexRow,
    escalation_tracker: _EscalationTracker,
) -> Pipeline:
    cfg = _make_cfg()
    return Pipeline(
        cfg=cfg,
        classifier=_FakeClassifier(cls),
        responder=_FakeResponder(),
        escalator=escalation_tracker,
        reply_sender=_FakeReplySender(),
        complexes=_FakeComplexesRepo(complex_row),
    )


# ---------------------------------------------------------------------------
# Tests: PROVOCATION + addressed_to=residents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provocation_residents_with_incitement_escalates() -> None:
    """PROVOCATION + residents + incitement-ключевое слово → должна быть эскалация."""
    cls = _make_cls(
        character=Character.PROVOCATION,
        addressed_to=AddressedTo.RESIDENTS,
        summary="Подстрекательство к смене УК",  # ключевое слово в summary
    )
    tracker = _EscalationTracker()
    pipeline = _build_pipeline(cls, _make_complex_row(), tracker)
    msg = _make_msg("Ахахаха тогда меняем парни погнали")

    decision = await pipeline.handle(msg)

    assert decision.escalate is True, "Должна быть эскалация для подстрекательства к смене УК"
    assert decision.escalation_reason == "provocation"
    assert len(tracker.escalated) == 1
    assert tracker.escalated[0][0] == "provocation"


@pytest.mark.asyncio
async def test_provocation_residents_incitement_in_text_escalates() -> None:
    """«менять УК» в тексте → эскалация, даже если addressed_to=residents."""
    cls = _make_cls(
        character=Character.PROVOCATION,
        addressed_to=AddressedTo.RESIDENTS,
        summary="Призыв к смене управляющей",
    )
    tracker = _EscalationTracker()
    pipeline = _build_pipeline(cls, _make_complex_row(), tracker)
    msg = _make_msg("Их бот разрешает менять УК")

    decision = await pipeline.handle(msg)

    assert decision.escalate is True
    assert decision.escalation_reason == "provocation"


@pytest.mark.asyncio
async def test_provocation_residents_without_incitement_no_escalate() -> None:
    """PROVOCATION + residents БЕЗ incitement-маркеров → НЕ эскалировать.

    Перепалки жильцов между собой («ты дурак», «сам дурак») нас не касаются.
    """
    cls = _make_cls(
        character=Character.PROVOCATION,
        addressed_to=AddressedTo.RESIDENTS,
        summary="Жильцы спорят между собой",
        theme=Theme.OTHER,
    )
    tracker = _EscalationTracker()
    pipeline = _build_pipeline(cls, _make_complex_row(), tracker)
    msg = _make_msg("Сам посмотри на своё поведение, сосед")

    decision = await pipeline.handle(msg)

    assert decision.escalate is False, "Перепалки жильцов не должны триггерить эскалацию"
    assert len(tracker.escalated) == 0


@pytest.mark.asyncio
async def test_provocation_uc_always_escalates() -> None:
    """PROVOCATION + addressed_to=uc → эскалация (существующее поведение, не сломали)."""
    cls = _make_cls(
        character=Character.PROVOCATION,
        addressed_to=AddressedTo.UC,
        summary="Бездоказательное обвинение УК в воровстве",
    )
    tracker = _EscalationTracker()
    pipeline = _build_pipeline(cls, _make_complex_row(), tracker)
    msg = _make_msg("Мерзкие воришки в УК, посадить бы всех")

    decision = await pipeline.handle(msg)

    assert decision.escalate is True
    assert decision.escalation_reason == "provocation"


@pytest.mark.asyncio
async def test_aggression_residents_no_escalate() -> None:
    """AGGRESSION + residents (жильцы ругаются между собой) → НЕ эскалировать."""
    cls = _make_cls(
        character=Character.AGGRESSION,
        addressed_to=AddressedTo.RESIDENTS,
        summary="Жилец грубит соседу",
        theme=Theme.OTHER,
    )
    tracker = _EscalationTracker()
    pipeline = _build_pipeline(cls, _make_complex_row(), tracker)
    msg = _make_msg("Заткнись уже, сосед надоел")

    decision = await pipeline.handle(msg)

    assert decision.escalate is False, "Агрессия между жильцами не должна триггерить эскалацию"


@pytest.mark.asyncio
async def test_aggression_uc_escalates() -> None:
    """AGGRESSION + addressed_to=uc → эскалация + удаление (существующее поведение)."""
    cls = _make_cls(
        character=Character.AGGRESSION,
        addressed_to=AddressedTo.UC,
        summary="Оскорбление в адрес УК",
    )
    tracker = _EscalationTracker()
    pipeline = _build_pipeline(cls, _make_complex_row(), tracker)
    msg = _make_msg("УК — мудаки")

    decision = await pipeline.handle(msg)

    assert decision.escalate is True
    assert decision.escalation_reason == "aggression"


# ---------------------------------------------------------------------------
# Tests: context window
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_passed_to_classifier() -> None:
    """Классификатор получает контекст чата из предыдущих сообщений."""
    received_contexts: list[list | None] = []

    class _TrackingClassifier:
        async def classify(
            self, *, text: str, author_name: str | None = None,
            chat_context: list | None = None,
            reply_to_bot: bool = False,
            linked_text: str | None = None,
            linked_sender_name: str | None = None,
            linked_type: str | None = None,
        ) -> Classification:
            received_contexts.append(chat_context)
            return _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UC)

    from balt_dom_bot.services.chat_context import ChatContextManager

    cfg = _make_cfg()
    ctx = ChatContextManager()
    tracker = _EscalationTracker()
    pipeline = Pipeline(
        cfg=cfg,
        classifier=_TrackingClassifier(),
        responder=_FakeResponder(),
        escalator=tracker,
        reply_sender=_FakeReplySender(),
        complexes=_FakeComplexesRepo(_make_complex_row()),
        chat_context=ctx,
    )

    msg1 = _make_msg("Когда починят лифт?")
    msg2 = _make_msg("Спасибо за ответ")

    await pipeline.handle(msg1)
    await pipeline.handle(msg2)

    # При обработке первого сообщения контекст пуст (ничего раньше не было).
    assert received_contexts[0] == [] or received_contexts[0] is None or received_contexts[0] == []

    # При обработке второго — в контексте есть первое сообщение.
    assert received_contexts[1] is not None
    assert len(received_contexts[1]) >= 1
    assert any("Когда починят лифт?" in e.text for e in received_contexts[1])


@pytest.mark.asyncio
async def test_context_window_age_expiry() -> None:
    """Сообщения старше MAX_AGE_SECONDS не попадают в контекст."""
    import time
    from balt_dom_bot.services.chat_context import ChatContextManager

    ctx = ChatContextManager()
    ctx.MAX_AGE_SECONDS = 1  # сжимаем окно до 1 секунды для теста

    ctx.add(chat_id=CHAT_ID, user_id=1, user_name="Старый", text="Старое сообщение")

    # Ждём истечения окна
    await asyncio.sleep(1.1)

    entries = ctx.get_context(chat_id=CHAT_ID, exclude_last=False)
    assert entries == [], "Протухшие сообщения должны быть выброшены из контекста"


@pytest.mark.asyncio
async def test_context_stores_bot_reply() -> None:
    """Ответ бота записывается в контекст как bot_reply."""
    from balt_dom_bot.services.chat_context import ChatContextManager

    ctx = ChatContextManager()
    ctx.add(chat_id=CHAT_ID, user_id=USER_ID, user_name="Лев", text="Вопрос жильца")
    ctx.attach_bot_reply(chat_id=CHAT_ID, user_id=USER_ID, reply_text="Ответ бота")

    entries = ctx.get_context(chat_id=CHAT_ID, exclude_last=False)
    assert len(entries) == 1
    assert entries[0].bot_reply == "Ответ бота"


@pytest.mark.asyncio
async def test_context_max_messages() -> None:
    """Буфер хранит не больше MAX_PER_CHAT сообщений (FIFO)."""
    from balt_dom_bot.services.chat_context import ChatContextManager

    ctx = ChatContextManager()
    for i in range(ctx.MAX_PER_CHAT + 5):
        ctx.add(chat_id=CHAT_ID, user_id=i, user_name=f"user{i}", text=f"msg{i}")

    entries = ctx.get_context(chat_id=CHAT_ID, exclude_last=False)
    assert len(entries) == ctx.MAX_PER_CHAT
    # Должны остаться ПОСЛЕДНИЕ сообщения (старые вытолканы).
    assert entries[-1].text == f"msg{ctx.MAX_PER_CHAT + 4}"
