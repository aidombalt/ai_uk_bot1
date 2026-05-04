"""Учётные записи администраторов GUI. bcrypt для хешей."""

from __future__ import annotations

from dataclasses import dataclass

import bcrypt

from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.db import Database

log = get_logger(__name__)


@dataclass
class UserRow:
    id: int
    login: str
    display_name: str | None
    role: str


class UsersRepo:
    def __init__(self, db: Database):
        self._db = db

    async def get_by_login(self, login: str) -> tuple[UserRow, str] | None:
        cur = await self._db.conn.execute(
            "SELECT id, login, password_hash, display_name, role FROM users WHERE login = ?",
            (login,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return (
            UserRow(id=row["id"], login=row["login"],
                    display_name=row["display_name"], role=row["role"]),
            row["password_hash"],
        )

    async def get(self, user_id: int) -> UserRow | None:
        cur = await self._db.conn.execute(
            "SELECT id, login, display_name, role FROM users WHERE id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return UserRow(**dict(row)) if row else None

    async def create(
        self, *, login: str, password: str, display_name: str | None = None,
        role: str = "manager",
    ) -> int:
        ph = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        cur = await self._db.conn.execute(
            "INSERT INTO users (login, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
            (login, ph, display_name, role),
        )
        await self._db.conn.commit()
        assert cur.lastrowid is not None
        log.info("users.created", login=login, role=role)
        return cur.lastrowid

    async def count(self) -> int:
        cur = await self._db.conn.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def ensure_admin(self, *, login: str, password: str) -> None:
        """Если пользователей нет — создаём admin'а с переданным паролем."""
        if await self.count() > 0:
            return
        await self.create(
            login=login, password=password, display_name="Администратор", role="admin",
        )

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode(), password_hash.encode())
        except Exception:
            return False
