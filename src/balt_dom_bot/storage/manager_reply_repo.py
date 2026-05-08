"""Репозиторий Manager Reply Flow.

notification_map  — отслеживает mid уведомлений/карточек бота в чате «Обращения».
manager_reply_drafts — черновики ответов управляющего, ожидающие выбора варианта отправки.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)


class DraftStatus(StrEnum):
    PENDING = "PENDING"              # ожидает нажатия кнопки управляющим
    SENT_FORMATTED = "SENT_FORMATTED"
    SENT_ORIGINAL = "SENT_ORIGINAL"
    SUPERSEDED = "SUPERSEDED"        # вытеснен более новым ответом управляющего


@dataclass
class NotificationEntry:
    notif_mid: str
    notif_chat_id: int
    complex_id: str
    resident_chat_id: int
    resident_mid: str
    resident_name: str | None
    resident_user_id: int | None
    created_at: datetime


@dataclass
class DraftRow:
    id: int
    notif_mid: str
    notif_chat_id: int
    complex_id: str
    resident_chat_id: int
    resident_mid: str
    resident_user_id: int | None
    manager_text: str
    formatted_text: str | None
    status: DraftStatus
    choice_card_mid: str | None   # mid карточки выбора в чате «Обращения»
    created_at: datetime
    sent_at: datetime | None


class ManagerReplyRepo:
    def __init__(self, db: Database):
        self._db = db

    # --- notification_map --------------------------------------------------

    async def save_notification(
        self,
        *,
        notif_mid: str,
        notif_chat_id: int,
        complex_id: str,
        resident_chat_id: int,
        resident_mid: str,
        resident_name: str | None,
        resident_user_id: int | None = None,
    ) -> None:
        """Сохраняет mid уведомления/карточки → контекст жильца.

        INSERT OR IGNORE — идемпотентен: повторный вызов с тем же mid
        безопасен и ничего не меняет.
        """
        await self._db.conn.execute(
            """
            INSERT OR IGNORE INTO notification_map
              (notif_mid, notif_chat_id, complex_id, resident_chat_id,
               resident_mid, resident_name, resident_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (notif_mid, notif_chat_id, complex_id, resident_chat_id,
             resident_mid, resident_name, resident_user_id),
        )
        await self._db.conn.commit()
        log.debug("notif_map.saved", notif_mid=notif_mid, complex_id=complex_id)

    async def find_notification(self, notif_mid: str) -> NotificationEntry | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM notification_map WHERE notif_mid = ?",
            (notif_mid,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return NotificationEntry(
            notif_mid=row["notif_mid"],
            notif_chat_id=row["notif_chat_id"],
            complex_id=row["complex_id"],
            resident_chat_id=row["resident_chat_id"],
            resident_mid=row["resident_mid"],
            resident_name=row["resident_name"],
            resident_user_id=row["resident_user_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # --- manager_reply_drafts ----------------------------------------------

    async def create_draft(self, notif: NotificationEntry, manager_text: str) -> int:
        """Создаёт черновик со статусом PENDING. Возвращает id."""
        cur = await self._db.conn.execute(
            """
            INSERT INTO manager_reply_drafts
              (notif_mid, notif_chat_id, complex_id, resident_chat_id,
               resident_mid, resident_user_id, manager_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notif.notif_mid, notif.notif_chat_id, notif.complex_id,
                notif.resident_chat_id, notif.resident_mid,
                notif.resident_user_id, manager_text,
            ),
        )
        await self._db.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_draft(self, draft_id: int) -> DraftRow | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM manager_reply_drafts WHERE id = ?",
            (draft_id,),
        )
        row = await cur.fetchone()
        return _row_to_draft(row) if row else None

    async def set_draft_choice_card(
        self,
        draft_id: int,
        *,
        formatted_text: str | None,
        choice_card_mid: str | None,
    ) -> None:
        """Сохраняет форматированный текст и mid карточки выбора."""
        await self._db.conn.execute(
            """
            UPDATE manager_reply_drafts
            SET formatted_text = ?, choice_card_mid = ?
            WHERE id = ?
            """,
            (formatted_text, choice_card_mid, draft_id),
        )
        await self._db.conn.commit()

    async def cancel_pending(self, notif_mid: str) -> list[str]:
        """Помечает все PENDING черновики для notif_mid как SUPERSEDED.

        Вызывается когда управляющий отправил новый ответ до нажатия кнопки.
        Возвращает список choice_card_mid для удаления из чата.
        """
        cur = await self._db.conn.execute(
            """
            SELECT id, choice_card_mid FROM manager_reply_drafts
            WHERE notif_mid = ? AND status = 'PENDING'
            """,
            (notif_mid,),
        )
        rows = await cur.fetchall()
        if not rows:
            return []

        mids_to_delete = [r["choice_card_mid"] for r in rows if r["choice_card_mid"]]
        ids = [r["id"] for r in rows]

        await self._db.conn.execute(
            "UPDATE manager_reply_drafts SET status = 'SUPERSEDED' "
            f"WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        await self._db.conn.commit()
        log.info(
            "drafts.superseded",
            notif_mid=notif_mid,
            count=len(ids),
            cards_to_delete=len(mids_to_delete),
        )
        return mids_to_delete

    async def mark_sent(self, draft_id: int, status: DraftStatus) -> bool:
        """Атомарно переводит PENDING → SENT_*. Возвращает True если успешно."""
        cur = await self._db.conn.execute(
            """
            UPDATE manager_reply_drafts
            SET status = ?, sent_at = datetime('now')
            WHERE id = ? AND status = 'PENDING'
            """,
            (status.value, draft_id),
        )
        await self._db.conn.commit()
        return cur.rowcount > 0


def _row_to_draft(row) -> DraftRow:
    return DraftRow(
        id=row["id"],
        notif_mid=row["notif_mid"],
        notif_chat_id=row["notif_chat_id"],
        complex_id=row["complex_id"],
        resident_chat_id=row["resident_chat_id"],
        resident_mid=row["resident_mid"],
        resident_user_id=row["resident_user_id"],
        manager_text=row["manager_text"],
        formatted_text=row["formatted_text"],
        status=DraftStatus(row["status"]),
        choice_card_mid=row["choice_card_mid"],
        created_at=datetime.fromisoformat(row["created_at"]),
        sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
    )
