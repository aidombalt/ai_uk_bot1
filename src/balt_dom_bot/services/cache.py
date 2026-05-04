"""Кэш ответов для экономии токенов LLM (ТЗ §9.4).

Реализации:
* `InMemoryResponseCache` — TTL+LRU в памяти; default; теряется при рестарте.
* `SqliteResponseCache`   — TTL в SQLite; переживает рестарт; периодический GC.
* `NullResponseCache`     — no-op; удобно для тестов.

Ключ кэша = (complex_id, theme, нормализованный_текст). Per-complex обязателен:
адрес и имя ЖК встроены в текст ответа, кросс-ЖК шаринг недопустим.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Protocol

from balt_dom_bot.log import get_logger
from balt_dom_bot.models import Theme
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)


class ResponseCache(Protocol):
    async def get(self, *, complex_id: str, theme: Theme, text: str) -> str | None: ...
    async def set(self, *, complex_id: str, theme: Theme, text: str, response: str) -> None: ...


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    return text


def _make_key(complex_id: str, theme: Theme, text: str) -> str:
    h = hashlib.sha1(_normalize(text).encode("utf-8")).hexdigest()[:16]
    return f"{complex_id}:{theme.value}:{h}"


# --- in-memory ---------------------------------------------------------------


@dataclass
class _Entry:
    value: str
    expires_at: float


class InMemoryResponseCache:
    """TTL-кэш с ручной чисткой просрочки при доступе."""

    def __init__(self, *, ttl_seconds: float = 3600.0, max_entries: int = 1000):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: dict[str, _Entry] = {}

    async def get(self, *, complex_id: str, theme: Theme, text: str) -> str | None:
        key = _make_key(complex_id, theme, text)
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.time():
            self._data.pop(key, None)
            return None
        log.debug("cache.hit", key=key, backend="memory")
        return entry.value

    async def set(self, *, complex_id: str, theme: Theme, text: str, response: str) -> None:
        if len(self._data) >= self._max:
            oldest = min(self._data.items(), key=lambda kv: kv[1].expires_at)[0]
            self._data.pop(oldest, None)
        key = _make_key(complex_id, theme, text)
        self._data[key] = _Entry(value=response, expires_at=time.time() + self._ttl)
        log.debug("cache.set", key=key, total=len(self._data), backend="memory")


# --- sqlite ------------------------------------------------------------------


class SqliteResponseCache:
    """Хранит кэш в таблице `response_cache`. Просрочка чистится при доступе.

    Подходит для production: переживает рестарт, можно осмотреть глазами.
    На запись — один INSERT OR REPLACE; на чтение — один SELECT.
    """

    def __init__(self, db: Database, *, ttl_seconds: float = 3600.0):
        self._db = db
        self._ttl = ttl_seconds

    async def get(self, *, complex_id: str, theme: Theme, text: str) -> str | None:
        key = _make_key(complex_id, theme, text)
        cur = await self._db.conn.execute(
            "SELECT response, expires_at FROM response_cache WHERE cache_key = ?",
            (key,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        if row["expires_at"] < time.time():
            await self._db.conn.execute(
                "DELETE FROM response_cache WHERE cache_key = ?", (key,)
            )
            await self._db.conn.commit()
            return None
        log.debug("cache.hit", key=key, backend="sqlite")
        return row["response"]

    async def set(self, *, complex_id: str, theme: Theme, text: str, response: str) -> None:
        key = _make_key(complex_id, theme, text)
        await self._db.conn.execute(
            """
            INSERT INTO response_cache (cache_key, response, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              response = excluded.response,
              expires_at = excluded.expires_at
            """,
            (key, response, time.time() + self._ttl),
        )
        await self._db.conn.commit()
        log.debug("cache.set", key=key, backend="sqlite")

    async def gc(self) -> int:
        """Удаляет просроченные записи. Можно вызывать по таймеру."""
        cur = await self._db.conn.execute(
            "DELETE FROM response_cache WHERE expires_at < ?", (time.time(),)
        )
        await self._db.conn.commit()
        log.debug("cache.gc", deleted=cur.rowcount)
        return cur.rowcount or 0


# --- null --------------------------------------------------------------------


class NullResponseCache:
    """Заглушка: ничего не кэширует."""

    async def get(self, *, complex_id: str, theme: Theme, text: str) -> str | None:
        return None

    async def set(self, *, complex_id: str, theme: Theme, text: str, response: str) -> None:
        return None
