"""Smoke-тесты команд для жильцов (/help, /mystatus, /contacts).

Проверяем:
1.  is_resident_command распознаёт все известные команды.
2.  is_resident_command возвращает False для обычных сообщений.
3.  is_resident_command нечувствителен к регистру и @-упоминаниям.
4.  RESIDENT_HELP_TEXT не содержит внутренних технических деталей.
5.  RESIDENT_HELP_TEXT упоминает /mystatus и /contacts.
6.  handle_resident_command → /help отправляет help-текст.
7.  handle_resident_command → /start отправляет help-текст.
8.  handle_resident_command → /mystatus без эскалаций: "нет обращений".
9.  handle_resident_command → /mystatus с обращениями: показывает статусы.
10. handle_resident_command → /contacts без contacts_info: дефолтное сообщение.
11. handle_resident_command → /contacts с contacts_info: кастомный текст.
12. handle_resident_command → /contacts в чате без ЖК: fallback-ответ.
13. Неизвестная команда → handle_resident_command возвращает False.
14. _format_mystatus: PENDING → "в работе", APPROVED → "рассмотрено".
15. _format_mystatus: пустой список → приглашение написать.
16. EscalationRepo.list_by_user_in_chat: возвращает только записи этого user/chat.
17. EscalationRepo.list_by_user_in_chat: пустой список для нового пользователя.
18. ComplexesRepo: contacts_info сохраняется и читается.
19. HELP_TEXT в lifecycle.py ссылается на RESIDENT_HELP_TEXT (не дублируется).
20. messages.py регистрирует resident_commands, не содержит HELP_TEXT-литерала.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest

# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------


async def _build_db():
    from balt_dom_bot.storage.db import Database
    db = Database(":memory:")
    await db.connect()
    return db


def _make_bot(sent: list) -> Any:
    """Мок бота, записывающий отправленные сообщения."""
    bot = MagicMock()
    async def _send(chat_id, text):
        sent.append(text)
    bot.send_message = AsyncMock(side_effect=_send)
    return bot


# ===========================================================================
# 1–3. is_resident_command
# ===========================================================================

def test_is_resident_command_recognizes_help():
    from balt_dom_bot.handlers.resident_commands import is_resident_command
    assert is_resident_command("/help")
    assert is_resident_command("/Help")
    assert is_resident_command("/HELP")
    assert is_resident_command("/help@mybot")
    assert is_resident_command("/start")
    assert is_resident_command("/mystatus")
    assert is_resident_command("/contacts")


def test_is_resident_command_false_for_normal_text():
    from balt_dom_bot.handlers.resident_commands import is_resident_command
    assert not is_resident_command("Привет")
    assert not is_resident_command("когда будет горячая вода?")
    assert not is_resident_command("")
    assert not is_resident_command("/unknown_cmd")
    assert not is_resident_command("/status")   # /status — только для управляющих


def test_is_resident_command_case_insensitive():
    from balt_dom_bot.handlers.resident_commands import is_resident_command
    assert is_resident_command("/MyStatus")
    assert is_resident_command("/CONTACTS")
    assert is_resident_command("/Contacts@bot")


# ===========================================================================
# 4–5. Текст RESIDENT_HELP_TEXT
# ===========================================================================

def test_resident_help_no_internal_details():
    from balt_dom_bot.handlers.resident_commands import RESIDENT_HELP_TEXT
    # Не должно быть слов, раскрывающих внутреннюю механику
    for bad_word in ["пересылаю", "lifecycle", "chat_id", "логах", "управляющий"]:
        assert bad_word not in RESIDENT_HELP_TEXT, \
            f"Help text should not contain internal term: {bad_word!r}"


def test_resident_help_mentions_new_commands():
    from balt_dom_bot.handlers.resident_commands import RESIDENT_HELP_TEXT
    assert "/mystatus" in RESIDENT_HELP_TEXT
    assert "/contacts" in RESIDENT_HELP_TEXT


# ===========================================================================
# 6–7. handle_resident_command → /help и /start
# ===========================================================================

@pytest.mark.asyncio
async def test_handle_help_command():
    from balt_dom_bot.handlers.resident_commands import (
        RESIDENT_HELP_TEXT, handle_resident_command,
    )
    sent = []
    bot = _make_bot(sent)
    complexes = MagicMock()
    complexes.find_by_chat = AsyncMock(return_value=None)

    result = await handle_resident_command(
        bot=bot, text="/help", user_id=42, chat_id=-100,
        complexes=complexes, escalations=None,
    )
    assert result is True
    assert len(sent) == 1
    assert sent[0] == RESIDENT_HELP_TEXT


@pytest.mark.asyncio
async def test_handle_start_command():
    from balt_dom_bot.handlers.resident_commands import (
        RESIDENT_HELP_TEXT, handle_resident_command,
    )
    sent = []
    bot = _make_bot(sent)
    complexes = MagicMock()
    result = await handle_resident_command(
        bot=bot, text="/start", user_id=1, chat_id=-100,
        complexes=complexes, escalations=None,
    )
    assert result is True
    assert sent[0] == RESIDENT_HELP_TEXT


# ===========================================================================
# 8–9. /mystatus
# ===========================================================================

@pytest.mark.asyncio
async def test_mystatus_no_escalations():
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    bot = _make_bot(sent)
    escalations = MagicMock()
    escalations.list_by_user_in_chat = AsyncMock(return_value=[])

    result = await handle_resident_command(
        bot=bot, text="/mystatus", user_id=77, chat_id=-200,
        complexes=MagicMock(), escalations=escalations,
    )
    assert result is True
    assert "нет" in sent[0].lower() or "пока" in sent[0].lower()


@pytest.mark.asyncio
async def test_mystatus_with_escalations():
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    bot = _make_bot(sent)

    mock_rows = [
        {"id": 10, "theme": "TECH_FAULT", "status": "PENDING",
         "created_at": "2026-05-10 10:00:00"},
        {"id": 7, "theme": "EMERGENCY", "status": "APPROVED",
         "created_at": "2026-05-08 15:30:00"},
    ]
    escalations = MagicMock()
    escalations.list_by_user_in_chat = AsyncMock(return_value=mock_rows)

    result = await handle_resident_command(
        bot=bot, text="/mystatus", user_id=77, chat_id=-200,
        complexes=MagicMock(), escalations=escalations,
    )
    assert result is True
    assert "#10" in sent[0]
    assert "#7" in sent[0]
    assert "в работе" in sent[0]
    assert "рассмотрено" in sent[0]


@pytest.mark.asyncio
async def test_mystatus_no_user_id():
    """Без user_id — fallback сообщение об ошибке, не падение."""
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    bot = _make_bot(sent)
    result = await handle_resident_command(
        bot=bot, text="/mystatus", user_id=None, chat_id=-200,
        complexes=MagicMock(), escalations=None,
    )
    assert result is True
    assert len(sent) == 1


# ===========================================================================
# 10–12. /contacts
# ===========================================================================

@pytest.mark.asyncio
async def test_contacts_with_custom_info():
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    bot = _make_bot(sent)

    complex_mock = MagicMock()
    complex_mock.name = "ЖК Аврора-1"
    complex_mock.contacts_info = "🆘 Аварийная: 8 (812) 123-45-67\n📱 Диспетчер: +7 (921) 000"
    complexes = MagicMock()
    complexes.find_by_chat = AsyncMock(return_value=complex_mock)

    result = await handle_resident_command(
        bot=bot, text="/contacts", user_id=5, chat_id=-300,
        complexes=complexes, escalations=None,
    )
    assert result is True
    assert "8 (812) 123-45-67" in sent[0]
    assert "ЖК Аврора-1" in sent[0]


@pytest.mark.asyncio
async def test_contacts_without_custom_info():
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    bot = _make_bot(sent)

    complex_mock = MagicMock()
    complex_mock.name = "ЖК Балтик"
    complex_mock.contacts_info = None
    complexes = MagicMock()
    complexes.find_by_chat = AsyncMock(return_value=complex_mock)

    result = await handle_resident_command(
        bot=bot, text="/contacts", user_id=5, chat_id=-300,
        complexes=complexes, escalations=None,
    )
    assert result is True
    assert "ЖК Балтик" in sent[0]
    # Честный fallback: не обещает «передам специалисту», упоминает аварийную службу
    assert "передам специалисту" not in sent[0]
    assert "аварийн" in sent[0].lower() or "не заполнена" in sent[0].lower()


@pytest.mark.asyncio
async def test_contacts_unknown_chat_fallback():
    """Если чат не является зарегистрированным ЖК — fallback без падения."""
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    bot = _make_bot(sent)
    complexes = MagicMock()
    complexes.find_by_chat = AsyncMock(return_value=None)

    result = await handle_resident_command(
        bot=bot, text="/contacts", user_id=5, chat_id=-999,
        complexes=complexes, escalations=None,
    )
    assert result is True
    assert len(sent) == 1


# ===========================================================================
# 13. Неизвестная команда → False
# ===========================================================================

@pytest.mark.asyncio
async def test_unknown_command_returns_false():
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    bot = _make_bot(sent)
    result = await handle_resident_command(
        bot=bot, text="/unknown_future_command", user_id=1, chat_id=-100,
        complexes=MagicMock(), escalations=None,
    )
    assert result is False
    assert len(sent) == 0


# ===========================================================================
# 14–15. _format_mystatus
# ===========================================================================

def test_format_mystatus_statuses():
    from balt_dom_bot.handlers.resident_commands import _format_mystatus
    rows = [
        {"id": 1, "theme": "UTILITY", "status": "PENDING",
         "created_at": "2026-05-01 09:00:00"},
        {"id": 2, "theme": "IMPROVEMENT", "status": "APPROVED",
         "created_at": "2026-04-30 10:00:00"},
        {"id": 3, "theme": "SECURITY", "status": "IGNORED",
         "created_at": "2026-04-29 11:00:00"},
    ]
    result = _format_mystatus(rows)
    assert "в работе" in result
    assert "рассмотрено" in result
    assert "закрыто" in result
    assert "#1" in result and "#2" in result and "#3" in result


def test_format_mystatus_empty():
    from balt_dom_bot.handlers.resident_commands import _format_mystatus
    result = _format_mystatus([])
    assert "нет" in result.lower() or "пока" in result.lower()
    assert len(result) > 10


def test_format_mystatus_theme_is_russian():
    from balt_dom_bot.handlers.resident_commands import _format_mystatus
    rows = [{"id": 5, "theme": "TECH_FAULT", "status": "PENDING",
             "created_at": "2026-05-01 12:00:00"}]
    result = _format_mystatus(rows)
    # Должна быть русская тема, не английская
    assert "TECH_FAULT" not in result
    assert "Технич" in result


def test_format_mystatus_active_count():
    from balt_dom_bot.handlers.resident_commands import _format_mystatus
    rows = [
        {"id": 1, "theme": "OTHER", "status": "PENDING", "created_at": "2026-05-01 12:00:00"},
        {"id": 2, "theme": "OTHER", "status": "PENDING", "created_at": "2026-05-02 12:00:00"},
        {"id": 3, "theme": "OTHER", "status": "APPROVED", "created_at": "2026-05-03 12:00:00"},
    ]
    result = _format_mystatus(rows)
    assert "2" in result  # 2 активных


# ===========================================================================
# 16–17. EscalationRepo.list_by_user_in_chat
# ===========================================================================

@pytest.mark.asyncio
async def test_escalation_repo_list_by_user_in_chat():
    from balt_dom_bot.storage.escalations import EscalationRepo
    from balt_dom_bot.storage.complexes_repo import ComplexesRepo
    from balt_dom_bot.models import (
        AddressedTo, Character, Classification, IncomingMessage, Theme, Urgency,
    )
    db = await _build_db()
    repo = EscalationRepo(db)

    cls = Classification(
        theme=Theme.TECH_FAULT, urgency=Urgency.HIGH,
        character=Character.QUESTION, summary="Тест",
        confidence=0.9, addressed_to=AddressedTo.UC,
    )
    incoming = IncomingMessage(
        chat_id=-555, message_id="m1", user_id=42, user_name="Иван",
        text="Тест", received_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    # Создаём 2 эскалации для user_id=42 в чате -555
    await repo.create(
        complex_id="c1", incoming=incoming,
        classification=cls, proposed_reply=None,
        reason="high_urgency", manager_chat_id=999,
    )
    await repo.create(
        complex_id="c1", incoming=incoming,
        classification=cls, proposed_reply=None,
        reason="low_confidence", manager_chat_id=999,
    )
    # Создаём 1 эскалацию для другого user_id=77 в том же чате
    other = IncomingMessage(
        chat_id=-555, message_id="m2", user_id=77, user_name="Мария",
        text="Другой запрос", received_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    await repo.create(
        complex_id="c1", incoming=other,
        classification=cls, proposed_reply=None,
        reason="aggression", manager_chat_id=999,
    )

    rows = await repo.list_by_user_in_chat(user_id=42, chat_id=-555)
    assert len(rows) == 2
    assert all(r["status"] == "PENDING" for r in rows)

    # Чужие записи не попадают
    other_rows = await repo.list_by_user_in_chat(user_id=77, chat_id=-555)
    assert len(other_rows) == 1

    # Другой чат — пусто
    empty = await repo.list_by_user_in_chat(user_id=42, chat_id=-9999)
    assert len(empty) == 0


@pytest.mark.asyncio
async def test_escalation_repo_list_by_user_empty():
    from balt_dom_bot.storage.escalations import EscalationRepo
    db = await _build_db()
    repo = EscalationRepo(db)
    rows = await repo.list_by_user_in_chat(user_id=12345, chat_id=-99999)
    assert rows == []


# ===========================================================================
# 18. ComplexesRepo: contacts_info сохраняется и читается
# ===========================================================================

@pytest.mark.asyncio
async def test_complexes_repo_contacts_info_roundtrip():
    from balt_dom_bot.storage.complexes_repo import ComplexesRepo
    db = await _build_db()
    repo = ComplexesRepo(db)
    contacts = "🆘 Аварийная: 8 (812) 123-45-67\n📱 Диспетчер: +7 (921) 888"
    await repo.upsert(
        complex_id="test-contacts",
        name="ЖК Контакты",
        address="ул. Тестовая, 1",
        chat_id=-700,
        manager_chat_id=800,
        contacts_info=contacts,
    )
    c = await repo.get("test-contacts")
    assert c is not None
    assert c.contacts_info == contacts


@pytest.mark.asyncio
async def test_complexes_repo_contacts_info_none_by_default():
    from balt_dom_bot.storage.complexes_repo import ComplexesRepo
    db = await _build_db()
    repo = ComplexesRepo(db)
    await repo.upsert(
        complex_id="test-no-contacts",
        name="ЖК Без контактов",
        address="ул. Тестовая, 2",
        chat_id=-701,
        manager_chat_id=801,
    )
    c = await repo.get("test-no-contacts")
    assert c is not None
    assert c.contacts_info is None


# ===========================================================================
# 19. lifecycle.py ссылается на RESIDENT_HELP_TEXT
# ===========================================================================

def test_lifecycle_imports_resident_help_text():
    """lifecycle.py должен импортировать RESIDENT_HELP_TEXT, не дублировать."""
    import inspect
    import balt_dom_bot.handlers.lifecycle as lc
    source = inspect.getsource(lc)
    assert "RESIDENT_HELP_TEXT" in source
    # Нет старого дублирующего HELP_TEXT литерала
    assert "пересылаю" not in source
    assert 'HELP_TEXT = ' not in source


# ===========================================================================
# 20. messages.py не содержит дублирующего HELP_TEXT
# ===========================================================================

def test_messages_no_duplicate_help_text():
    """messages.py больше не должен содержать HELP_TEXT-константу напрямую."""
    import inspect
    import balt_dom_bot.handlers.messages as msg_module
    source = inspect.getsource(msg_module)
    # Старая константа HELP_TEXT удалена — вместо неё resident_cmd
    assert "HELP_TEXT" not in source
    assert "resident_cmd" in source or "resident_commands" in source
    # Нет упоминания внутренних деталей модерации в константах
    assert "пересылаю" not in source
