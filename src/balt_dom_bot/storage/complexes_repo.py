"""Репозиторий конфига ЖК.

YAML — seed-источник на первом старте. Дальше единственный источник истины — БД.
Pipeline ищет ЖК через `ComplexesRepo.find_by_chat`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from balt_dom_bot.config import ComplexConfig
from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)


@dataclass
class ComplexRow:
    id: str
    name: str
    address: str
    chat_id: int
    manager_chat_id: int
    active: bool
    updated_at: str
    escalation_chat_id: int | None = None
    escalate_to_manager: bool = True
    escalate_to_chat: bool = False
    manager_user_id: int | None = None
    auto_delete_aggression: bool = False
    strikes_for_ban: int = 3
    trolling_strikes_for_ban: int = 6
    reply_mode: str = "normal"   # 'normal' | 'holiday' | 'off'
    holiday_message: str | None = None
    daily_replies_limit: int = 5
    daily_window_hours: int = 6
    chat_mode_enabled: bool = False
    contacts_info: str | None = None


class ComplexesRepo:
    def __init__(self, db: Database):
        self._db = db
        # Маленький TTL-кэш для горячего пути (find_by_chat на каждое сообщение).
        self._cache_by_chat: dict[int, tuple[ComplexRow | None, float]] = {}
        self._ttl = 10.0

    # --- read --------------------------------------------------------------

    async def list_all(self, *, only_active: bool = False) -> list[ComplexRow]:
        sql = "SELECT * FROM complexes_db"
        params: tuple = ()
        if only_active:
            sql += " WHERE active = 1"
        sql += " ORDER BY name"
        cur = await self._db.conn.execute(sql, params)
        return [_row_to_complex(r) for r in await cur.fetchall()]

    async def get(self, complex_id: str) -> ComplexRow | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM complexes_db WHERE id = ?", (complex_id,)
        )
        row = await cur.fetchone()
        return _row_to_complex(row) if row else None

    async def find_by_chat(self, chat_id: int) -> ComplexRow | None:
        now = time.time()
        cached = self._cache_by_chat.get(chat_id)
        if cached and cached[1] > now:
            return cached[0]
        cur = await self._db.conn.execute(
            "SELECT * FROM complexes_db WHERE chat_id = ? AND active = 1", (chat_id,)
        )
        row = await cur.fetchone()
        result = _row_to_complex(row) if row else None
        self._cache_by_chat[chat_id] = (result, now + self._ttl)
        return result

    async def find_by_escalation_chat(self, chat_id: int) -> ComplexRow | None:
        """Возвращает ЖК, для которого данный chat_id является чатом «Обращения».

        Используется для определения: является ли входящее сообщение репляем
        управляющего в чате «Обращения» (не в основном чате ЖК).
        Не кэшируется — этот путь не на горячем пути обработки жильцов.
        """
        cur = await self._db.conn.execute(
            "SELECT * FROM complexes_db WHERE escalation_chat_id = ? AND active = 1",
            (chat_id,),
        )
        row = await cur.fetchone()
        return _row_to_complex(row) if row else None

    # --- write -------------------------------------------------------------

    async def upsert(
        self,
        *,
        complex_id: str,
        name: str,
        address: str,
        chat_id: int,
        manager_chat_id: int,
        active: bool = True,
        escalation_chat_id: int | None = None,
        escalate_to_manager: bool = True,
        escalate_to_chat: bool = False,
        manager_user_id: int | None = None,
        auto_delete_aggression: bool = False,
        strikes_for_ban: int = 3,
        trolling_strikes_for_ban: int = 6,
        reply_mode: str = "normal",
        holiday_message: str | None = None,
        daily_replies_limit: int = 5,
        daily_window_hours: int = 6,
        chat_mode_enabled: bool = False,
        contacts_info: str | None = None,
    ) -> None:
        # Защита: бан раньше 1 страйка нелогичен, больше 10 — фактически выключает фичу.
        strikes_for_ban = max(1, min(10, int(strikes_for_ban)))
        trolling_strikes_for_ban = max(2, min(20, int(trolling_strikes_for_ban)))
        # Дневная квота: 1–50 ответов, окно 1–24 часа.
        daily_replies_limit = max(1, min(50, int(daily_replies_limit)))
        daily_window_hours = max(1, min(24, int(daily_window_hours)))
        # Валидация режима: только из allowlist.
        if reply_mode not in ("normal", "holiday", "off"):
            reply_mode = "normal"
        await self._db.conn.execute(
            """
            INSERT INTO complexes_db
              (id, name, address, chat_id, manager_chat_id, active,
               escalation_chat_id, escalate_to_manager, escalate_to_chat,
               manager_user_id, auto_delete_aggression, strikes_for_ban,
               trolling_strikes_for_ban, reply_mode, holiday_message,
               daily_replies_limit, daily_window_hours, chat_mode_enabled,
               contacts_info)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name = excluded.name,
              address = excluded.address,
              chat_id = excluded.chat_id,
              manager_chat_id = excluded.manager_chat_id,
              active = excluded.active,
              escalation_chat_id = excluded.escalation_chat_id,
              escalate_to_manager = excluded.escalate_to_manager,
              escalate_to_chat = excluded.escalate_to_chat,
              manager_user_id = excluded.manager_user_id,
              auto_delete_aggression = excluded.auto_delete_aggression,
              strikes_for_ban = excluded.strikes_for_ban,
              trolling_strikes_for_ban = excluded.trolling_strikes_for_ban,
              reply_mode = excluded.reply_mode,
              holiday_message = excluded.holiday_message,
              daily_replies_limit = excluded.daily_replies_limit,
              daily_window_hours = excluded.daily_window_hours,
              chat_mode_enabled = excluded.chat_mode_enabled,
              contacts_info = excluded.contacts_info,
              updated_at = datetime('now')
            """,
            (
                complex_id, name, address, chat_id, manager_chat_id,
                1 if active else 0,
                escalation_chat_id,
                1 if escalate_to_manager else 0,
                1 if escalate_to_chat else 0,
                manager_user_id,
                1 if auto_delete_aggression else 0,
                strikes_for_ban,
                trolling_strikes_for_ban,
                reply_mode,
                holiday_message,
                daily_replies_limit,
                daily_window_hours,
                1 if chat_mode_enabled else 0,
                contacts_info or None,
            ),
        )
        await self._db.conn.commit()
        self._cache_by_chat.clear()
        log.info("complexes.upsert", id=complex_id, mode=reply_mode)

    async def set_reply_mode(self, complex_id: str, reply_mode: str) -> None:
        """Быстрый switch режима без полного upsert. Используется командами в чате."""
        if reply_mode not in ("normal", "holiday", "off"):
            return
        await self._db.conn.execute(
            "UPDATE complexes_db SET reply_mode = ?, updated_at = datetime('now') WHERE id = ?",
            (reply_mode, complex_id),
        )
        await self._db.conn.commit()
        self._cache_by_chat.clear()
        log.info("complexes.set_reply_mode", id=complex_id, mode=reply_mode)

    async def set_auto_delete(self, complex_id: str, value: bool) -> None:
        """Быстрый switch модерации."""
        await self._db.conn.execute(
            "UPDATE complexes_db SET auto_delete_aggression = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if value else 0, complex_id),
        )
        await self._db.conn.commit()
        self._cache_by_chat.clear()
        log.info("complexes.set_auto_delete", id=complex_id, value=value)

    async def list_for_manager(self, user_id: int) -> list[ComplexRow]:
        """Возвращает все ЖК где этот юзер указан как manager_user_id.

        Используется командами админа в ЛС с ботом.
        """
        cur = await self._db.conn.execute(
            "SELECT * FROM complexes_db WHERE manager_user_id = ? ORDER BY name",
            (user_id,),
        )
        return [_row_to_complex(r) for r in await cur.fetchall()]

    async def delete(self, complex_id: str) -> None:
        await self._db.conn.execute(
            "DELETE FROM complexes_db WHERE id = ?", (complex_id,)
        )
        await self._db.conn.commit()
        self._cache_by_chat.clear()

    # --- seed --------------------------------------------------------------

    async def seed_from_yaml(self, complexes: list[ComplexConfig]) -> int:
        """Если таблица пуста — заливаем из YAML. Возвращает число записей."""
        cur = await self._db.conn.execute("SELECT COUNT(*) FROM complexes_db")
        row = await cur.fetchone()
        existing = row[0] if row else 0
        if existing > 0:
            return 0
        for c in complexes:
            await self.upsert(
                complex_id=c.id, name=c.name, address=c.address,
                chat_id=c.chat_id, manager_chat_id=c.manager_chat_id, active=True,
            )
        log.info("complexes.seeded", count=len(complexes))
        return len(complexes)


def _row_to_complex(row) -> ComplexRow:
    # Безопасно читаем новые поля — если миграция m4 ещё не прошла или поля nullable
    keys = row.keys() if hasattr(row, "keys") else []

    def _opt(name, default=None):
        try:
            v = row[name]
            return v if v is not None else default
        except (IndexError, KeyError):
            return default

    return ComplexRow(
        id=row["id"],
        name=row["name"],
        address=row["address"],
        chat_id=row["chat_id"],
        manager_chat_id=row["manager_chat_id"],
        active=bool(row["active"]),
        updated_at=row["updated_at"],
        escalation_chat_id=_opt("escalation_chat_id"),
        escalate_to_manager=bool(_opt("escalate_to_manager", 1)),
        escalate_to_chat=bool(_opt("escalate_to_chat", 0)),
        manager_user_id=_opt("manager_user_id"),
        auto_delete_aggression=bool(_opt("auto_delete_aggression", 0)),
        strikes_for_ban=int(_opt("strikes_for_ban", 3) or 3),
        trolling_strikes_for_ban=int(_opt("trolling_strikes_for_ban", 6) or 6),
        reply_mode=str(_opt("reply_mode", "normal") or "normal"),
        holiday_message=_opt("holiday_message"),
        daily_replies_limit=int(_opt("daily_replies_limit", 5) or 5),
        daily_window_hours=int(_opt("daily_window_hours", 6) or 6),
        chat_mode_enabled=bool(_opt("chat_mode_enabled", 0)),
        contacts_info=_opt("contacts_info"),
    )
