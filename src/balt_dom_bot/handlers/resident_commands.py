"""Пользовательские команды для жильцов — работают ТОЛЬКО в личном диалоге с ботом.

В групповом чате ЖК бот на эти команды не реагирует (тихо поглощает),
чтобы не порождать спам в общем чате.

/help, /start  — справка для жильца
/mystatus      — статус обращений во всех ЖК, где жилец активен
/contacts      — контакты УК всех ЖК, где жилец активен
"""

from __future__ import annotations

from typing import Any

from balt_dom_bot.l10n import THEME_RU
from balt_dom_bot.log import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Текст /help — единственный источник истины. Импортируется в lifecycle.py.
# ---------------------------------------------------------------------------

RESIDENT_HELP_TEXT = (
    "Добрый день! Я — цифровой помощник управляющей компании. 🏠\n\n"
    "Если у вас вопрос по дому или ситуация, которую нужно решить —\n"
    "напишите об этом в чат вашего ЖК:\n\n"
    "🔧 Вопросы по дому и инфраструктуре — отвечу сразу или передам специалисту\n"
    "🆘 Авария или срочная ситуация — немедленно уведомлю ответственного\n"
    "💡 Предложения по улучшению — приму и зафиксирую\n\n"
    "Полезные команды (только в этом личном чате):\n"
    "/mystatus — статус ваших обращений\n"
    "/contacts — телефоны аварийной службы и контакты УК\n"
    "/help — эта справка"
)


# ---------------------------------------------------------------------------
# Определение команды
# ---------------------------------------------------------------------------

RESIDENT_COMMANDS = {"/help", "/start", "/команды", "/cmd", "/mystatus", "/contacts"}


def is_resident_command(text: str) -> bool:
    """True если текст — известная пользовательская команда."""
    parts = text.strip().lower().split("@")[0].split()
    if not parts:
        return False
    return parts[0] in RESIDENT_COMMANDS


# ---------------------------------------------------------------------------
# Форматирование /mystatus
# ---------------------------------------------------------------------------

_STATUS_ICON = {
    "PENDING": "⏳",
    "APPROVED": "✅",
    "IGNORED": "🗂",
}

_STATUS_TEXT = {
    "PENDING": "в работе",
    "APPROVED": "рассмотрено",
    "IGNORED": "закрыто",
}


def _fmt_date(iso: str) -> str:
    """ISO datetime → 'дд.мм.гггг чч:мм'."""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso[:16] if iso else "—"


def _format_mystatus(rows: list) -> str:
    """Форматирует список обращений жильца (из list_by_user).

    Строки должны содержать поля: id, complex_name, theme, status, created_at.
    Группирует по ЖК.
    """
    if not rows:
        return (
            "У вас пока нет зафиксированных обращений ни в одном чате ЖК.\n\n"
            "Чтобы обратиться в УК, напишите ваш вопрос в чат вашего ЖК."
        )

    # Группируем по ЖК.
    groups: dict[str, list] = {}
    for r in rows:
        name = str(r.get("complex_name") or "Неизвестный ЖК")
        groups.setdefault(name, []).append(r)

    lines = ["📋 Ваши последние обращения:\n"]
    active = 0

    for complex_name, items in groups.items():
        lines.append(f"🏠 {complex_name}:")
        for r in items:
            icon = _STATUS_ICON.get(str(r["status"]), "•")
            status_text = _STATUS_TEXT.get(str(r["status"]), str(r["status"]))

            raw_theme = r["theme"] or ""
            theme_ru = THEME_RU.get(raw_theme, raw_theme).lstrip("🆘🔧🌿🔒ℹ️⚖️💧📎 ").strip()

            lines.append(
                f"  {icon} #{r['id']} • {theme_ru}\n"
                f"     {status_text} • {_fmt_date(str(r['created_at']))}"
            )
            if str(r["status"]) == "PENDING":
                active += 1
        lines.append("")  # пустая строка между ЖК

    if active:
        lines.append(f"Активных обращений: {active}")
    else:
        lines.append("Все ваши обращения рассмотрены.")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Форматирование /contacts
