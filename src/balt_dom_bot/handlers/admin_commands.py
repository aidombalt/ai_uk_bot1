"""Команды управления ботом из личного диалога с ботом.

Распознаются ТОЛЬКО при выполнении всех условий:
* Сообщение пришло в личный диалог (chat_type == 'dialog'), а не в группу.
* user_id отправителя совпадает с manager_user_id хотя бы одного ЖК
  (значит это управляющий — авторизован).
* Текст начинается с `/` (команда).

Иначе обработка отдаётся pipeline как обычно.

Команды:
* /help, /start — справка
* /status — список «своих» ЖК с inline-кнопками управления
* /global_off, /global_on — глобальный тумблер бота

Inline-кнопки в /status: смена режима и переключение модерации per-ЖК.
Callback'и обрабатываются в callbacks.py (расширяется отдельно).
"""

from __future__ import annotations

from typing import Any

from balt_dom_bot.log import get_logger
from balt_dom_bot.storage.complexes_repo import ComplexesRepo, ComplexRow
from balt_dom_bot.storage.global_settings_repo import GlobalSettingsRepo

log = get_logger(__name__)


ADMIN_HELP_TEXT = (
    "🛠 Команды управления (только для управляющих):\n\n"
    "/me — показать ваши user_id и chat_id (для настройки в GUI)\n"
    "/status — статус всех ваших ЖК + кнопки управления\n"
    "/global_off — аварийно выключить бота во всех ЖК\n"
    "/global_on — включить обратно\n\n"
    "В выдаче /status у каждого ЖК будут кнопки:\n"
    "🟢 Обычный режим / 🌴 Праздничный / ⏸ Off\n"
    "🛡 Модерация ВКЛ/ВЫКЛ"
)


# Команда /me работает БЕЗ проверки на admin: любой пользователь может
# узнать свой user_id и chat_id, чтобы потом администратор GUI прописал
# его в форме ЖК как manager_user_id и manager_chat_id.
ANYONE_COMMANDS = {"/me", "/myid", "/whoami"}


def _format_me(user_id: int, chat_id: int, user_name: str | None) -> str:
    name = user_name or "—"
    return (
        f"👤 Ваши идентификаторы:\n\n"
        f"<b>user_id</b>: <code>{user_id}</code>\n"
        f"<b>chat_id (этот диалог)</b>: <code>{chat_id}</code>\n"
        f"Имя: {name}\n\n"
        f"Скопируйте эти значения и впишите в форму ЖК в GUI:\n"
        f"• <code>{user_id}</code> → поле «manager_user_id»\n"
        f"• <code>{chat_id}</code> → поле «manager_chat_id»"
    )


def is_command(text: str) -> bool:
    """True если текст начинается с / и похож на команду."""
    return text.startswith("/") and len(text) >= 2


def is_dialog_chat(msg: Any) -> bool:
    """Определяет, это личный диалог 1-на-1 с ботом (а не группа).

    В Max recipient.chat_type может быть 'dialog' или 'chat'. У нас
    нет 100% надёжного способа из maxapi — пробуем разные эвристики.
    """
    recipient = getattr(msg, "recipient", None)
    if recipient is None:
        return False
    chat_type = getattr(recipient, "chat_type", None)
    if chat_type is None:
        # Fallback: в личке chat_id обычно положительный (user_id), в группах
        # отрицательный (-100... или подобный). В Max это скорее всего так же.
        chat_id = getattr(recipient, "chat_id", None)
        if chat_id is not None and isinstance(chat_id, int):
            return chat_id > 0
        return False
    # Различные API возвращают разные строки/enum'ы. Считаем dialog'ом всё,
    # что не chat/group.
    val = str(chat_type).lower()
    return "dialog" in val or val in ("private", "user")


async def is_admin(user_id: int | None, complexes: ComplexesRepo) -> list[ComplexRow]:
    """Возвращает список ЖК, в которых данный user — manager. Пустой → не админ."""
    if user_id is None:
        return []
    return await complexes.list_for_manager(user_id)


def _mode_label(mode: str) -> str:
    return {
        "normal": "🟢 Обычный",
        "holiday": "🌴 Праздничный",
        "off": "⏸ Выключен",
    }.get(mode, mode)


def _format_status(rows: list[ComplexRow], bot_enabled: bool) -> str:
    """Форматирует /status в текст. Inline-кнопки добавляются отдельно."""
    header = (
        f"🤖 Бот: {'🟢 ВКЛЮЧЁН' if bot_enabled else '🔴 ВЫКЛЮЧЕН (ВЕЗДЕ)'}\n\n"
    )
    if not rows:
        return header + "Под вашим управлением нет ЖК."
    parts = [header, f"Ваши ЖК ({len(rows)}):\n"]
    for r in rows:
        parts.append(
            f"\n🏢 {r.name} · id=`{r.id}`\n"
            f"   {('🟢 активен' if r.active else '⚪️ выключен')} · "
            f"режим: {_mode_label(r.reply_mode)} · "
            f"мод: {'🛡 ВКЛ' if r.auto_delete_aggression else '⚪️ ВЫКЛ'}"
        )
    return "".join(parts)


