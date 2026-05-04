"""Обработчики жизненного цикла: bot_started, bot_added, bot_removed + /help."""

from __future__ import annotations

from typing import Any

from balt_dom_bot.config import AppConfig
from balt_dom_bot.log import get_logger

log = get_logger(__name__)


HELP_TEXT = (
    "Я — AI-ассистент управляющей компании. Работаю в чатах ЖК:\n\n"
    "🟢 Отвечаю на типовые вопросы (вода, лифт, охрана, благоустройство и т.д.)\n"
    "🆘 Срочные обращения и аварии — сразу передаю управляющему\n"
    "🔇 На оскорбления и провокации публично не отвечаю — пересылаю\n\n"
    "Команды:\n"
    "/start — приветствие\n"
    "/help — это сообщение\n\n"
    "Если Вы — управляющий: Ваш chat_id для настройки бота указан в логах "
    "после нажатия /start (lifecycle.bot_started chat_id=...)."
)


async def _safe_send(event: Any, text: str) -> None:
    """Безопасная отправка: если нет прав, пишем в лог и идём дальше."""
    chat_id = getattr(event, "chat_id", None)
    if chat_id is None:
        return
    try:
        await event.bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        log.warning("lifecycle.send_failed", chat_id=chat_id, error=str(exc))


def register_lifecycle_handlers(dp: Any, cfg: AppConfig) -> None:
    """Регистрирует хендлеры в диспетчере maxapi."""

    @dp.bot_started()
    async def on_bot_started(event: Any) -> None:  # type: ignore[no-untyped-def]
        chat_id = getattr(event, "chat_id", None)
        user = getattr(event, "user", None)
        user_id = getattr(user, "user_id", None) if user else None
        log.info("lifecycle.bot_started", chat_id=chat_id, user_id=user_id)
        await _safe_send(
            event,
            "Здравствуйте! Я ассистент управляющей компании.\n\n"
            "В групповом чате жилого комплекса я отвечаю на типовые вопросы; "
            "сложные обращения передаются управляющему.\n\n"
            "Команды: /help",
        )

    @dp.bot_added()
    async def on_bot_added(event: Any) -> None:  # type: ignore[no-untyped-def]
        chat_id = getattr(event, "chat_id", None)
        log.info("lifecycle.bot_added", chat_id=chat_id)
        # Часто у бота ещё нет прав admin → send_message вернёт 403.
        # Это нормально — просто пишем в логи и не пытаемся слать без прав.
        await _safe_send(
            event,
            "Бот добавлен в чат. Для работы выдайте ему права администратора "
            "и сообщите chat_id админу для регистрации ЖК в системе.",
        )

    @dp.bot_removed()
    async def on_bot_removed(event: Any) -> None:  # type: ignore[no-untyped-def]
        log.info("lifecycle.bot_removed", chat_id=getattr(event, "chat_id", None))


async def register_bot_commands(bot: Any) -> None:
    """Регистрирует список команд бота через Max API.

    Сигнатура `bot.set_my_commands(*commands)` — принимает развёрнутые
    `BotCommand` объекты, не список. Поэтому вызываем с распаковкой.
    """
    try:
        from maxapi.types.command import BotCommand  # type: ignore[import-not-found]
    except ImportError:
        log.warning("bot.commands_skipped", reason="BotCommand not importable")
        return

    commands = [
        BotCommand(name="start", description="Приветствие"),
        BotCommand(name="help", description="Что умеет бот"),
    ]
    method = getattr(bot, "set_my_commands", None)
    if not callable(method):
        log.warning("bot.commands_skipped", reason="set_my_commands not found")
        return
    try:
        await method(*commands)  # развёрнуто, не списком!
        log.info("bot.commands_registered", count=len(commands))
    except Exception as exc:
        log.warning("bot.commands_failed", error=str(exc), error_type=type(exc).__name__)
