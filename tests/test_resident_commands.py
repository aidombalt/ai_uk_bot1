"""Smoke-тесты команд для жильцов (/help, /mystatus, /contacts).

Проверяем:
 1. is_resident_command распознаёт все известные команды.
 2. is_resident_command → False для обычных сообщений и пустой строки.
 3. is_resident_command нечувствителен к регистру и @-упоминаниям.
 4. RESIDENT_HELP_TEXT не содержит внутренних технических деталей.
 5. RESIDENT_HELP_TEXT упоминает /mystatus и /contacts.
 6. handle_resident_command → /help отправляет help-текст.
 7. handle_resident_command → /start отправляет help-текст.
 8. /mystatus без обращений — понятный текст, не «Не удалось».
 9. /mystatus с обращениями — показывает статусы и complex_name.
10. /mystatus без user_id — fallback, не падение.
11. /contacts с contacts_info — кастомный текст УК.
12. /contacts без contacts_info — честный fallback без «передам».
13. /contacts без обращений в ЖК (нет ЖК) — текст с подсказкой.
14. Неизвестная команда → handle_resident_command возвращает False.
15. _format_mystatus: PENDING/APPROVED/IGNORED → русские статусы.
16. _format_mystatus: пустой список → «нет обращений».
17. _format_mystatus: группирует по ЖК.
18. _format_mystatus: считает активных.
19. _format_contacts: один ЖК с контактами.
20. _format_contacts: несколько ЖК.
21. _format_contacts: пустой список → просьба написать в ЖК.
22. EscalationRepo.list_by_user: возвращает записи с complex_name.
23. EscalationRepo.list_by_user: пустой список для нового пользователя.
24. EscalationRepo.list_user_complexes: правильные ЖК для пользователя.
25. ComplexesRepo: contacts_info сохраняется и читается.
26. lifecycle.py импортирует RESIDENT_HELP_TEXT (не дублирует).
27. messages.py содержит resident_commands, не дублирует HELP_TEXT.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
    bot = MagicMock()
    async def _send(chat_id, text):
        sent.append(text)
    bot.send_message = AsyncMock(side_effect=_send)
    return bot


def _make_escalations_mock(rows_by_user=None, complexes_by_user=None):
    """Мок EscalationRepo с list_by_user и list_user_complexes."""
    esc = MagicMock()
    esc.list_by_user = AsyncMock(return_value=rows_by_user or [])
    esc.list_user_complexes = AsyncMock(return_value=complexes_by_user or [])
    return esc


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
    assert not is_resident_command("/status")   # только для управляющих


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
    from balt_dom_bot.handlers.resident_commands import RESIDENT_HELP_TEXT, handle_resident_command
    sent = []
    bot = _make_bot(sent)
    result = await handle_resident_command(
        bot=bot, text="/help", user_id=42, chat_id=42,
        escalations=None,
    )
    assert result is True
    assert sent[0] == RESIDENT_HELP_TEXT


@pytest.mark.asyncio
async def test_handle_start_command():
    from balt_dom_bot.handlers.resident_commands import RESIDENT_HELP_TEXT, handle_resident_command
    sent = []
    result = await handle_resident_command(
        bot=_make_bot(sent), text="/start", user_id=1, chat_id=1,
        escalations=None,
    )
    assert result is True
    assert sent[0] == RESIDENT_HELP_TEXT


# ===========================================================================
# 8–10. /mystatus
# ===========================================================================

@pytest.mark.asyncio
async def test_mystatus_no_escalations():
    """/mystatus без записей — понятный текст, не «Не удалось»."""
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    esc = _make_escalations_mock(rows_by_user=[])
    result = await handle_resident_command(
        bot=_make_bot(sent), text="/mystatus", user_id=77, chat_id=77,
        escalations=esc,
    )
    assert result is True
    # Должны быть слова об отсутствии обращений, а не «Не удалось»
    text = sent[0].lower()
    assert "не удалось" not in text
    assert "нет" in text or "пока" in text


@pytest.mark.asyncio
async def test_mystatus_with_escalations():
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    rows = [
        {"id": 10, "complex_name": "ЖК Балтийский", "theme": "TECH_FAULT",
         "status": "PENDING", "created_at": "2026-05-10 10:00:00"},
        {"id": 7, "complex_name": "ЖК Балтийский", "theme": "EMERGENCY",
         "status": "APPROVED", "created_at": "2026-05-08 15:30:00"},
    ]
    esc = _make_escalations_mock(rows_by_user=rows)
    result = await handle_resident_command(
        bot=_make_bot(sent), text="/mystatus", user_id=77, chat_id=77,
        escalations=esc,
    )
    assert result is True
    assert "#10" in sent[0]
    assert "#7" in sent[0]
    assert "в работе" in sent[0]
    assert "рассмотрено" in sent[0]
    assert "ЖК Балтийский" in sent[0]


@pytest.mark.asyncio
async def test_mystatus_no_user_id():
    """Без user_id — fallback, не падение."""
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    result = await handle_resident_command(
        bot=_make_bot(sent), text="/mystatus", user_id=None, chat_id=1,
        escalations=None,
    )
    assert result is True
    assert len(sent) == 1
    assert "не удалось" in sent[0].lower() or "аккаунт" in sent[0].lower()


# ===========================================================================
# 11–13. /contacts
# ===========================================================================

@pytest.mark.asyncio
async def test_contacts_with_custom_info():
    """/contacts — показывает contacts_info из ЖК жильца."""
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    esc = _make_escalations_mock(complexes_by_user=[
        {"name": "ЖК Аврора-1",
         "contacts_info": "🆘 Аварийная: 8 (812) 123-45-67\n📱 Диспетчер: +7 (921) 000"},
    ])
    result = await handle_resident_command(
        bot=_make_bot(sent), text="/contacts", user_id=5, chat_id=5,
        escalations=esc,
    )
    assert result is True
    assert "8 (812) 123-45-67" in sent[0]
    assert "ЖК Аврора-1" in sent[0]


@pytest.mark.asyncio
async def test_contacts_without_custom_info():
    """/contacts — ЖК найден, но contacts_info не заполнен → честный fallback."""
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    esc = _make_escalations_mock(complexes_by_user=[
        {"name": "ЖК Балтик", "contacts_info": None},
    ])
    result = await handle_resident_command(
        bot=_make_bot(sent), text="/contacts", user_id=5, chat_id=5,
        escalations=esc,
    )
    assert result is True
    assert "ЖК Балтик" in sent[0]
    assert "передам специалисту" not in sent[0]
    assert "аварийн" in sent[0].lower() or "не заполнена" in sent[0].lower()


@pytest.mark.asyncio
async def test_contacts_no_complexes_found():
    """/contacts без обращений в ЖК — подсказка написать в чат ЖК."""
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    esc = _make_escalations_mock(complexes_by_user=[])
    result = await handle_resident_command(
        bot=_make_bot(sent), text="/contacts", user_id=5, chat_id=5,
        escalations=esc,
    )
    assert result is True
    assert len(sent) == 1
    # Должна быть подсказка написать в чат ЖК
    assert "чат" in sent[0].lower() or "напишите" in sent[0].lower()


# ===========================================================================
# 14. Неизвестная команда → False
# ===========================================================================

@pytest.mark.asyncio
async def test_unknown_command_returns_false():
    from balt_dom_bot.handlers.resident_commands import handle_resident_command
    sent = []
    result = await handle_resident_command(
        bot=_make_bot(sent), text="/unknown_future_command", user_id=1, chat_id=1,
        escalations=None,
    )
    assert result is False
    assert len(sent) == 0


# ===========================================================================
# 15–18. _format_mystatus
# ===========================================================================

def test_format_mystatus_statuses():
    from balt_dom_bot.handlers.resident_commands import _format_mystatus
    rows = [
        {"id": 1, "complex_name": "ЖК А", "theme": "UTILITY",
         "status": "PENDING", "created_at": "2026-05-01 09:00:00"},
        {"id": 2, "complex_name": "ЖК А", "theme": "IMPROVEMENT",
         "status": "APPROVED", "created_at": "2026-04-30 10:00:00"},
        {"id": 3, "complex_name": "ЖК А", "theme": "SECURITY",
         "status": "IGNORED", "created_at": "2026-04-29 11:00:00"},
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
    assert "не удалось" not in result.lower()
    assert len(result) > 10


def test_format_mystatus_groups_by_complex():
    from balt_dom_bot.handlers.resident_commands import _format_mystatus
    rows = [
        {"id": 1, "complex_name": "ЖК Север", "theme": "UTILITY",
         "status": "PENDING", "created_at": "2026-05-01 09:00:00"},
        {"id": 2, "complex_name": "ЖК Юг", "theme": "SECURITY",
         "status": "APPROVED", "created_at": "2026-04-30 10:00:00"},
    ]
    result = _format_mystatus(rows)
    assert "ЖК Север" in result
    assert "ЖК Юг" in result


def test_format_mystatus_active_count():
    from balt_dom_bot.handlers.resident_commands import _format_mystatus
    rows = [
        {"id": 1, "complex_name": "ЖК А", "theme": "OTHER",
         "status": "PENDING", "created_at": "2026-05-01 12:00:00"},
        {"id": 2, "complex_name": "ЖК А", "theme": "OTHER",
         "status": "PENDING", "created_at": "2026-05-02 12:00:00"},
        {"id": 3, "complex_name": "ЖК А", "theme": "OTHER",
         "status": "APPROVED", "created_at": "2026-05-03 12:00:00"},
    ]
    result = _format_mystatus(rows)
    assert "2" in result  # 2 активных


# ===========================================================================
# 19–21. _format_contacts
# ===========================================================================

def test_format_contacts_single_with_info():
    from balt_dom_bot.handlers.resident_commands import _format_contacts
    result = _format_contacts([{"name": "ЖК Север", "contacts_info": "📞 8-800-123"}])
    assert "ЖК Север" in result
    assert "8-800-123" in result


def test_format_contacts_multiple():
    from balt_dom_bot.handlers.resident_commands import _format_contacts
    result = _format_contacts([
        {"name": "ЖК А", "contacts_info": "Тел: 111"},
        {"name": "ЖК Б", "contacts_info": None},
    ])
    assert "ЖК А" in result
    assert "ЖК Б" in result
    assert "Тел: 111" in result
    assert "аварийн" in result.lower() or "не заполнена" in result.lower()


def test_format_contacts_empty():
    from balt_dom_bot.handlers.resident_commands import _format_contacts
    result = _format_contacts([])
    # Должна быть подсказка написать в чат ЖК
    assert "чат" in result.lower() or "напишите" in result.lower()
    assert "передам специалисту" not in result


# ===========================================================================
# 22–24. EscalationRepo.list_by_user и list_user_complexes
# ===========================================================================

@pytest.mark.asyncio
async def test_escalation_repo_list_by_user():
    """list_by_user возвращает записи с complex_name через JOIN."""
    db = await _build_db()
    from balt_dom_bot.storage.escalations import EscalationRepo
    from balt_dom_bot.storage.complexes_repo import ComplexesRepo

    complexes = ComplexesRepo(db)
    await complexes.upsert(
        complex_id="jk1", name="ЖК Тест", address="ул. Ленина 1",
        chat_id=-111, manager_chat_id=0,
    )

    esc_repo = EscalationRepo(db)
    cls_json = json.dumps({
        "theme": "UTILITY", "urgency": "LOW", "character": "QUESTION",
        "confidence": 0.9, "addressed_to": "MANAGEMENT",
        "summary": "тест", "keywords": [],
    })
    await db.conn.execute(
        """INSERT INTO escalations
           (complex_id, chat_id, user_message_id, user_id, user_name,
            user_text, classification, proposed_reply, reason,
            manager_chat_id, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("jk1", -111, "msg1", 999, "Иван", "тест", cls_json, None,
         "question", 0, "PENDING"),
    )
    await db.conn.commit()

    rows = await esc_repo.list_by_user(user_id=999)
    assert len(rows) == 1
    assert rows[0]["complex_name"] == "ЖК Тест"
    assert rows[0]["theme"] == "UTILITY"
    await db.close()


