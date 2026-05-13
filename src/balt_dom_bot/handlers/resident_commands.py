"""Пользовательские команды для жильцов в чате ЖК.

Команды технически принимаются в любом чате, но основная функциональность
(/mystatus, обработка обращений) работает только в зарегистрированном
групповом чате ЖК — в личных сообщениях обращения не фиксируются.

/help, /start  — справка для жильца
/mystatus      — статус последних обращений в данном чате ЖК
/contacts      — контактная информация (аварийная служба, телефоны УК)
"""

from __future__ import annotations

from typing import Any

from balt_dom_bot.l10n import REASON_RU, THEME_RU
from balt_dom_bot.log import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Текст /help — единственный источник истины. Импортируется в lifecycle.py и
# messages.py. НЕ содержит внутренних деталей работы бота.
# ---------------------------------------------------------------------------

RESIDENT_HELP_TEXT = (
    "Добрый день! Я — цифровой помощник управляющей компании. 🏠\n\n"
    "Просто напишите свой вопрос или опишите ситуацию — и я помогу:\n\n"
    "🔧 Вопросы по дому и инфраструктуре — отвечу сразу или передам специалисту\n"
    "🆘 Авария или срочная ситуация — немедленно уведомлю ответственного\n"
    "💡 Предложения по улучшению — приму и зафиксирую\n\n"
    "Полезные команды:\n"
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


def _fmt_date(iso: str) -> str:
    """ISO datetime → 'дд.мм.гггг чч:мм'."""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso[:16] if iso else "—"


def _format_mystatus(rows: list) -> str:
    if not rows:
        return (
            "У вас пока нет зафиксированных обращений в этом чате ЖК.\n"
            "Напишите свой вопрос в чат — мы его зафиксируем и обработаем."
        )

    lines = ["📋 Ваши последние обращения:\n"]
    active = 0
    for r in rows:
        icon = _STATUS_ICON.get(str(r["status"]), "•")
        status_text = {
            "PENDING": "в работе",
            "APPROVED": "рассмотрено",
            "IGNORED": "закрыто",
        }.get(str(r["status"]), str(r["status"]))

        # Тема через l10n (убираем эмодзи из начала для компактности).
        raw_theme = r["theme"] or ""
        theme_ru = THEME_RU.get(raw_theme, raw_theme).lstrip("🆘🔧🌿🔒ℹ️⚖️💧📎 ").strip()

        lines.append(
            f"{icon} #{r['id']} • {theme_ru}\n"
            f"   {status_text} • {_fmt_date(str(r['created_at']))}"
        )
        if str(r["status"]) == "PENDING":
            active += 1

    if active:
        lines.append(f"\nАктивных обращений: {active}")
    else:
        lines.append("\nВсе ваши обращения рассмотрены.")
    return "\n".join(lines)


def _format_contacts(complex_name: str, contacts_info: str | None) -> str:
    if contacts_info and contacts_info.strip():
        return f"📞 Контакты — {complex_name}\n\n{contacts_info.strip()}"
    return (
        f"📞 Контакты — {complex_name}\n\n"
        "Контактная информация пока не заполнена управляющей компанией.\n\n"
        "🆘 По аварийным ситуациям обратитесь в аварийно-диспетчерскую службу вашего района."
    )


# ---------------------------------------------------------------------------
# Главный обработчик
# ---------------------------------------------------------------------------

async def handle_resident_command(
    *,
    bot: Any,
    text: str,
    user_id: int | None,
    chat_id: int,
    complexes: Any,          # ComplexesRepo
    escalations: Any | None,  # EscalationRepo | None
) -> bool:
    """Обрабатывает пользовательскую команду жильца.

    Возвращает True если команда была распознана и обработана.
    Pipeline после этого запускать не нужно.
    """
    cmd = text.strip().lower().split("@")[0].split()[0]

    if cmd in ("/help", "/start", "/команды", "/cmd"):
        await _reply(bot, chat_id, RESIDENT_HELP_TEXT)
        return True

    if cmd == "/mystatus":
        await _handle_mystatus(
            bot=bot, chat_id=chat_id, user_id=user_id, escalations=escalations,
        )
        return True

    if cmd == "/contacts":
        await _handle_contacts(bot=bot, chat_id=chat_id, complexes=complexes)
        return True

    return False


async def _handle_mystatus(
    *,
    bot: Any,
    chat_id: int,
    user_id: int | None,
    escalations: Any | None,
) -> None:
    if user_id is None or escalations is None:
        await _reply(bot, chat_id, "Не удалось получить информацию об обращениях.")
        return
    try:
        rows = await escalations.list_by_user_in_chat(
            user_id=user_id, chat_id=chat_id, limit=5,
        )
        text = _format_mystatus(rows)
    except Exception as exc:
        log.warning("resident_cmd.mystatus_error", error=str(exc))
        text = "Не удалось загрузить ваши обращения. Попробуйте позже."
    await _reply(bot, chat_id, text)


async def _handle_contacts(
    *,
    bot: Any,
    chat_id: int,
    complexes: Any,
) -> None:
    try:
        c = await complexes.find_by_chat(chat_id)
        if c is not None:
            name = c.name
            contacts_info = getattr(c, "contacts_info", None)
        else:
            name = "жилого комплекса"
            contacts_info = None
        text = _format_contacts(name, contacts_info)
    except Exception as exc:
        log.warning("resident_cmd.contacts_error", error=str(exc))
        text = "Не удалось загрузить контакты. Обратитесь в управляющую компанию."
    await _reply(bot, chat_id, text)


async def _reply(bot: Any, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        log.warning("resident_cmd.send_failed", chat_id=chat_id, error=str(exc))
