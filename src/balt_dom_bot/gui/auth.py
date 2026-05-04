"""JWT-cookie аутентификация для GUI."""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request, Response, status

from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.users_repo import UserRow, UsersRepo

log = get_logger(__name__)

COOKIE_NAME = "balt_dom_session"
ALG = "HS256"
TTL_SECONDS = 60 * 60 * 24 * 7  # 7 дней


@dataclass
class AuthConfig:
    secret_key: str  # минимум 32 символа в проде


def issue_token(cfg: AuthConfig, user: UserRow) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user.id),
        "login": user.login,
        "role": user.role,
        "iat": now,
        "exp": now + TTL_SECONDS,
    }
    return jwt.encode(payload, cfg.secret_key, algorithm=ALG)


def decode_token(cfg: AuthConfig, token: str) -> dict | None:
    try:
        return jwt.decode(token, cfg.secret_key, algorithms=[ALG])
    except jwt.PyJWTError as exc:
        log.debug("auth.token_invalid", error=str(exc))
        return None


def set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=TTL_SECONDS, httponly=True, samesite="lax", secure=False,
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


async def get_current_user(
    request: Request, cfg: AuthConfig, users: UsersRepo,
) -> UserRow | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    payload = decode_token(cfg, token)
    if payload is None:
        return None
    user_id_raw = payload.get("sub")
    try:
        user_id = int(user_id_raw) if user_id_raw is not None else None
    except (TypeError, ValueError):
        return None
    if user_id is None:
        return None
    return await users.get(user_id)


async def require_user(
    request: Request, cfg: AuthConfig, users: UsersRepo,
) -> UserRow:
    user = await get_current_user(request, cfg, users)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется вход")
    return user
