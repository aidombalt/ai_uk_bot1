"""Репозиторий редактируемых промтов.

Дефолтные тексты живут в `prompts/*.py` (константы в коде). При первом
обращении к промту, отсутствующему в БД, кладём дефолт. После этого
GUI может править строку — изменения подхватываются на следующий
вызов (через короткий TTL-кэш в памяти, чтобы не бить БД на каждом
сообщении).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)


@dataclass
class PromptRow:
    name: str
    content: str
    updated_at: str
    updated_by: int | None


class PromptsRepo:
    """CRUD над таблицей `prompts` + lazy-init дефолтами."""

    def __init__(self, db: Database):
        self._db = db

    async def get(self, name: str) -> PromptRow | None:
        cur = await self._db.conn.execute(
            "SELECT name, content, updated_at, updated_by FROM prompts WHERE name = ?",
            (name,),
        )
        row = await cur.fetchone()
        return PromptRow(**dict(row)) if row else None

    async def get_or_seed(self, name: str, default: str) -> str:
        row = await self.get(name)
        if row is not None:
            return row.content
        await self._db.conn.execute(
            "INSERT INTO prompts (name, content) VALUES (?, ?)",
            (name, default),
        )
        await self._db.conn.commit()
        log.info("prompts.seeded", name=name, chars=len(default))
        return default

    async def upsert(self, name: str, content: str, *, by_user_id: int | None = None) -> None:
        await self._db.conn.execute(
            """
            INSERT INTO prompts (name, content, updated_by)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              content = excluded.content,
              updated_at = datetime('now'),
              updated_by = excluded.updated_by
            """,
            (name, content, by_user_id),
        )
        await self._db.conn.commit()
        log.info("prompts.updated", name=name, by=by_user_id)

    async def list_all(self) -> list[PromptRow]:
        cur = await self._db.conn.execute(
            "SELECT name, content, updated_at, updated_by FROM prompts ORDER BY name"
        )
        return [PromptRow(**dict(r)) for r in await cur.fetchall()]


class PromptProvider:
    """Тонкая обёртка с TTL-кэшем для горячего пути pipeline.

    `get(name, default)` возвращает текст промта; на горячем пути читает
    из памяти, раз в `ttl_seconds` подтягивает свежее из БД.
    """

    def __init__(self, repo: PromptsRepo, *, ttl_seconds: float = 30.0):
        self._repo = repo
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    async def get(self, name: str, default: str) -> str:
        now = time.time()
        cached = self._cache.get(name)
        if cached and cached[1] > now:
            return cached[0]
        text = await self._repo.get_or_seed(name, default)
        self._cache[name] = (text, now + self._ttl)
        return text

    def invalidate(self, name: str | None = None) -> None:
        if name is None:
            self._cache.clear()
        else:
            self._cache.pop(name, None)
