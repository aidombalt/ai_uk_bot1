"""Репозиторий эскалаций."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from balt_dom_bot.log import get_logger
from balt_dom_bot.models import Classification, IncomingMessage
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)


class EscalationStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    IGNORED = "IGNORED"


@dataclass
class EscalationRow:
    id: int
    complex_id: str
    chat_id: int
    user_message_id: str
    user_id: int | None
    user_name: str | None
    user_text: str
    classification: Classification
    proposed_reply: str | None
    reason: str
    manager_chat_id: int
    manager_message_id: str | None
    chat_message_id: str | None
    chat_card_chat_id: int | None
    status: EscalationStatus
    resolved_by: int | None
    resolved_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, r) -> EscalationRow:
        # Безопасное чтение опциональных колонок (могут отсутствовать в старых БД).
        keys = r.keys() if hasattr(r, "keys") else []
        def _opt(name: str):
            return r[name] if name in keys else None
        return cls(
            id=r["id"],
            complex_id=r["complex_id"],
            chat_id=r["chat_id"],
            user_message_id=r["user_message_id"],
            user_id=r["user_id"],
            user_name=r["user_name"],
            user_text=r["user_text"],
            classification=Classification.model_validate_json(r["classification"]),
            proposed_reply=r["proposed_reply"],
            reason=r["reason"],
            manager_chat_id=r["manager_chat_id"],
            manager_message_id=r["manager_message_id"],
            chat_message_id=_opt("chat_message_id"),
            chat_card_chat_id=_opt("chat_card_chat_id"),
            status=EscalationStatus(r["status"]),
            resolved_by=r["resolved_by"],
            resolved_at=datetime.fromisoformat(r["resolved_at"]) if r["resolved_at"] else None,
            created_at=datetime.fromisoformat(r["created_at"]),
        )


class EscalationRepo:
    def __init__(self, db: Database):
        self._db = db

    async def create(
        self,
        *,
        complex_id: str,
        incoming: IncomingMessage,
        classification: Classification,
        proposed_reply: str | None,
        reason: str,
        manager_chat_id: int,
    ) -> int:
        cur = await self._db.conn.execute(
            """
            INSERT INTO escalations
              (complex_id, chat_id, user_message_id, user_id, user_name, user_text,
               classification, proposed_reply, reason, manager_chat_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (
                complex_id,
                incoming.chat_id,
                incoming.message_id,
                incoming.user_id,
                incoming.user_name,
                incoming.text,
                classification.model_dump_json(),
                proposed_reply,
                reason,
                manager_chat_id,
            ),
        )
        await self._db.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def set_manager_message_id(self, esc_id: int, manager_message_id: str) -> None:
        await self._db.conn.execute(
            "UPDATE escalations SET manager_message_id = ? WHERE id = ?",
            (manager_message_id, esc_id),
        )
        await self._db.conn.commit()

    async def set_message_ids(
        self,
        esc_id: int,
        *,
        manager_message_id: str | None = None,
        chat_message_id: str | None = None,
        chat_card_chat_id: int | None = None,
    ) -> None:
        """Сохраняет mid карточек в обоих каналах (личка + чат «Обращения»).

        Каждый аргумент опционален — обновляется только то, что передано.
        """
        sets = []
        vals: list = []
        if manager_message_id is not None:
            sets.append("manager_message_id = ?"); vals.append(manager_message_id)
        if chat_message_id is not None:
            sets.append("chat_message_id = ?"); vals.append(chat_message_id)
        if chat_card_chat_id is not None:
            sets.append("chat_card_chat_id = ?"); vals.append(chat_card_chat_id)
        if not sets:
            return
        vals.append(esc_id)
        await self._db.conn.execute(
            f"UPDATE escalations SET {', '.join(sets)} WHERE id = ?", vals,
        )
        await self._db.conn.commit()

    async def get(self, esc_id: int) -> EscalationRow | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM escalations WHERE id = ?", (esc_id,)
        )
        row = await cur.fetchone()
        return EscalationRow.from_row(row) if row else None

    async def resolve(
        self, esc_id: int, *, status: EscalationStatus, by_user_id: int | None
    ) -> EscalationRow | None:
        """Атомарно переводит PENDING → status. Возвращает обновлённую строку,
        либо None если эскалация не существует или уже была разрешена.

        Используем UPDATE с условием на текущий статус — single-statement
        atomic в SQLite, корректно при параллельных вызовах на одном
        aiosqlite-connection.
        """
        cur = await self._db.conn.execute(
            """
            UPDATE escalations
               SET status = ?, resolved_by = ?, resolved_at = datetime('now')
             WHERE id = ? AND status = 'PENDING'
            """,
            (status.value, by_user_id, esc_id),
        )
        await self._db.conn.commit()
        if cur.rowcount == 0:
            return None
        return await self.get(esc_id)

    async def list_pending(self, *, complex_id: str | None = None, limit: int = 50) -> list[EscalationRow]:
        if complex_id:
            cur = await self._db.conn.execute(
                "SELECT * FROM escalations WHERE status='PENDING' AND complex_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (complex_id, limit),
            )
        else:
            cur = await self._db.conn.execute(
                "SELECT * FROM escalations WHERE status='PENDING' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [EscalationRow.from_row(r) for r in await cur.fetchall()]

    async def list_by_user_in_chat(
        self,
        *,
        user_id: int,
        chat_id: int,
        limit: int = 5,
    ) -> list[dict]:
        """Последние обращения конкретного жильца в конкретном чате ЖК.

        Возвращает лёгкие dict-ы (не полные EscalationRow) чтобы не
        тянуть тяжёлую десериализацию Classification.
        """
        cur = await self._db.conn.execute(
            """
            SELECT e.id,
                   json_extract(e.classification, '$.theme') AS theme,
                   e.status,
                   e.created_at
            FROM escalations e
            WHERE e.user_id = ? AND e.chat_id = ?
            ORDER BY e.created_at DESC
            LIMIT ?
            """,
            (user_id, chat_id, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
