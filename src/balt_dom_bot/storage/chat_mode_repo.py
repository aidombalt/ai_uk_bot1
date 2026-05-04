"""Whitelist жильцов «для болтания» + история переписки.

Концепция: бот может вести **диалог-абсорбер** с конкретными жильцами,
которым нужно «выпустить пар». Не каждому подряд — только с согласия
управляющего, который добавил юзера в whitelist через GUI или /chat_add.

Архитектура:
* `chat_whitelist` — кто из жильцов может «болтать» (per-чат).
* `chat_messages` — история реплик (последние N для контекста LLM).

История ограничивается N последними сообщениями (не временем) — длинные паузы
в чате нормальны, контекст важнее.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)


@dataclass
class WhitelistEntry:
    chat_id: int
    user_id: int
    user_name: str | None
    note: str | None
    added_at: str
    added_by: int | None


@dataclass
class ChatMessage:
    role: str  # 'user' | 'assistant'
    text: str


class ChatModeRepo:
    """Read/write для whitelist и истории переписки."""

    HISTORY_LIMIT = 12  # 6 пар user/assistant — баланс контекста и токенов

    def __init__(self, db: Database):
        self._db = db
        # Кэш членства whitelist для скорости pipeline.
        self._wl_cache: dict[tuple[int, int], bool] = {}

    # ----- whitelist -----

    async def is_whitelisted(self, *, chat_id: int, user_id: int) -> bool:
        key = (chat_id, user_id)
        if key in self._wl_cache:
            return self._wl_cache[key]
        cur = await self._db.conn.execute(
            "SELECT 1 FROM chat_whitelist WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        row = await cur.fetchone()
        result = row is not None
        self._wl_cache[key] = result
        return result

    async def add_to_whitelist(
        self,
        *,
        chat_id: int,
        user_id: int,
        user_name: str | None = None,
        note: str | None = None,
        added_by: int | None = None,
    ) -> None:
        await self._db.conn.execute(
            """
            INSERT INTO chat_whitelist (chat_id, user_id, user_name, note, added_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                user_name = COALESCE(excluded.user_name, user_name),
                note = COALESCE(excluded.note, note)
            """,
            (chat_id, user_id, user_name, note, added_by),
        )
        await self._db.conn.commit()
        self._wl_cache[(chat_id, user_id)] = True
        log.info("chat_whitelist.added",
                 chat_id=chat_id, user_id=user_id, by=added_by)

    async def remove_from_whitelist(
        self, *, chat_id: int, user_id: int,
    ) -> bool:
        cur = await self._db.conn.execute(
            "DELETE FROM chat_whitelist WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        await self._db.conn.commit()
        self._wl_cache.pop((chat_id, user_id), None)
        ok = (cur.rowcount or 0) > 0
        if ok:
            log.info("chat_whitelist.removed", chat_id=chat_id, user_id=user_id)
        # Чистим историю чтобы не висела впустую.
        if ok:
            await self.clear_history(chat_id=chat_id, user_id=user_id)
        return ok

    async def list_whitelist(
        self, *, chat_id: int | None = None,
    ) -> list[WhitelistEntry]:
        if chat_id is not None:
            cur = await self._db.conn.execute(
                "SELECT * FROM chat_whitelist WHERE chat_id=? "
                "ORDER BY added_at DESC", (chat_id,),
            )
        else:
            cur = await self._db.conn.execute(
                "SELECT * FROM chat_whitelist ORDER BY added_at DESC",
            )
        rows = await cur.fetchall()
        return [
            WhitelistEntry(
                chat_id=r["chat_id"], user_id=r["user_id"],
                user_name=r["user_name"], note=r["note"],
                added_at=r["added_at"], added_by=r["added_by"],
            )
            for r in rows
        ]

    # ----- история переписки -----

    async def append_message(
        self, *,
        chat_id: int, user_id: int, role: str, text: str,
    ) -> None:
        """Добавляет реплику в историю. role: 'user' | 'assistant'.

        После записи чистит самые старые строки сверх HISTORY_LIMIT.
        """
        if role not in ("user", "assistant"):
            return
        # Обрезаем длинные сообщения чтобы не раздувать контекст.
        text = text[:1000]
        await self._db.conn.execute(
            "INSERT INTO chat_messages (chat_id, user_id, role, text) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, user_id, role, text),
        )
        # Удаляем старые сверх лимита.
        await self._db.conn.execute(
            """
            DELETE FROM chat_messages
            WHERE id IN (
                SELECT id FROM chat_messages
                WHERE chat_id=? AND user_id=?
                ORDER BY id DESC LIMIT -1 OFFSET ?
            )
            """,
            (chat_id, user_id, self.HISTORY_LIMIT),
        )
        await self._db.conn.commit()

    async def get_history(
        self, *, chat_id: int, user_id: int,
    ) -> list[ChatMessage]:
        """Последние HISTORY_LIMIT сообщений в хронологическом порядке."""
        cur = await self._db.conn.execute(
            """
            SELECT role, text FROM chat_messages
            WHERE chat_id=? AND user_id=?
            ORDER BY id DESC LIMIT ?
            """,
            (chat_id, user_id, self.HISTORY_LIMIT),
        )
        rows = await cur.fetchall()
        # Реверсим — нужен порядок от старого к новому.
        return [ChatMessage(role=r["role"], text=r["text"])
                for r in reversed(rows)]

    async def clear_history(self, *, chat_id: int, user_id: int) -> None:
        await self._db.conn.execute(
            "DELETE FROM chat_messages WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        await self._db.conn.commit()
        log.info("chat_history.cleared", chat_id=chat_id, user_id=user_id)
