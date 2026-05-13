"""Локализация строковых enum-значений на русский язык.

Используется в:
- уведомлениях управляющему (services/escalation.py)
- веб-интерфейсе (gui/app.py — Jinja2 globals)
"""

from __future__ import annotations

THEME_RU: dict[str, str] = {
    "EMERGENCY":    "🆘 Аварийная ситуация",
    "TECH_FAULT":   "🔧 Техническая неисправность",
    "IMPROVEMENT":  "🌿 Благоустройство",
    "SECURITY":     "🔒 Безопасность",
    "INFO_REQUEST": "ℹ️ Информационный запрос",
    "LEGAL_ORG":    "⚖️ Юридический / организационный",
    "UTILITY":      "💧 Коммунальные услуги",
    "OTHER":        "📎 Прочее",
}

URGENCY_RU: dict[str, str] = {
    "HIGH":   "🔴 Срочно",
    "MEDIUM": "🟡 Средняя",
    "LOW":    "🟢 Низкая",
}

CHARACTER_RU: dict[str, str] = {
    "QUESTION":        "❓ Вопрос",
    "COMPLAINT_MILD":  "😤 Жалоба (мягкая)",
    "COMPLAINT_STRONG":"😡 Жалоба (жёсткая)",
    "AGGRESSION":      "🚫 Агрессия",
    "PROVOCATION":     "⚠️ Провокация",
    "REPORT":          "📋 Заявление",
}

REASON_RU: dict[str, str] = {
    "aggression":           "🚫 Агрессия / оскорбления",
    "provocation":          "⚠️ Провокация",
    "high_urgency":         "🆘 Высокая срочность",
    "always_escalate_theme":"🏛 Особая тема",
    "low_confidence":       "❓ Низкая уверенность классификатора",
    "llm_error":            "🛠 Ошибка ИИ",
    "after_hours":          "🌙 Вне рабочих часов",
    "spam_drugs":           "💊 Спам: наркотики",
    "spam_crypto":          "₿ Спам: криптовалюта",
    "spam_earn":            "💰 Спам: заработок / MLM",
    "spam_esoteric":        "🔮 Спам: эзотерика",
    "spam_ads":             "📢 Спам: реклама",
    "spam_mass_mention":    "📣 Спам: массовые упоминания",
    "spam_unknown":         "🗑 Спам: неизвестный тип",
    "fragment_troll":       "🧩 Дробный мат / троллинг частями",
}


def theme_ru(value: str) -> str:
    return THEME_RU.get(value, value)


def urgency_ru(value: str) -> str:
    return URGENCY_RU.get(value, value)


def character_ru(value: str) -> str:
    return CHARACTER_RU.get(value, value)


def reason_ru(value: str) -> str:
    return REASON_RU.get(value, value)