def _build_status_buttons(rows: list[ComplexRow]) -> list[list[dict]]:
    """Готовит inline-кнопки для каждого ЖК.

    Формат payload: 'admin:<action>:<complex_id>:<value>'
    Действия: mode, mod (модерация), active.
    Возвращает список рядов кнопок (в формате описаний для KeyboardBuilder).
    """
    rows_btns: list[list[dict]] = []
    for r in rows:
        # Заголовок-разделитель не делаем — в Max некрасиво. Просто ряд кнопок.
        # Ряд 1: режим (3 кнопки)
        rows_btns.append([
            {
                "text": f"{r.name} · 🟢 normal",
                "payload": f"admin:mode:{r.id}:normal",
                "active": r.reply_mode == "normal",
            },
            {
                "text": "🌴 holiday",
                "payload": f"admin:mode:{r.id}:holiday",
                "active": r.reply_mode == "holiday",
            },
            {
                "text": "⏸ off",
                "payload": f"admin:mode:{r.id}:off",
                "active": r.reply_mode == "off",
            },
        ])
        # Ряд 2: модерация
        rows_btns.append([
            {
                "text": f"🛡 Мод: {'ВКЛ' if r.auto_delete_aggression else 'ВЫКЛ'} → переключить",
                "payload": f"admin:mod:{r.id}:{0 if r.auto_delete_aggression else 1}",
            },
        ])
    return rows_btns


async def handle_admin_command(
    *,
    bot: Any,
    text: str,
    user_id: int,
    chat_id: int,
    user_name: str | None,
    complexes: ComplexesRepo,
    global_settings: GlobalSettingsRepo,
    admin_complexes: list[ComplexRow],
) -> bool:
    """Обработка одной команды от админа.

    Возвращает True если команда распознана и обработана (pipeline
    дальше запускать не нужно). False — если это не команда админа.
    """
    cmd = text.strip().split()[0].lower()
    cmd = cmd.split("@")[0]

    if cmd in ANYONE_COMMANDS:
        # Команда работает для всех (не только админов).
        await _send_html(
            bot, chat_id, _format_me(user_id, chat_id, user_name),
        )
        return True

    if cmd in ("/help", "/start", "/admin"):
        await _send(bot, chat_id, ADMIN_HELP_TEXT)
        return True

    if cmd == "/status":
        bot_enabled = await global_settings.is_bot_enabled()
        body = _format_status(admin_complexes, bot_enabled)
        # Глобальный тумблер тоже кнопкой.
        global_btn_row = [{
            "text": (
                f"🌍 Глобально: {'🔴 ВЫКЛЮЧИТЬ' if bot_enabled else '🟢 ВКЛЮЧИТЬ'}"
            ),
            "payload": f"admin:global:{0 if bot_enabled else 1}",
        }]
        kb_rows = [global_btn_row] + _build_status_buttons(admin_complexes)
        await _send_with_buttons(bot, chat_id, body, kb_rows)
        return True

    if cmd == "/global_off":
        await global_settings.set_bot_enabled(False)
        await _send(bot, chat_id, "🔴 Бот ВЫКЛЮЧЕН во всех чатах. Чтобы включить — /global_on")
        log.warning("admin.global_off", user_id=user_id)
        return True

    if cmd == "/global_on":
        await global_settings.set_bot_enabled(True)
        await _send(bot, chat_id, "🟢 Бот ВКЛЮЧЁН.")
        log.info("admin.global_on", user_id=user_id)
        return True

    # Неопознанная команда от админа — отправляем help, чтобы не молчать.
    await _send(bot, chat_id, "Команда не распознана. " + ADMIN_HELP_TEXT)
    return True


async def _send(bot: Any, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        log.warning("admin.send_failed", chat_id=chat_id, error=str(exc))


async def _send_html(bot: Any, chat_id: int, text: str) -> None:
    """Отправка с HTML-форматированием. Если упадёт — fallback на plain."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, format="html")
    except Exception:
        # Fallback: вырезаем HTML-теги и отправляем как обычный текст.
        import re
        plain = re.sub(r"<[^>]+>", "", text)
        await _send(bot, chat_id, plain)


async def _send_with_buttons(
    bot: Any, chat_id: int, text: str, rows: list[list[dict]],
) -> None:
    """Шлёт сообщение с inline-кнопками. Использует тот же KeyboardBuilder,
    что и эскалации."""
    try:
        from maxapi.types.attachments.buttons.callback_button import CallbackButton
        from maxapi.types.attachments.inline_keyboard.inline_keyboard_attachment import (
            InlineKeyboardBuilder,
        )
    except Exception:
        # Fallback если что-то пошло не так с API
        await _send(bot, chat_id, text)
        return

    builder = InlineKeyboardBuilder()
    for row in rows:
        btns = [
            CallbackButton(text=b["text"][:64], payload=b["payload"][:256])
            for b in row
        ]
        if btns:
            builder.row(*btns)
    try:
        await bot.send_message(
            chat_id=chat_id, text=text, attachments=[builder.as_markup()],
        )
    except Exception as exc:
        log.warning("admin.send_with_btn_failed", chat_id=chat_id, error=str(exc))
        await _send(bot, chat_id, text)
