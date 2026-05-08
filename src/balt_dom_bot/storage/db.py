"""Async SQLite-обёртка с инкрементальными миграциями.

Версия схемы хранится в `pragma user_version`. Каждая миграция — функция
`(conn) -> None`, которая поднимает версию на единицу.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite

from balt_dom_bot.log import get_logger

log = get_logger(__name__)

Migration = Callable[[aiosqlite.Connection], Awaitable[None]]


# ---------------------------------------------------------------------------
# Миграции. Не редактировать прежние — только добавлять новые в конец.
# ---------------------------------------------------------------------------


async def _m1_escalations(conn: aiosqlite.Connection) -> None:
    statements = [
        """
        CREATE TABLE escalations (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            complex_id         TEXT    NOT NULL,
            chat_id            INTEGER NOT NULL,
            user_message_id    TEXT    NOT NULL,
            user_id            INTEGER,
            user_name          TEXT,
            user_text          TEXT    NOT NULL,
            classification     TEXT    NOT NULL,
            proposed_reply     TEXT,
            reason             TEXT    NOT NULL,
            manager_chat_id    INTEGER NOT NULL,
            manager_message_id TEXT,
            status             TEXT    NOT NULL DEFAULT 'PENDING',
            resolved_by        INTEGER,
            resolved_at        TEXT,
            created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """,
        "CREATE INDEX idx_escalations_status ON escalations(status)",
        "CREATE INDEX idx_escalations_complex ON escalations(complex_id, status)",
    ]
    for stmt in statements:
        await conn.execute(stmt)


async def _m2_logs_and_cache(conn: aiosqlite.Connection) -> None:
    statements = [
        # Входящие сообщения от жильцов.
        """
        CREATE TABLE messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            complex_id      TEXT,
            chat_id         INTEGER NOT NULL,
            user_message_id TEXT    NOT NULL,
            user_id         INTEGER,
            user_name       TEXT,
            user_text       TEXT    NOT NULL,
            classification  TEXT,                  -- JSON, может быть NULL до классификации
            decision        TEXT,                  -- JSON: reply_text/escalate/reason
            received_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """,
        "CREATE INDEX idx_messages_chat ON messages(chat_id, received_at)",
        "CREATE INDEX idx_messages_complex ON messages(complex_id, received_at)",
        # Исходящие ответы бота.
        """
        CREATE TABLE replies (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            complex_id    TEXT,
            chat_id       INTEGER NOT NULL,
            in_reply_to   TEXT,                    -- user_message_id, если есть
            text          TEXT    NOT NULL,
            source        TEXT    NOT NULL,        -- 'auto' | 'manager_approved'
            sent_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """,
        "CREATE INDEX idx_replies_chat ON replies(chat_id, sent_at)",
        # Кэш ответов с TTL.
        """
        CREATE TABLE response_cache (
            cache_key   TEXT PRIMARY KEY,
            response    TEXT    NOT NULL,
            expires_at  REAL    NOT NULL
        )
        """,
        "CREATE INDEX idx_response_cache_expires ON response_cache(expires_at)",
    ]
    for stmt in statements:
        await conn.execute(stmt)


async def _m3_prompts_complexes_users(conn: aiosqlite.Connection) -> None:
    statements = [
        # Редактируемые из GUI системные промты (классификатор, генератор и т.д.).
        """
        CREATE TABLE prompts (
            name        TEXT PRIMARY KEY,
            content     TEXT NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_by  INTEGER
        )
        """,
        # Конфиг ЖК — управляется из GUI; YAML только seed на первом старте.
        """
        CREATE TABLE complexes_db (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            address         TEXT NOT NULL,
            chat_id         INTEGER NOT NULL UNIQUE,
            manager_chat_id INTEGER NOT NULL,
            active          INTEGER NOT NULL DEFAULT 1,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        # Учётные записи администраторов GUI.
        """
        CREATE TABLE users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            login         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name  TEXT,
            role          TEXT NOT NULL DEFAULT 'manager',
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
    ]
    for stmt in statements:
        await conn.execute(stmt)


async def _m4_escalation_targets(conn: aiosqlite.Connection) -> None:
    """Эскалация может идти в личку управляющему И/ИЛИ в отдельный чат «Обращения».
    Каждый канал включается чекбоксом per-ЖК."""
    statements = [
        # NULL = эскалация в чат не используется (только manager).
        "ALTER TABLE complexes_db ADD COLUMN escalation_chat_id INTEGER",
        # Включена ли пересылка управляющему в личку.
        "ALTER TABLE complexes_db ADD COLUMN escalate_to_manager INTEGER NOT NULL DEFAULT 1",
        # Включена ли пересылка в отдельный чат.
        "ALTER TABLE complexes_db ADD COLUMN escalate_to_chat INTEGER NOT NULL DEFAULT 0",
    ]
    for stmt in statements:
        await conn.execute(stmt)


async def _m5_manager_user_id(conn: aiosqlite.Connection) -> None:
    """user_id управляющего — чтобы игнорировать его сообщения в чате ЖК.

    Если управляющий пишет в чат жильцов, бот не должен реагировать как на
    запрос жильца. user_id берётся из логов lifecycle.bot_started, когда
    управляющий нажимает /start боту.
    """
    await conn.execute(
        "ALTER TABLE complexes_db ADD COLUMN manager_user_id INTEGER"
    )


async def _m6_two_card_mids(conn: aiosqlite.Connection) -> None:
    """Эскалация может иметь ДВЕ карточки (личка + чат «Обращения»).

    Чтобы при resolve синхронно отредактировать обе, сохраняем mid каждой
    и chat_id для карточки-в-чате (для delete-and-resend, т.к. Max не убирает
    кнопки через edit_message с пустым attachments).
    """
    await conn.execute("ALTER TABLE escalations ADD COLUMN chat_message_id TEXT")
    await conn.execute("ALTER TABLE escalations ADD COLUMN chat_card_chat_id INTEGER")


async def _m7_moderation(conn: aiosqlite.Connection) -> None:
    """Опциональная авто-модерация мата.

    Per-ЖК флаги:
    * auto_delete_aggression — удалять ли сообщения с агрессией.
    * strikes_for_ban — после скольких страйков банить пользователя (default 3).
    Счётчик страйков с TTL (по last_at), общий по чату ЖК.
    """
    await conn.execute(
        "ALTER TABLE complexes_db ADD COLUMN auto_delete_aggression "
        "INTEGER NOT NULL DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE complexes_db ADD COLUMN strikes_for_ban "
        "INTEGER NOT NULL DEFAULT 3"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS aggression_strikes (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            last_at REAL NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strikes_last_at "
        "ON aggression_strikes(last_at)"
    )


async def _m8_modes_and_global(conn: aiosqlite.Connection) -> None:
    """Режимы ЖК + глобальный тумблер бота.

    * reply_mode: normal/holiday/off — режим ответов жильцам в этом ЖК.
    * holiday_message: кастомный текст для режима holiday (опционально).
    * Таблица global_settings: для bot_enabled и других глобальных флагов.
    """
    await conn.execute(
        "ALTER TABLE complexes_db ADD COLUMN reply_mode TEXT "
        "NOT NULL DEFAULT 'normal'"
    )
    await conn.execute(
        "ALTER TABLE complexes_db ADD COLUMN holiday_message TEXT"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS global_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Bot enabled by default.
    await conn.execute(
        "INSERT OR IGNORE INTO global_settings (key, value) "
        "VALUES ('bot_enabled', '1')"
    )


async def _m9_trolling_strikes_and_bans(conn: aiosqlite.Connection) -> None:
    """Раздельный счётчик троллинга + лог банов.

    * trolling_count в aggression_strikes — счётчик anti-trolling страйков,
      отдельный от агрессии/спама. Имеет свой порог (мягче, по умолчанию 6).
    * trolling_strikes_for_ban в complexes_db — настраиваемый порог.
    * Таблица bans — лог всех банов с возможностью отслеживания unban.
    """
    await conn.execute(
        "ALTER TABLE aggression_strikes ADD COLUMN trolling_count "
        "INTEGER NOT NULL DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE complexes_db ADD COLUMN trolling_strikes_for_ban "
        "INTEGER NOT NULL DEFAULT 6"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            complex_id TEXT,
            reason TEXT NOT NULL,
            aggression_count INTEGER DEFAULT 0,
            trolling_count INTEGER DEFAULT 0,
            banned_at TEXT NOT NULL DEFAULT (datetime('now')),
            unbanned_at TEXT,
            unbanned_by INTEGER
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bans_active "
        "ON bans(chat_id, user_id) WHERE unbanned_at IS NULL"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bans_banned_at "
        "ON bans(banned_at)"
    )


async def _m10_quotas_and_chat(conn: aiosqlite.Connection) -> None:
    """Daily quota per user + chat-mode whitelist + conversation history.

    Per-ЖК настройки квоты:
    * daily_replies_limit — сколько раз бот ответит одному жильцу в окне.
    * daily_window_hours — длина окна в часах.
    * chat_mode_enabled — глобальный per-ЖК тумблер фичи "болтания".

    Таблица user_quota_state — дневной счётчик ответов на пользователя в чате.
    Таблица chat_whitelist — белый список жильцов для болтания.
    Таблица chat_messages — история переписки (последние N реплик
    для передачи в LLM как контекст).
    """
    await conn.execute(
        "ALTER TABLE complexes_db ADD COLUMN daily_replies_limit "
        "INTEGER NOT NULL DEFAULT 5"
    )
    await conn.execute(
        "ALTER TABLE complexes_db ADD COLUMN daily_window_hours "
        "INTEGER NOT NULL DEFAULT 6"
    )
    await conn.execute(
        "ALTER TABLE complexes_db ADD COLUMN chat_mode_enabled "
        "INTEGER NOT NULL DEFAULT 0"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_quota_state (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            replies_count INTEGER NOT NULL DEFAULT 0,
            window_start_at REAL NOT NULL,
            quota_warned INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quota_window "
        "ON user_quota_state(window_start_at)"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_whitelist (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            note TEXT,
            added_at TEXT NOT NULL DEFAULT (datetime('now')),
            added_by INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,  -- 'user' | 'assistant'
            text TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_msgs_user "
        "ON chat_messages(chat_id, user_id, id)"
    )


async def _m11_manager_reply_flow(conn: aiosqlite.Connection) -> None:
    """Manager Reply Flow — отслеживание уведомлений + черновики ответов управляющего.

    notification_map — маппинг mid уведомлений/карточек в чате «Обращения»
    на контекст исходного сообщения жильца. Позволяет боту понять, на какое
    обращение отвечает управляющий, когда пишет реплай на уведомление.

    manager_reply_drafts — черновики ответов управляющего: хранит оригинальный
    текст, форматированный AI-вариант и статус (PENDING / SENT_* / SUPERSEDED).
    """
    await conn.execute("""
        CREATE TABLE notification_map (
            notif_mid        TEXT PRIMARY KEY,
            notif_chat_id    INTEGER NOT NULL,
            complex_id       TEXT NOT NULL,
            resident_chat_id INTEGER NOT NULL,
            resident_mid     TEXT NOT NULL,
            resident_name    TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifmap_chat "
        "ON notification_map(notif_chat_id)"
    )
    await conn.execute("""
        CREATE TABLE manager_reply_drafts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            notif_mid        TEXT NOT NULL,
            notif_chat_id    INTEGER NOT NULL,
            complex_id       TEXT NOT NULL,
            resident_chat_id INTEGER NOT NULL,
            resident_mid     TEXT NOT NULL,
            manager_text     TEXT NOT NULL,
            formatted_text   TEXT,
            status           TEXT NOT NULL DEFAULT 'PENDING',
            choice_card_mid  TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            sent_at          TEXT
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_drafts_notif "
        "ON manager_reply_drafts(notif_mid, status)"
    )


async def _m12_resident_user_id(conn: aiosqlite.Connection) -> None:
    """Добавляем resident_user_id в notification_map и manager_reply_drafts.

    Позволяет принудительно включать cooldown для жильца после того, как
    управляющий отправил ему ответ через Manager Reply Flow.
    """
    await conn.execute(
        "ALTER TABLE notification_map ADD COLUMN resident_user_id INTEGER"
    )
    await conn.execute(
        "ALTER TABLE manager_reply_drafts ADD COLUMN resident_user_id INTEGER"
    )


MIGRATIONS: tuple[Migration, ...] = (
    _m1_escalations,
    _m2_logs_and_cache,
    _m3_prompts_complexes_users,
    _m4_escalation_targets,
    _m5_manager_user_id,
    _m6_two_card_mids,
    _m7_moderation,
    _m8_modes_and_global,
    _m9_trolling_strikes_and_bans,
    _m10_quotas_and_chat,
    _m11_manager_reply_flow,
    _m12_resident_user_id,
)


# ---------------------------------------------------------------------------


class Database:
    """Тонкая обёртка над aiosqlite-соединением + миграции на старте."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        self._conn = conn
        await self._migrate()
        log.info("db.connected", path=str(self._path))

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _migrate(self) -> None:
        cursor = await self.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = int(row[0]) if row else 0
        target = len(MIGRATIONS)
        if version >= target:
            return
        log.info("db.migrating", from_=version, to=target)
        for i in range(version, target):
            async with self.conn.cursor() as cur:
                await cur.execute("BEGIN")
                try:
                    await MIGRATIONS[i](self.conn)
                    await cur.execute(f"PRAGMA user_version = {i + 1}")
                    await cur.execute("COMMIT")
                except Exception:
                    await cur.execute("ROLLBACK")
                    raise
            log.info("db.migrated", step=i + 1)
