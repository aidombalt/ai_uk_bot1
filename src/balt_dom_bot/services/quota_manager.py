"""Дневная квота ответов на одного жильца.

Цель: защитить чат от тревожных жильцов которые пишут в чат каждые 5 минут весь
день. anti-flood (2 ответа/мин) ловит короткие очереди, но если человек пишет
с интервалом 30 минут — anti-flood не сработает, а чат всё равно засирается.

Решение: per-user счётчик ответов в скользящем окне (default 5 за 6 часов).
При исчерпании:
* `is_quota_exceeded` возвращает True
* Бот молча игнорирует (silent), НО один раз эскалирует управляющему
  с пометкой «жилец X превысил квоту, возможно требуется внимание»
* После сброса (окно прошло) счётчик начинается заново

Хранение: `user_quota_state` в БД для durability (не теряется при рестартах).
In-memory кэш для скорости — синкается при `register_reply`.

Параметры конфигурируются per-ЖК через ComplexInfo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)


@dataclass
class QuotaState:
    replies_count: int
    window_start_at: float
    quota_warned: bool


class QuotaManager:
    def __init__(self, db: Database):
        self._db = db
        # Кэш для быстрого чтения; синкается при записи.
        self._cache: dict[tuple[int, int], QuotaState] = {}

    async def _load(self, *, chat_id: int, user_id: int) -> QuotaState | None:
        key = (chat_id, user_id)
        if key in self._cache:
            return self._cache[key]
        cur = await self._db.conn.execute(
            "SELECT replies_count, window_start_at, quota_warned "
            "FROM user_quota_state WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        state = QuotaState(
            replies_count=int(row["replies_count"]),
            window_start_at=float(row["window_start_at"]),
            quota_warned=bool(row["quota_warned"]),
        )
        self._cache[key] = state
        return state

    async def _save(
        self, *, chat_id: int, user_id: int, state: QuotaState,
    ) -> None:
        await self._db.conn.execute(
            """
            INSERT INTO user_quota_state
                (chat_id, user_id, replies_count, window_start_at, quota_warned)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                replies_count = excluded.replies_count,
                window_start_at = excluded.window_start_at,
                quota_warned = excluded.quota_warned
            """,
            (chat_id, user_id, state.replies_count,
             state.window_start_at, 1 if state.quota_warned else 0),
        )
        await self._db.conn.commit()
        self._cache[(chat_id, user_id)] = state

    async def is_quota_exceeded(
        self, *,
        chat_id: int, user_id: int | None,
        limit: int, window_hours: int,
    ) -> bool:
        """True если жилец уже использовал свою дневную квоту.

        Если окно прошло — автосброс (вернёт False, состояние очистится).
        """
        if user_id is None:
            return False
        state = await self._load(chat_id=chat_id, user_id=user_id)
        if state is None:
            return False
        # Если окно протухло — сбросим (нулевой счётчик до register_reply).
        now = time.time()
        window_seconds = window_hours * 3600
        if (now - state.window_start_at) > window_seconds:
            return False
        return state.replies_count >= limit

    async def has_been_warned(
        self, *, chat_id: int, user_id: int | None,
        window_hours: int,
    ) -> bool:
        """True если для этого юзера уже отправили эскалацию о превышении.

        Используется чтобы не плодить эскалации: одна на одно превышение.
        """
        if user_id is None:
            return False
        state = await self._load(chat_id=chat_id, user_id=user_id)
        if state is None:
            return False
        now = time.time()
        window_seconds = window_hours * 3600
        if (now - state.window_start_at) > window_seconds:
            return False
        return state.quota_warned

    async def register_reply(
        self, *, chat_id: int, user_id: int | None, window_hours: int,
    ) -> None:
        """Инкрементирует счётчик после успешного ответа жильцу."""
        if user_id is None:
            return
        now = time.time()
        window_seconds = window_hours * 3600
        state = await self._load(chat_id=chat_id, user_id=user_id)

        if state is None or (now - state.window_start_at) > window_seconds:
            # Новое окно — начинаем с 1.
            state = QuotaState(
                replies_count=1, window_start_at=now, quota_warned=False,
            )
        else:
            state.replies_count += 1

        await self._save(chat_id=chat_id, user_id=user_id, state=state)

    async def mark_warned(
        self, *, chat_id: int, user_id: int | None,
    ) -> None:
        """Отмечает что эскалация о превышении уже отправлена."""
        if user_id is None:
            return
        state = await self._load(chat_id=chat_id, user_id=user_id)
        if state is None:
            return
        state.quota_warned = True
        await self._save(chat_id=chat_id, user_id=user_id, state=state)

    async def reset(self, *, chat_id: int, user_id: int) -> None:
        """Ручной сброс квоты для пользователя (для GUI)."""
        await self._db.conn.execute(
            "DELETE FROM user_quota_state WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        await self._db.conn.commit()
        self._cache.pop((chat_id, user_id), None)
        log.info("quota.reset", chat_id=chat_id, user_id=user_id)
