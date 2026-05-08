"""Smoke-тесты Manager Reply Flow.

Проверяем:
1.  ManagerReplyRepo.save_notification / find_notification — базовый CRUD.
2.  create_draft / get_draft — создание черновика.
3.  cancel_pending — умная замена: старый PENDING → SUPERSEDED, возврат card_mids.
4.  mark_sent — атомарный переход PENDING → SENT_*, идемпотентность.
5.  ManagerReplyHandler.try_handle — нет linked_mid → False.
6.  ManagerReplyHandler.try_handle — chat не эскалационный → False.
7.  ManagerReplyHandler.try_handle — linked_mid не в notification_map → False.
8.  ManagerReplyHandler.try_handle — happy path:
    - отменяет старый PENDING черновик + удаляет старую карточку,
    - создаёт новый черновик,
    - вызывает formatter,
    - вызывает sender.send_manager_reply_choice,
    - сохраняет formatted_text + choice_card_mid.
9.  _handle_manager_reply callback — send_formatted=True:
    - отправляет форматированный текст в resident chat,
    - меняет статус на SENT_FORMATTED,
    - вызывает resolve_escalation_card для замены карточки.
10. _handle_manager_reply callback — send_formatted=False (оригинал).
11. _handle_manager_reply callback — повторное нажатие (draft уже SENT_*) → "Уже обработано".
12. Pipeline._maybe_notify_chat — сохраняет mid в notification_map.
13. Escalator.escalate — сохраняет card mid в notification_map при escalate_to_chat=True.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from balt_dom_bot.handlers.manager_reply import ManagerReplyHandler, _format_choice_card
from balt_dom_bot.storage.manager_reply_repo import (
    DraftStatus,
    ManagerReplyRepo,
    NotificationEntry,
)

# ---------------------------------------------------------------------------
# Инфраструктура тестов (in-memory SQLite)
# ---------------------------------------------------------------------------


async def _build_db():
    from balt_dom_bot.storage.db import Database
    db = Database(":memory:")
    await db.connect()
    return db


# ---------------------------------------------------------------------------
# 1–4. Unit-тесты ManagerReplyRepo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_save_and_find_notification():
    db = await _build_db()
    repo = ManagerReplyRepo(db)

    await repo.save_notification(
        notif_mid="mid-001",
        notif_chat_id=-100_001,
        complex_id="jk-1",
        resident_chat_id=-200_001,
        resident_mid="resident-msg-001",
        resident_name="Иван",
    )

    entry = await repo.find_notification("mid-001")
    assert entry is not None
    assert entry.notif_mid == "mid-001"
    assert entry.complex_id == "jk-1"
    assert entry.resident_chat_id == -200_001
    assert entry.resident_name == "Иван"

    # Повторный save — не ломается (INSERT OR IGNORE)
    await repo.save_notification(
        notif_mid="mid-001",
        notif_chat_id=-100_001,
        complex_id="jk-1",
        resident_chat_id=-200_001,
        resident_mid="resident-msg-001",
        resident_name="Иван",
    )
    assert await repo.find_notification("mid-001") is not None

    # Несуществующий
    assert await repo.find_notification("unknown") is None

    await db.close()


@pytest.mark.asyncio
async def test_repo_create_and_get_draft():
    db = await _build_db()
    repo = ManagerReplyRepo(db)

    notif = NotificationEntry(
        notif_mid="mid-001", notif_chat_id=-100_001,
        complex_id="jk-1", resident_chat_id=-200_001,
        resident_mid="res-001", resident_name="Иван",
        created_at=datetime.now(),
    )
    draft_id = await repo.create_draft(notif, "Починим завтра")
    assert isinstance(draft_id, int) and draft_id > 0

    draft = await repo.get_draft(draft_id)
    assert draft is not None
    assert draft.manager_text == "Починим завтра"
    assert draft.status == DraftStatus.PENDING
    assert draft.formatted_text is None
    assert draft.choice_card_mid is None

    # Сохраняем formatted + choice_card_mid
    await repo.set_draft_choice_card(
        draft_id, formatted_text="Работы будут выполнены.", choice_card_mid="card-mid-001"
    )
    draft = await repo.get_draft(draft_id)
    assert draft.formatted_text == "Работы будут выполнены."
    assert draft.choice_card_mid == "card-mid-001"

    await db.close()


@pytest.mark.asyncio
async def test_repo_cancel_pending():
    db = await _build_db()
    repo = ManagerReplyRepo(db)

    notif = NotificationEntry(
        notif_mid="mid-002", notif_chat_id=-100_001,
        complex_id="jk-1", resident_chat_id=-200_001,
        resident_mid="res-002", resident_name=None,
        created_at=datetime.now(),
    )

    # Создаём 2 черновика, один с card_mid
    id1 = await repo.create_draft(notif, "Черновик 1")
    await repo.set_draft_choice_card(id1, formatted_text=None, choice_card_mid="card-001")
    id2 = await repo.create_draft(notif, "Черновик 2")
    await repo.set_draft_choice_card(id2, formatted_text=None, choice_card_mid="card-002")

    # cancel_pending должен вернуть оба card_mid и пометить как SUPERSEDED
    mids = await repo.cancel_pending("mid-002")
    assert set(mids) == {"card-001", "card-002"}

    d1 = await repo.get_draft(id1)
    d2 = await repo.get_draft(id2)
    assert d1.status == DraftStatus.SUPERSEDED
    assert d2.status == DraftStatus.SUPERSEDED

    # Повторный cancel — безопасен, нет PENDING
    mids2 = await repo.cancel_pending("mid-002")
    assert mids2 == []

    await db.close()


@pytest.mark.asyncio
async def test_repo_mark_sent_idempotent():
    db = await _build_db()
    repo = ManagerReplyRepo(db)

    notif = NotificationEntry(
        notif_mid="mid-003", notif_chat_id=-100_001,
        complex_id="jk-1", resident_chat_id=-200_001,
        resident_mid="res-003", resident_name=None,
        created_at=datetime.now(),
    )
    draft_id = await repo.create_draft(notif, "Текст")

    # Первое mark_sent → True
    ok = await repo.mark_sent(draft_id, DraftStatus.SENT_FORMATTED)
    assert ok is True
    assert (await repo.get_draft(draft_id)).status == DraftStatus.SENT_FORMATTED

    # Повторное mark_sent → False (уже не PENDING)
    ok2 = await repo.mark_sent(draft_id, DraftStatus.SENT_ORIGINAL)
    assert ok2 is False
    # Статус не изменился
    assert (await repo.get_draft(draft_id)).status == DraftStatus.SENT_FORMATTED

    await db.close()


# ---------------------------------------------------------------------------
# 5–8. Unit-тесты ManagerReplyHandler
# ---------------------------------------------------------------------------


def _make_handler(
    *,
    complexes_row=None,
    notification_entry=None,
    pending_mids=None,
    formatter_result="Форматированный ответ.",
    choice_mid="choice-mid-001",
):
    complexes = AsyncMock()
    complexes.find_by_escalation_chat = AsyncMock(return_value=complexes_row)

    repo = AsyncMock()
    repo.find_notification = AsyncMock(return_value=notification_entry)
    repo.cancel_pending = AsyncMock(return_value=pending_mids or [])
    repo.create_draft = AsyncMock(return_value=42)
    repo.set_draft_choice_card = AsyncMock()

    formatter = AsyncMock()
    formatter.format = AsyncMock(return_value=formatter_result)

    sender = AsyncMock()
    sender.send_manager_reply_choice = AsyncMock(return_value=choice_mid)

    bot = AsyncMock()
    bot.delete_message = AsyncMock()

    return (
        ManagerReplyHandler(
            complexes=complexes,
            manager_reply_repo=repo,
            reply_formatter=formatter,
            escalation_sender=sender,
            bot=bot,
        ),
        complexes, repo, formatter, sender, bot,
    )


def _make_complex_row(name="Тест ЖК"):
    row = MagicMock()
    row.name = name
    row.id = "jk-test"
    return row


def _make_notif_entry():
    return NotificationEntry(
        notif_mid="mid-linked", notif_chat_id=-100_001,
        complex_id="jk-test", resident_chat_id=-200_001,
        resident_mid="res-999", resident_name="Ольга",
        created_at=datetime.now(),
    )


@pytest.mark.asyncio
async def test_handler_no_linked_mid():
    handler, *_ = _make_handler()
    result = await handler.try_handle(
        chat_id=-100_001, user_id=7, text="Ответ", linked_mid=None
    )
    assert result is False


@pytest.mark.asyncio
async def test_handler_not_escalation_chat():
    handler, complexes, *_ = _make_handler(complexes_row=None)
    result = await handler.try_handle(
        chat_id=-999_999, user_id=7, text="Ответ", linked_mid="some-mid"
    )
    assert result is False
    complexes.find_by_escalation_chat.assert_awaited_once_with(-999_999)


@pytest.mark.asyncio
async def test_handler_linked_mid_not_tracked():
    handler, _, repo, *_ = _make_handler(
        complexes_row=_make_complex_row(), notification_entry=None
    )
    result = await handler.try_handle(
        chat_id=-100_001, user_id=7, text="Ответ", linked_mid="unknown-mid"
    )
    assert result is False
    repo.find_notification.assert_awaited_once_with("unknown-mid")


@pytest.mark.asyncio
async def test_handler_happy_path():
    """Happy path: старый pending черновик отменяется, новый создаётся, карточка отправлена."""
    old_card_mid = "old-card-mid-001"
    handler, _, repo, formatter, sender, bot = _make_handler(
        complexes_row=_make_complex_row("ЖК Победа"),
        notification_entry=_make_notif_entry(),
        pending_mids=[old_card_mid],
        formatter_result="Официальный ответ УК.",
        choice_mid="new-card-mid-001",
    )

    result = await handler.try_handle(
        chat_id=-100_001, user_id=555, text="Починим через пару дней",
        linked_mid="mid-linked",
    )
    assert result is True

    # Старая карточка удалена
    bot.delete_message.assert_awaited_once_with(message_id=old_card_mid)

    # Создан новый черновик
    repo.create_draft.assert_awaited_once()
    call_args = repo.create_draft.call_args
    assert call_args.args[1] == "Починим через пару дней"

    # Форматирование вызвано
    formatter.format.assert_awaited_once_with("Починим через пару дней", "ЖК Победа")

    # Карточка выбора отправлена
    sender.send_manager_reply_choice.assert_awaited_once()
    kw = sender.send_manager_reply_choice.call_args.kwargs
    assert kw["chat_id"] == -100_001
    assert kw["draft_id"] == 42
    assert kw["has_formatted"] is True

    # set_draft_choice_card вызван с правильными данными
    repo.set_draft_choice_card.assert_awaited_once_with(
        42,
        formatted_text="Официальный ответ УК.",
        choice_card_mid="new-card-mid-001",
    )


@pytest.mark.asyncio
async def test_handler_formatter_fails_still_sends_card():
    """Если formatter вернул None — карточка с has_formatted=False отправляется."""
    handler, _, repo, formatter, sender, _ = _make_handler(
        complexes_row=_make_complex_row(),
        notification_entry=_make_notif_entry(),
        formatter_result=None,
        choice_mid="card-123",
    )

    result = await handler.try_handle(
        chat_id=-100_001, user_id=555, text="Исходный текст", linked_mid="mid-linked",
    )
    assert result is True

    kw = sender.send_manager_reply_choice.call_args.kwargs
    assert kw["has_formatted"] is False

    repo.set_draft_choice_card.assert_awaited_once_with(
        42, formatted_text=None, choice_card_mid="card-123",
    )


# ---------------------------------------------------------------------------
# 9–11. Callback tests (_handle_manager_reply)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_send_formatted():
    from balt_dom_bot.handlers.callbacks import _handle_manager_reply
    from balt_dom_bot.storage.manager_reply_repo import DraftRow, DraftStatus

    draft = DraftRow(
        id=42, notif_mid="mid-1", notif_chat_id=-100_001,
        complex_id="jk-1", resident_chat_id=-200_001,
        resident_mid="res-001", manager_text="Починим завтра",
        formatted_text="Работы будут выполнены.",
        status=DraftStatus.PENDING, choice_card_mid="choice-001",
        created_at=datetime.now(), sent_at=None,
    )

    repo = AsyncMock()
    repo.get_draft = AsyncMock(return_value=draft)
    repo.mark_sent = AsyncMock(return_value=True)

    reply_sender = AsyncMock()
    reply_sender.send_reply = AsyncMock()

    escalation_sender = AsyncMock()
    escalation_sender.resolve_escalation_card = AsyncMock(return_value="final-mid")

    event = AsyncMock()
    event.answer = AsyncMock()

    await _handle_manager_reply(
        event, draft_id=42, send_formatted=True,
        manager_reply_repo=repo, reply_sender=reply_sender,
        escalation_sender=escalation_sender, message_log=None,
    )

    # Форматированный текст отправлен жильцу
    reply_sender.send_reply.assert_awaited_once_with(
        chat_id=-200_001,
        text="Работы будут выполнены.",
        reply_to_mid="res-001",
    )
    repo.mark_sent.assert_awaited_once_with(42, DraftStatus.SENT_FORMATTED)

    # Карточка заменена
    escalation_sender.resolve_escalation_card.assert_awaited_once_with(
        chat_id=-100_001,
        old_message_id="choice-001",
        text=pytest.approx("✅ Ответ отправлен в чат ЖК\n─────\nВариант: форматированный ✅\n«Работы будут выполнены.»", rel=None),
    )

    # Уведомление нажавшему
    event.answer.assert_awaited_once()
    assert "форматированный" in event.answer.call_args.kwargs.get("notification", "")


@pytest.mark.asyncio
async def test_callback_send_original():
    from balt_dom_bot.handlers.callbacks import _handle_manager_reply
    from balt_dom_bot.storage.manager_reply_repo import DraftRow, DraftStatus

    draft = DraftRow(
        id=77, notif_mid="mid-2", notif_chat_id=-100_002,
        complex_id="jk-2", resident_chat_id=-200_002,
        resident_mid="res-002", manager_text="Нет воды, ждите",
        formatted_text="Ведутся технические работы.",
        status=DraftStatus.PENDING, choice_card_mid=None,
        created_at=datetime.now(), sent_at=None,
    )

    repo = AsyncMock()
    repo.get_draft = AsyncMock(return_value=draft)
    repo.mark_sent = AsyncMock(return_value=True)

    reply_sender = AsyncMock()
    reply_sender.send_reply = AsyncMock()

    escalation_sender = AsyncMock()
    escalation_sender.resolve_escalation_card = AsyncMock()

    event = AsyncMock()
    event.answer = AsyncMock()

    await _handle_manager_reply(
        event, draft_id=77, send_formatted=False,
        manager_reply_repo=repo, reply_sender=reply_sender,
        escalation_sender=escalation_sender, message_log=None,
    )

    # Оригинальный текст отправлен
    reply_sender.send_reply.assert_awaited_once_with(
        chat_id=-200_002, text="Нет воды, ждите", reply_to_mid="res-002",
    )
    repo.mark_sent.assert_awaited_once_with(77, DraftStatus.SENT_ORIGINAL)

    # choice_card_mid=None → resolve_escalation_card не вызывается
    escalation_sender.resolve_escalation_card.assert_not_awaited()

    assert "оригинал" in event.answer.call_args.kwargs.get("notification", "")


@pytest.mark.asyncio
async def test_callback_already_sent():
    from balt_dom_bot.handlers.callbacks import _handle_manager_reply
    from balt_dom_bot.storage.manager_reply_repo import DraftRow, DraftStatus

    draft = DraftRow(
        id=99, notif_mid="mid-3", notif_chat_id=-100_003,
        complex_id="jk-3", resident_chat_id=-200_003,
        resident_mid="res-003", manager_text="Текст",
        formatted_text=None, status=DraftStatus.SENT_FORMATTED,
        choice_card_mid=None, created_at=datetime.now(), sent_at=datetime.now(),
    )

    repo = AsyncMock()
    repo.get_draft = AsyncMock(return_value=draft)
    repo.mark_sent = AsyncMock()

    reply_sender = AsyncMock()
    escalation_sender = AsyncMock()
    event = AsyncMock()
    event.answer = AsyncMock()

    await _handle_manager_reply(
        event, draft_id=99, send_formatted=True,
        manager_reply_repo=repo, reply_sender=reply_sender,
        escalation_sender=escalation_sender, message_log=None,
    )

    # Статус уже не PENDING — ничего не отправляем
    reply_sender.send_reply.assert_not_awaited()
    # Уведомление об уже-обработанном состоянии
    notif = event.answer.call_args.kwargs.get("notification", "")
    assert "Уже обработано" in notif


# ---------------------------------------------------------------------------
# 12. Pipeline._maybe_notify_chat сохраняет mid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_maybe_notify_saves_notif_mid():
    """_maybe_notify_chat должен передать mid уведомления в manager_reply_repo."""
    from balt_dom_bot.config import AppConfig, PipelineConfig, YandexGptConfig
    from balt_dom_bot.models import (
        Character, Classification, ComplexInfo, IncomingMessage,
        PipelineDecision, ReplyMode, Theme, Urgency,
    )
    from balt_dom_bot.services.pipeline import Pipeline

    cfg = AppConfig(
        yandex_gpt=YandexGptConfig(folder_id="X", api_key="X"),
        pipeline=PipelineConfig(
            confidence_threshold=0.6,
            silent_characters=["AGGRESSION", "PROVOCATION"],
            always_escalate_themes=["EMERGENCY"],
        ),
    )

    notifier = AsyncMock()
    notifier.send_notification_to_chat = AsyncMock(return_value="notif-mid-sent")

    mr_repo = AsyncMock()
    mr_repo.save_notification = AsyncMock()

    pipeline = Pipeline(
        cfg=cfg,
        classifier=AsyncMock(),
        responder=AsyncMock(),
        escalator=AsyncMock(),
        reply_sender=AsyncMock(),
        complexes=AsyncMock(),
        notifier=notifier,
        manager_reply_repo=mr_repo,
    )

    complex_info = ComplexInfo(
        id="jk-1", name="ЖК Тест", address="ул. Тестовая",
        manager_chat_id=-111,
        escalation_chat_id=-222,
        escalate_to_chat=True,
    )
    msg = IncomingMessage(
        chat_id=-200, message_id="msg-abc", user_id=7,
        user_name="Тест", text="Вопрос",
        received_at=datetime.now(),
    )
    cls = Classification(
        theme=Theme.INFO_REQUEST, urgency=Urgency.LOW,
        character=Character.QUESTION, summary="тест", confidence=0.9,
    )
    decision = PipelineDecision(
        classification=cls, reply_text="Ваш вопрос принят.", escalate=False,
    )

    await pipeline._maybe_notify_chat(complex_info, msg, cls, decision)

    # Уведомление отправлено
    notifier.send_notification_to_chat.assert_awaited_once()

    # notification_map обновлён
    mr_repo.save_notification.assert_awaited_once_with(
        notif_mid="notif-mid-sent",
        notif_chat_id=-222,
        complex_id="jk-1",
        resident_chat_id=-200,
        resident_mid="msg-abc",
        resident_name="Тест",
    )


# ---------------------------------------------------------------------------
# 13. Escalator сохраняет card mid в notification_map
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalator_saves_card_mid_to_notif_map():
    from balt_dom_bot.models import (
        Character, Classification, ComplexInfo, IncomingMessage,
        PipelineDecision, Theme, Urgency,
    )
    from balt_dom_bot.services.escalation import Escalator

    sender = AsyncMock()
    sender.send_escalation_to_chat = AsyncMock(return_value="card-mid-sent")
    sender.send_escalation_to_user = AsyncMock(return_value=None)

    esc_repo = AsyncMock()
    esc_repo.create = AsyncMock(return_value=101)
    esc_repo.set_message_ids = AsyncMock()

    mr_repo = AsyncMock()
    mr_repo.save_notification = AsyncMock()

    escalator = Escalator(sender=sender, repo=esc_repo, manager_reply_repo=mr_repo)

    cls = Classification(
        theme=Theme.TECH_FAULT, urgency=Urgency.HIGH,
        character=Character.COMPLAINT_STRONG, summary="авария", confidence=0.95,
    )
    incoming = IncomingMessage(
        chat_id=-200, message_id="msg-esc", user_id=8,
        user_name="Жилец", text="Нет воды!",
        received_at=datetime.now(),
    )
    complex_info = ComplexInfo(
        id="jk-1", name="ЖК Тест", address="ул. Тестовая",
        manager_chat_id=-111,
        escalation_chat_id=-222,
        escalate_to_chat=True,
        escalate_to_manager=False,
    )
    decision = PipelineDecision(
        classification=cls, escalate=True, escalation_reason="high_urgency",
    )

    await escalator.escalate(
        incoming=incoming, complex_info=complex_info,
        decision=decision, proposed_reply=None,
    )

    # Карточка отправлена в чат обращений
    sender.send_escalation_to_chat.assert_awaited_once()

    # notification_map обновлён
    mr_repo.save_notification.assert_awaited_once_with(
        notif_mid="card-mid-sent",
        notif_chat_id=-222,
        complex_id="jk-1",
        resident_chat_id=-200,
        resident_mid="msg-esc",
        resident_name="Жилец",
    )


# ---------------------------------------------------------------------------
# 14. _format_choice_card — форматирование текста карточки
# ---------------------------------------------------------------------------


def test_format_choice_card_with_formatted():
    text = _format_choice_card(
        manager_text="Починим завтра",
        formatted_text="Работы будут выполнены в ближайшее время.",
        complex_name="ЖК Победа",
    )
    assert "ЖК Победа" in text
    assert "Починим завтра" in text
    assert "Работы будут выполнены" in text
    assert "рекомендуется" in text


def test_format_choice_card_without_formatted():
    text = _format_choice_card(
        manager_text="Звоните в диспетчерскую",
        formatted_text=None,
        complex_name="ЖК Тест",
    )
    assert "⚠️" in text
    assert "Звоните в диспетчерскую" in text
    assert "недоступно" in text