# ---------------------------------------------------------------------------

def _format_contacts(complexes: list[dict]) -> str:
    """Форматирует контакты ЖК жильца.

    complexes — список dict с ключами name и contacts_info (из list_user_complexes).
    """
    if not complexes:
        return (
            "📞 Контакты\n\n"
            "Мы пока не можем определить ваш жилой комплекс.\n\n"
            "Напишите любое сообщение в чате вашего ЖК — бот вас зафиксирует. "
            "После этого команда /contacts покажет контакты вашей управляющей компании."
        )

    if len(complexes) == 1:
        c = complexes[0]
        header = f"📞 Контакты — {c['name']}\n\n"
    else:
        header = "📞 Контакты ваших жилых комплексов:\n\n"

    parts = []
    for c in complexes:
        info = (c.get("contacts_info") or "").strip()
        if len(complexes) > 1:
            parts.append(f"🏠 {c['name']}:")
        if info:
            parts.append(info)
        else:
            parts.append(
                "Контактная информация пока не заполнена управляющей компанией.\n"
                "🆘 По аварийным ситуациям обратитесь в аварийно-диспетчерскую службу вашего района."
            )
        parts.append("")  # разделитель между ЖК

    return header + "\n".join(parts).rstrip()


# ---------------------------------------------------------------------------
# Главный обработчик (вызывается ТОЛЬКО из личного диалога с ботом)
# ---------------------------------------------------------------------------

async def handle_resident_command(
    *,
    bot: Any,
    text: str,
    user_id: int | None,
    chat_id: int,
    escalations: Any | None,  # EscalationRepo | None
) -> bool:
    """Обрабатывает пользовательскую команду жильца в личном диалоге.

    Возвращает True если команда была распознана и обработана.
    Вызывать только из личного диалога (messages.py проверяет is_dialog_chat).
    """
    cmd = text.strip().lower().split("@")[0].split()[0]

    if cmd in ("/help", "/start", "/команды", "/cmd"):
        await _reply(bot, chat_id, RESIDENT_HELP_TEXT)
        return True

    if cmd == "/mystatus":
        await _handle_mystatus(bot=bot, chat_id=chat_id, user_id=user_id, escalations=escalations)
        return True

    if cmd == "/contacts":
        await _handle_contacts(bot=bot, chat_id=chat_id, user_id=user_id, escalations=escalations)
        return True

    return False


async def _handle_mystatus(
    *,
    bot: Any,
    chat_id: int,
    user_id: int | None,
    escalations: Any | None,
) -> None:
    if user_id is None:
        await _reply(bot, chat_id, "Не удалось определить ваш аккаунт. Попробуйте позже.")
        return
    if escalations is None:
        await _reply(bot, chat_id, "Сервис временно недоступен. Попробуйте позже.")
        return
    try:
        rows = await escalations.list_by_user(user_id=user_id, limit=10)
        msg = _format_mystatus(rows)
    except Exception as exc:
        log.warning("resident_cmd.mystatus_error", error=str(exc))
        msg = "Не удалось загрузить ваши обращения. Попробуйте позже."
    await _reply(bot, chat_id, msg)


async def _handle_contacts(
    *,
    bot: Any,
    chat_id: int,
    user_id: int | None,
    escalations: Any | None,
) -> None:
    if user_id is None:
        await _reply(bot, chat_id, "Не удалось определить ваш аккаунт. Попробуйте позже.")
        return
    if escalations is None:
        await _reply(bot, chat_id, "Сервис временно недоступен. Попробуйте позже.")
        return
    try:
        complexes = await escalations.list_user_complexes(user_id=user_id)
        msg = _format_contacts(complexes)
    except Exception as exc:
        log.warning("resident_cmd.contacts_error", error=str(exc))
        msg = "Не удалось загрузить контакты. Обратитесь в управляющую компанию."
    await _reply(bot, chat_id, msg)


async def _reply(bot: Any, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        log.warning("resident_cmd.send_failed", chat_id=chat_id, error=str(exc))
