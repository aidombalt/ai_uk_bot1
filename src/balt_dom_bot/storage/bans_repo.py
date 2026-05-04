"""Repository для логирования банов и их разбана.

Каждый бан создаёт запись в таблице. При разбане заполняется unbanned_at
вместо физического удаления — нужен полный аудит.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)


@dataclass
class BanRow:
    id: int
    chat_id: int
    user_id: int
    user_name: str | None
    complex_id: str | None
    reason: str
    aggression_count: int
    trolling_count: int
    banned_at: str
    unbanned_at: str | None
    unbanned_by: int | None


def _row_to_ban(row: Any) -> BanRow:
    def _g(name: str, default=None):
        try:
            return row[name]
        except (KeyError, IndexError):
            return default

    return BanRow(
        id=_g("id"),
        chat_id=_g("chat_id"),
        user_id=_g("user_id"),
        user_name=_g("user_name"),
        complex_id=_g("complex_id"),
        reason=_g("reason"),
        aggression_count=int(_g("aggression_count", 0) or 0),
        trolling_count=int(_g("trolling_count", 0) or 0),
        banned_at=_g("banned_at"),
        unbanned_at=_g("unbanned_at"),
        unbanned_by=_g("unbanned_by"),
    )


class BansRepo:
    def __init__(self, db: Database):
        self._db = db

    async def record_ban(
        self,
        *,
        chat_id: int,
        user_id: int,
        user_name: str | None,
        complex_id: str | None,
        reason: str,
        aggression_count: int = 0,
        trolling_count: int = 0,
    ) -> int:
        """Создаёт запись о бане. Возвращает id записи."""
        cur = await self._db.conn.execute(
            """
            INSERT INTO bans (chat_id, user_id, user_name, complex_id, reason,
                              aggression_count, trolling_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, user_name, complex_id, reason,
             aggression_count, trolling_count),
        )
        await self._db.conn.commit()
        ban_id = cur.lastrowid
        log.info(
            "bans.recorded",
            id=ban_id, chat_id=chat_id, user_id=user_id, reason=reason,
        )
        return ban_id or 0

    async def find_active_ban(
        self, *, chat_id: int, user_id: int,
    ) -> BanRow | None:
        """Активный бан этого юзера в этом чате (если есть)."""
        cur = await self._db.conn.execute(
            """
            SELECT * FROM bans
            WHERE chat_id = ? AND user_id = ? AND unbanned_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (chat_id, user_id),
        )
        row = await cur.fetchone()
        return _row_to_ban(row) if row else None

    async def list_active(
        self, *, complex_id: str | None = None, limit: int = 100,
    ) -> list[BanRow]:
        """Список активных банов. Опционально — по конкретному ЖК."""
        if complex_id:
            cur = await self._db.conn.execute(
                """
                SELECT * FROM bans WHERE unbanned_at IS NULL AND complex_id = ?
                ORDER BY banned_at DESC LIMIT ?
                """,
                (complex_id, limit),
            )
        else:
            cur = await self._db.conn.execute(
                """
                SELECT * FROM bans WHERE unbanned_at IS NULL
                ORDER BY banned_at DESC LIMIT ?
                """,
                (limit,),
            )
        rows = await cur.fetchall()
        return [_row_to_ban(r) for r in rows]

    async def list_all(self, *, limit: int = 100) -> list[BanRow]:
        """Все записи (включая разбаненные) для аудита."""
        cur = await self._db.conn.execute(
            "SELECT * FROM bans ORDER BY banned_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [_row_to_ban(r) for r in rows]

    async def mark_unbanned(
        self, *, ban_id: int, by_user_id: int | None = None,
    ) -> bool:
        """Помечает бан как снятый. Возвращает True если успешно."""
        cur = await self._db.conn.execute(
            """
            UPDATE bans
            SET unbanned_at = datetime('now'), unbanned_by = ?
            WHERE id = ? AND unbanned_at IS NULL
            """,
            (by_user_id, ban_id),
        )
        await self._db.conn.commit()
        ok = (cur.rowcount or 0) > 0
        if ok:
            log.info("bans.unbanned", id=ban_id, by=by_user_id)
        return ok

    async def find_active_by_chat_user(
        self, *, chat_id: int, user_id: int,
    ) -> int | None:
        """Возвращает id активного бана для (chat_id, user_id) если есть."""
        ban = await self.find_active_ban(chat_id=chat_id, user_id=user_id)
        return ban.id if ban else None