@pytest.mark.asyncio
async def test_escalation_repo_list_by_user_empty():
    db = await _build_db()
    from balt_dom_bot.storage.escalations import EscalationRepo
    esc_repo = EscalationRepo(db)
    rows = await esc_repo.list_by_user(user_id=99999)
    assert rows == []
    await db.close()


@pytest.mark.asyncio
async def test_escalation_repo_list_user_complexes():
    """list_user_complexes возвращает ЖК пользователя."""
    db = await _build_db()
    from balt_dom_bot.storage.escalations import EscalationRepo
    from balt_dom_bot.storage.complexes_repo import ComplexesRepo

    complexes = ComplexesRepo(db)
    await complexes.upsert(
        complex_id="jk_c", name="ЖК Контакт", address="пр. Мира 5",
        chat_id=-222, manager_chat_id=0,
        contacts_info="📞 Тел: 8-800",
    )
    esc_repo = EscalationRepo(db)
    cls_json = json.dumps({
        "theme": "SECURITY", "urgency": "HIGH", "character": "COMPLAINT_STRONG",
        "confidence": 0.8, "addressed_to": "MANAGEMENT",
        "summary": "тест", "keywords": [],
    })
    await db.conn.execute(
        """INSERT INTO escalations
           (complex_id, chat_id, user_message_id, user_id, user_name,
            user_text, classification, proposed_reply, reason,
            manager_chat_id, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("jk_c", -222, "msg2", 888, "Петр", "проблема", cls_json, None,
         "aggression", 0, "PENDING"),
    )
    await db.conn.commit()

    complexes_list = await esc_repo.list_user_complexes(user_id=888)
    assert len(complexes_list) == 1
    assert complexes_list[0]["name"] == "ЖК Контакт"
    assert "8-800" in (complexes_list[0]["contacts_info"] or "")
    await db.close()


# ===========================================================================
# 25. ComplexesRepo: contacts_info
# ===========================================================================

@pytest.mark.asyncio
async def test_complexes_repo_contacts_info_roundtrip():
    db = await _build_db()
    from balt_dom_bot.storage.complexes_repo import ComplexesRepo
    repo = ComplexesRepo(db)
    await repo.upsert(
        complex_id="c1", name="ЖК1", address="addr", chat_id=-1,
        manager_chat_id=0, contacts_info="📞 112",
    )
    c = await repo.get("c1")
    assert c is not None
    assert c.contacts_info == "📞 112"
    await db.close()


@pytest.mark.asyncio
async def test_complexes_repo_contacts_info_none_by_default():
    db = await _build_db()
    from balt_dom_bot.storage.complexes_repo import ComplexesRepo
    repo = ComplexesRepo(db)
    await repo.upsert(
        complex_id="c2", name="ЖК2", address="addr", chat_id=-2,
        manager_chat_id=0,
    )
    c = await repo.get("c2")
    assert c is not None
    assert c.contacts_info is None
    await db.close()


# ===========================================================================
# 26–27. Структурные проверки lifecycle.py и messages.py
# ===========================================================================

def test_lifecycle_imports_resident_help_text():
    """lifecycle.py должен импортировать RESIDENT_HELP_TEXT, не дублировать."""
    import inspect
    import balt_dom_bot.handlers.lifecycle as lc
    source = inspect.getsource(lc)
    assert "RESIDENT_HELP_TEXT" in source
    assert "пересылаю" not in source
    assert 'HELP_TEXT = ' not in source


def test_messages_no_duplicate_help_text():
    """messages.py больше не должен содержать HELP_TEXT-константу напрямую."""
    import inspect
    import balt_dom_bot.handlers.messages as msg_module
    source = inspect.getsource(msg_module)
    assert "resident_cmd" in source
    assert 'HELP_TEXT = "' not in source
    assert "HELP_TEXT = '" not in source
