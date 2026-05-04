"""Счётчики страйков per (chat_id, user_id) с TTL.

Два независимых счётчика:
* `count` (агрессия) — за мат, спам с явным вредом, провокации.
  Жёстче — порог `strikes_for_ban` (default 3).
* `trolling_count` — за повторный troll-spam (off-topic тегание бота,
  «АЛО», «Эй», спам в cooldown). Мягче — порог `trolling_strikes_for_ban`
  (default 6).

Если последний strike был >TTL дней назад → СООТВЕТСТВУЮЩИЙ счётчик
сбрасывается до 1. TTL общий (last_at). Если жилец ругнулся раз в полгода,
его не нужно банить как рецидивиста.

`reset` обнуляет ОБА счётчика (после бана).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)

DEFAULT_TTL_SECONDS = 7 * 24 * 3600


@dataclass
class StrikeCounts:
    """Текущие значения счётчиков после регистрации страйка."""
    aggression: int
    trolling: int


class StrikesRepo:
    def __init__(self, db: Database, *, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._db = db
        self._ttl = ttl_seconds

    async def register_aggression_strike(
        self, *, chat_id: int, user_id: int,
    ) -> StrikeCounts:
        """Регистрирует страйк агрессии. Возвращает оба счётчика."""
        return await self._register(chat_id=chat_id, user_id=user_id, kind="agg")

    async def register_trolling_strike(
        self, *, chat_id: int, user_id: int,
    ) -> StrikeCounts:
        """Регистрирует страйк троллинга. Возвращает оба счётчика."""
        return await self._register(chat_id=chat_id, user_id=user_id, kind="troll")

    # Backward-compat alias.
    async def register_strike(self, *, chat_id: int, user_id: int) -> int:
        counts = await self.register_aggression_strike(
            chat_id=chat_id, user_id=user_id,
        )
        return counts.aggression

    async def _register(
        self, *, chat_id: int, user_id: int, kind: str,
    ) -> StrikeCounts:
        now = time.time()
        cur = await self._db.conn.execute(
            "SELECT count, trolling_count, last_at FROM aggression_strikes "
            "WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        row = await cur.fetchone()

        if row is None:
            agg = 1 if kind == "agg" else 0
            troll = 1 if kind == "troll" else 0
            await self._db.conn.execute(
                "INSERT INTO aggression_strikes "
                "(chat_id, user_id, count, trolling_count, last_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, user_id, agg, troll, now),
            )
            await self._db.conn.commit()
            return StrikeCounts(aggression=agg, trolling=troll)

        prev_agg = int(row["count"] or 0)
        prev_troll = int(row["trolling_count"] or 0)
        prev_at = row["last_at"]

        if (now - prev_at) > self._ttl:
            new_agg = 1 if kind == "agg" else 0
            new_troll = 1 if kind == "troll" else 0
        else:
            new_agg = prev_agg + (1 if kind == "agg" else 0)
            new_troll = prev_troll + (1 if kind == "troll" else 0)

        await self._db.conn.execute(
            "UPDATE aggression_strikes "
            "SET count = ?, trolling_count = ?, last_at = ? "
            "WHERE chat_id = ? AND user_id = ?",
            (new_agg, new_troll, now, chat_id, user_id),
        )
        await self._db.conn.commit()
        return StrikeCounts(aggression=new_agg, trolling=new_troll)

    async def reset(self, *, chat_id: int, user_id: int) -> None:
        """Обнуляет ОБА счётчика. Используется после бана/разбана."""
        await self._db.conn.execute(
            "DELETE FROM aggression_strikes WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await self._db.conn.commit()
