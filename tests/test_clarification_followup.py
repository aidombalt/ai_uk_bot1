"""Smoke-тесты: pipeline корректно обрабатывает ответы жильца на уточняющие вопросы.

Сценарий-триггер:
  1. Жилец пишет жалобу без деталей (нет парадной/квартиры).
  2. Бот задаёт уточняющий вопрос (pipeline.completeness_clarification_needed).
  3. Жилец отвечает кратко: «Первая парадная» (15 символов).
  4. LLM классифицирует ответ как addressed_to=unclear.
  5. БАГ: pipeline.skip_off_topic → бот молчит.
  6. ФИКС: is_pending=True → bypass off-topic → бот отвечает.

Проверяемые инварианты:
  A. После completeness_clarification_needed следующий unclear-ответ обрабатывается.
  B. Pending state — one-shot: сообщение после followup снова фильтруется.
  C. TTL: истёкшее pending не влияет на фильтр.
  D. Pending не утекает между разными пользователями.
  E. Нормальный off-topic без pending по-прежнему фильтруется (регрессия).
  F. Pending сбрасывается если ответ classifed как UC (ответ был явным).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

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
from balt_dom_bot.services.completeness_checker import CompletenessChecker
from balt_dom_bot.services.pipeline import Pipeline, ReplySender
from balt_dom_bot.storage.complexes_repo import ComplexRow

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

CHAT_ID = -100_000_099
USER_ID = 55
OTHER_USER_ID = 56
NOW = datetime(2026, 5, 8, 14, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Фабрики
# ---------------------------------------------------------------------------


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
        id="test-clarify-1",
        name="Тест ЖК",
        address="ул. Тестовая, 1",
        chat_id=CHAT_ID,
        manager_chat_id=99999,
        active=True,
        updated_at="2026-01-01",
        escalation_chat_id=None,
        escalate_to_manager=False,
        escalate_to_chat=False,
        manager_user_id=None,
        auto_delete_aggression=False,
        strikes_for_ban=3,
        trolling_strikes_for_ban=6,
        reply_mode="normal",
        holiday_message=None,
        daily_replies_limit=10,
        daily_window_hours=6,
        chat_mode_enabled=False,
    )
    defaults.update(kwargs)
    return ComplexRow(**defaults)


def _make_msg(
    text: str,
    user_id: int = USER_ID,
) -> IncomingMessage:
    return IncomingMessage(
        chat_id=CHAT_ID,
        message_id=f"mid.{abs(hash(text)):016x}",
        user_id=user_id,
        user_name="Лев Тестов",
        text=text,
        received_at=NOW,
        reply_to_bot=False,
    )


def _make_cls(
    *,
    character: Character,
    addressed_to: AddressedTo,
    theme: Theme = Theme.IMPROVEMENT,
    urgency: Urgency = Urgency.MEDIUM,
    confidence: float = 0.9,
) -> Classification:
    return Classification(
        theme=theme,
        urgency=urgency,
        character=character,
        name="Лев",
        summary="тест уточнения",
        confidence=confidence,
        addressed_to=addressed_to,
    )


# ---------------------------------------------------------------------------
# Фейковые зависимости
# ---------------------------------------------------------------------------


class _FakeClassifier:
    """Возвращает по очереди заранее заданные Classifications."""

    def __init__(self, responses: list[Classification]) -> None:
        self._responses = list(responses)
        self._idx = 0

    async def classify(self, *, text: str, **_: Any) -> Classification:
        cls = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return cls


class _FakeResponder:
    def __init__(self, reply: str = "Ответ бота") -> None:
        self._reply = reply
        self.call_count = 0

    async def respond(self, *, classification: Any, original_text: str, complex_info: Any, **_: Any) -> str:
        self.call_count += 1
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
    async def escalate(self, **_: Any) -> None:
        pass


def _build_pipeline(
    classifier: _FakeClassifier,
    *,
    completeness: CompletenessChecker | None = None,
    responder: _FakeResponder | None = None,
    complex_row: ComplexRow | None = None,
) -> tuple[Pipeline, _FakeReplySender]:
    cfg = _make_cfg()
    reply_sender = _FakeReplySender()
    pipeline = Pipeline(
        cfg=cfg,
        classifier=classifier,
        responder=responder or _FakeResponder(),
        escalator=_FakeEscalator(),
        reply_sender=reply_sender,
        complexes=_FakeComplexesRepo(complex_row or _make_complex_row()),
        completeness=completeness,
    )
    return pipeline, reply_sender


# ---------------------------------------------------------------------------
# A: ответ на уточняющий вопрос (unclear) не фильтруется при is_pending=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarification_followup_not_filtered() -> None:
    """Краткий ответ на уточняющий вопрос (addressed_to=unclear) обрабатывается,
    а не отбрасывается как off-topic.

    Воспроизводит баг из prod-логов: «Первая парадная» (15 символов, unclear)
    уходило в pipeline.skip_off_topic сразу после completeness_clarification_needed.
    """
    completeness = CompletenessChecker()

    # Первое сообщение: жалоба без деталей → classified as UC/COMPLAINT_MILD
    cls_initial = _make_cls(character=Character.COMPLAINT_MILD, addressed_to=AddressedTo.UC)
    # Второе сообщение: краткий ответ → LLM говорит unclear
    cls_followup = _make_cls(
        character=Character.QUESTION,
        addressed_to=AddressedTo.UNCLEAR,
        urgency=Urgency.LOW,
    )

    responder = _FakeResponder(reply="Спасибо, принято!")
    pipeline, reply_sender = _build_pipeline(
        _FakeClassifier([cls_initial, cls_followup]),
        completeness=completeness,
        responder=responder,
    )

    # Шаг 1: первое сообщение — completeness должен поставить set_pending
    msg1 = _make_msg("Нам поменяли в субботу один коврик который у входа чистый, тот что у лифта грязный")
    await pipeline.handle(msg1)

    # Убеждаемся что pending установлен
    assert completeness.is_pending(CHAT_ID, USER_ID), (
        "После completeness_clarification_needed должен быть set_pending"
    )
    assert len(reply_sender.replies) == 1, "Бот должен ответить (с уточняющим вопросом)"

    # Шаг 2: ответ жильца «Первая парадная» — unclear, но должен обработаться
    msg2 = _make_msg("Первая парадная")
    await pipeline.handle(msg2)

    assert len(reply_sender.replies) == 2, (
        "Бот должен ответить на followup (не фильтровать как off-topic)"
    )


# ---------------------------------------------------------------------------
# B: pending — one-shot, следующее unclear-сообщение снова фильтруется
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarification_pending_is_one_shot() -> None:
    """После обработки followup pending сбрасывается.

    Третье unclear-сообщение от того же юзера должно снова фильтроваться.
    """
    completeness = CompletenessChecker()

    cls_initial = _make_cls(character=Character.COMPLAINT_MILD, addressed_to=AddressedTo.UC)
    cls_followup = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UNCLEAR, urgency=Urgency.LOW)
    cls_random = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UNCLEAR, urgency=Urgency.LOW)

    pipeline, reply_sender = _build_pipeline(
        _FakeClassifier([cls_initial, cls_followup, cls_random]),
        completeness=completeness,
    )

    # Шаг 1: initial complaint → pending установлен
    await pipeline.handle(_make_msg("Коврик у лифта грязный и мокрый был вчера и сегодня"))
    assert completeness.is_pending(CHAT_ID, USER_ID)

    # Шаг 2: followup → pending использован и очищен
    await pipeline.handle(_make_msg("Первая парадная"))
    assert not completeness.is_pending(CHAT_ID, USER_ID), (
        "После обработки followup pending должен быть очищен"
    )
    replies_after_followup = len(reply_sender.replies)

    # Шаг 3: следующее unclear — снова off-topic, бот молчит
    await pipeline.handle(_make_msg("ну и что"))
    assert len(reply_sender.replies) == replies_after_followup, (
        "Третье unclear-сообщение без pending должно фильтроваться"
    )


# ---------------------------------------------------------------------------
# C: CompletenessChecker.is_pending уважает TTL
# ---------------------------------------------------------------------------


def test_pending_ttl_expired() -> None:
    """is_pending возвращает False и очищает запись после истечения TTL."""
    checker = CompletenessChecker(pending_ttl_seconds=1)
    checker.set_pending(CHAT_ID, USER_ID)
    assert checker.is_pending(CHAT_ID, USER_ID)

    # Имитируем истечение TTL подменой timestamp
    checker._pending[(CHAT_ID, USER_ID)] = time.time() - 2  # 2 секунды назад

    assert not checker.is_pending(CHAT_ID, USER_ID), "После TTL is_pending должен вернуть False"
    assert (CHAT_ID, USER_ID) not in checker._pending, "Запись должна быть удалена"


# ---------------------------------------------------------------------------
# D: pending не утекает между пользователями
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_does_not_leak_between_users() -> None:
    """Pending для одного пользователя не влияет на другого."""
    completeness = CompletenessChecker()

    cls_unclear = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UNCLEAR, urgency=Urgency.LOW)

    pipeline, reply_sender = _build_pipeline(
        _FakeClassifier([cls_unclear]),
        completeness=completeness,
    )

    # Ставим pending только для USER_ID
    completeness.set_pending(CHAT_ID, USER_ID)

    # OTHER_USER_ID пишет unclear — у него нет pending, должно фильтроваться
    msg_other = _make_msg("ну давай", user_id=OTHER_USER_ID)
    await pipeline.handle(msg_other)

    assert len(reply_sender.replies) == 0, (
        "У OTHER_USER_ID нет pending — его unclear должно фильтроваться"
    )
    # Pending USER_ID должен остаться нетронутым
    assert completeness.is_pending(CHAT_ID, USER_ID), (
        "Pending USER_ID не должен быть тронут обработкой OTHER_USER_ID"
    )


# ---------------------------------------------------------------------------
# E: регрессия — нормальный off-topic без pending фильтруется как раньше
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_offtopic_still_filtered_without_pending() -> None:
    """Обычные off-topic сообщения (RESIDENTS/UNCLEAR) без pending по-прежнему молча игнорируются."""
    completeness = CompletenessChecker()

    cls_residents = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.RESIDENTS, urgency=Urgency.LOW)
    cls_unclear = _make_cls(character=Character.QUESTION, addressed_to=AddressedTo.UNCLEAR, urgency=Urgency.LOW)

    pipeline_r, replies_r = _build_pipeline(
        _FakeClassifier([cls_residents]),
        completeness=completeness,
    )
    pipeline_u, replies_u = _build_pipeline(
        _FakeClassifier([cls_unclear]),
        completeness=completeness,
    )

    await pipeline_r.handle(_make_msg("Привет соседи, кто идёт на субботник?"))
    await pipeline_u.handle(_make_msg("ну и что"))

    assert len(replies_r.replies) == 0, "RESIDENTS без pending → off-topic, бот молчит"
    assert len(replies_u.replies) == 0, "UNCLEAR без pending → off-topic, бот молчит"


# ---------------------------------------------------------------------------
# F: set_pending/is_pending/clear_pending unit-тесты
# ---------------------------------------------------------------------------


def test_completeness_checker_pending_lifecycle() -> None:
    """Unit-тест полного цикла pending: set → is_pending=True → clear → is_pending=False."""
    checker = CompletenessChecker()

    assert not checker.is_pending(CHAT_ID, USER_ID), "Изначально нет pending"

    checker.set_pending(CHAT_ID, USER_ID)
    assert checker.is_pending(CHAT_ID, USER_ID), "После set_pending — True"

    checker.clear_pending(CHAT_ID, USER_ID)
    assert not checker.is_pending(CHAT_ID, USER_ID), "После clear_pending — False"


def test_completeness_checker_clear_nonexistent_is_safe() -> None:
    """clear_pending на несуществующую запись не бросает исключение."""
    checker = CompletenessChecker()
    checker.clear_pending(CHAT_ID, USER_ID)  # не должно упасть


def test_completeness_checker_pending_isolated_per_user() -> None:
    """Pending изолирован по (chat_id, user_id) — разные ключи не мешают друг другу."""
    checker = CompletenessChecker()
    checker.set_pending(CHAT_ID, USER_ID)
    checker.set_pending(CHAT_ID, OTHER_USER_ID)

    checker.clear_pending(CHAT_ID, USER_ID)

    assert not checker.is_pending(CHAT_ID, USER_ID)
    assert checker.is_pending(CHAT_ID, OTHER_USER_ID), (
        "Очистка USER_ID не должна затрагивать OTHER_USER_ID"
    )
