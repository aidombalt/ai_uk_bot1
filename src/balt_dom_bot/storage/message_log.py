"""Лог входящих сообщений и исходящих ответов."""

from __future__ import annotations

from typing import Literal

from balt_dom_bot.log import get_logger
from balt_dom_bot.models import Classification, IncomingMessage, PipelineDecision
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)

ReplySource = Literal["auto", "manager_approved"]


class MessageLog:
    def __init__(self, db: Database):
        self._db = db

    async def log_incoming(
        self,
        *,
        incoming: IncomingMessage,
        complex_id: str | None,
        classification: Classification | None,
        decision: PipelineDecision | None,
    ) -> int:
        cur = await self._db.conn.execute(
            """
            INSERT INTO messages
              (complex_id, chat_id, user_message_id, user_id, user_name, user_text,
               classification, decision, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                complex_id,
                incoming.chat_id,
                incoming.message_id,
                incoming.user_id,
                incoming.user_name,
                incoming.text,
                classification.model_dump_json() if classification else None,
                decision.model_dump_json() if decision else None,
                incoming.received_at.isoformat(timespec="seconds"),
            ),
        )
        await self._db.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def log_reply(
        self,
        *,
        complex_id: str | None,
        chat_id: int,
        in_reply_to: str | None,
        text: str,
        source: ReplySource,
    ) -> None:
        await self._db.conn.execute(
            """
            INSERT INTO replies (complex_id, chat_id, in_reply_to, text, source)
            VALUES (?, ?, ?, ?, ?)
            """,
            (complex_id, chat_id, in_reply_to, text, source),
        )
        await self._db.conn.commit()
