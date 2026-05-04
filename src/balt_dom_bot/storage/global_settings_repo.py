"""Глобальные настройки бота (key/value).

Используется в первую очередь для глобального тумблера `bot_enabled`,
но архитектура позволяет добавлять и другие флаги (например, режим
maintenance, временные флаги, etc.).

In-memory кэш с явной инвалидацией. После set_* кэш обновляется сразу.
"""

from __future__ import annotations

from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)


class GlobalSettingsRepo:
    def __init__(self, db: Database):
        self._db = db
        self._cache: dict[str, str] = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._db.conn.execute(
            "SELECT key, value FROM global_settings"
        ) as cur:
            rows = await cur.fetchall()
        self._cache = {r[0]: r[1] for r in rows}
        self._loaded = True

    async def get_str(self, key: str, default: str = "") -> str:
        await self._ensure_loaded()
        return self._cache.get(key, default)

    async def get_bool(self, key: str, default: bool = False) -> bool:
        v = await self.get_str(key, "1" if default else "0")
        return v.strip() in {"1", "true", "True", "on", "yes"}

    async def set_str(self, key: str, value: str) -> None:
        await self._db.conn.execute(
            "INSERT INTO global_settings (key, value, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value),
        )
        await self._db.conn.commit()
        self._cache[key] = value
        log.info("global_settings.set", key=key, value=value)

    async def set_bool(self, key: str, value: bool) -> None:
        await self.set_str(key, "1" if value else "0")

    # --- удобные shortcut'ы --------------------------------------------------

    async def is_bot_enabled(self) -> bool:
        return await self.get_bool("bot_enabled", default=True)

    async def set_bot_enabled(self, enabled: bool) -> None:
        await self.set_bool("bot_enabled", enabled)
